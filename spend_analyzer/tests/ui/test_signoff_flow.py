"""The Review view over the real archive with an empty (tmp) intent log: the Sheet baseline,
signing off one category's group, and undoing it. Every write lands in the tmp log."""
import json

import pytest

import signoff
from budget_model import load_budget_model
from tests.ui._harness import widget


@pytest.fixture
def app_df(cube_df, isolate_writes):
    """The frame the app builds over the tmp intent log (``cube_df`` replays the live one,
    whose overrides move a few rows between categories)."""
    from data import build_cube
    return build_cube().df


def _signoffs(log):
    lines = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    return [json.loads(line) for line in lines if json.loads(line).get("action") == "budget_review"]


def test_given_no_signoffs_when_baseline_pressed_then_one_signoff_of_the_sheet_rows(
        boot_app, isolate_writes, app_df):
    expected = signoff.baseline(app_df, signoff.manifest_path(), load_budget_model().needs_decision)
    if not expected:
        pytest.skip("no Sheet export manifest in the data root")
    at = boot_app("Review")

    widget(at.button, key="so_baseline").click()
    at.run()

    written = _signoffs(isolate_writes)
    assert not at.exception and len(written) == 1
    assert written[0]["set"]["reviewed"] == expected


def test_given_a_category_group_when_signed_off_then_one_signoff_of_exactly_its_rows(
        boot_app, isolate_writes, app_df):
    dining = set(app_df.loc[app_df["budget_cat"] == "Dining Out", "transaction_id"])
    if not dining:
        pytest.skip("no Dining Out rows in the archive")
    at = boot_app("Review")

    widget(at.button, key="so_dining_out_0_sign").click()
    at.run()

    written = _signoffs(isolate_writes)
    assert not at.exception and len(written) == 1
    assert set(written[0]["set"]["reviewed"]) == dining
    assert set(written[0]["set"]["reviewed"].values()) == {"Dining Out"}
    assert "so_dining_out_1_sign" not in [b.key for b in at.button]


def test_given_a_signoff_when_undone_then_revoked_and_the_group_returns(
        boot_app, isolate_writes, app_df):
    if not (app_df["budget_cat"] == "Dining Out").any():
        pytest.skip("no Dining Out rows in the archive")
    at = boot_app("Review")
    widget(at.button, key="so_dining_out_0_sign").click()
    at.run()
    sign_id = _signoffs(isolate_writes)[0]["id"]

    widget(at.button, key=f"so_undo_{sign_id}").click()
    at.run()

    entries = [json.loads(line) for line in isolate_writes.read_text().splitlines()]
    assert not at.exception
    assert {"action": "revoke", "revokes": sign_id}.items() <= entries[-1].items()
    assert "so_dining_out_1_sign" in [b.key for b in at.button]
