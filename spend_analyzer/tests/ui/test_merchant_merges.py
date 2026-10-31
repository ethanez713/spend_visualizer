"""Merge, reject and revoke on the real archive; the intent log is redirected to tmp."""
import json

import pytest

from tests.ui._harness import go, widget


def _count(at) -> int:
    return next(int(e.label.rsplit("· ", 1)[1]) for e in at.expander
                if e.label.startswith("Possible duplicates"))


def _button_keys(at, prefix: str) -> list[str]:
    return [b.key for b in at.button if (b.key or "").startswith(prefix)]


def _merchant_options(at) -> int:
    return len(widget(at.selectbox, key="merchant_search").options)


def _lines(log) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()]


@pytest.fixture
def merchants(boot_app):
    at = boot_app("Merchants")
    if not _button_keys(at, "mm_merge_"):
        pytest.skip("no merge candidates in the archive")
    return at


def test_given_a_candidate_when_merged_then_one_merchant_fewer_and_revoke_restores_it(
        merchants, isolate_writes):
    at, before, candidates = merchants, _merchant_options(merchants), _count(merchants)

    widget(at.button, key=_button_keys(at, "mm_merge_")[0]).click()
    at.run()

    assert not at.exception, [str(e.value) for e in at.exception]
    written = _lines(isolate_writes)
    assert len(written) == 1 and written[0]["action"] == "merchant_alias"
    assert _merchant_options(at) == before - 1
    assert not any(e.label.startswith("Possible duplicates") for e in at.expander) or _count(at) < candidates

    go(at, "Overrides")
    widget(at.button, key=f"revoke_merge_{written[0]['id']}").click()
    at.run()
    go(at, "Merchants")

    assert _merchant_options(at) == before and _count(at) == candidates


def test_given_a_candidate_when_rejected_then_it_is_not_suggested_again(merchants, isolate_writes):
    at, candidates = merchants, _count(merchants)
    key = _button_keys(at, "mm_distinct_")[0]

    widget(at.button, key=key).click()
    at.run()

    assert not at.exception, [str(e.value) for e in at.exception]
    assert "distinct" in _lines(isolate_writes)[0]["set"]
    assert key not in _button_keys(at, "mm_distinct_")
    assert not any(e.label.startswith("Possible duplicates") for e in at.expander) or _count(at) == candidates - 1


def test_given_a_merchant_when_opened_then_any_other_can_be_merged_by_hand(
        boot_app, isolate_writes, cube_df):
    at = boot_app("Merchants")
    search = widget(at.selectbox, key="merchant_search")
    before = len(search.options)
    search.set_value(search.options[0]).run()
    assert not at.exception

    pick = widget(at.selectbox, key_prefix="mm_pick_")
    shown = pick.key.removeprefix("mm_pick_")
    spending = cube_df[(cube_df["flow"] == "spend") & ~cube_df["merchant_id"].isin(["", "unknown", shown])]
    pick.set_value(spending["merchant_id"].iloc[0]).run()
    widget(at.button, key_prefix="mm_manual_").click()
    at.run()

    assert not at.exception, [str(e.value) for e in at.exception]
    assert "into" in _lines(isolate_writes)[0]["set"]
    assert len(widget(at.selectbox, key="merchant_search").options) < before
