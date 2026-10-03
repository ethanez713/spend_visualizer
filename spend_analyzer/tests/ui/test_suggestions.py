"""The Review view: each decision is one intent, and the row leaves the queue at once.

Runs over the archive's real pending suggestions (skips when there are none); the
intent log is redirected to tmp by conftest.
"""
import json

import pytest

from tests.ui._harness import widget


def _queue(at) -> list[str]:
    return [b.key for b in at.button if (b.key or "").startswith("rv_keep_")]


def _only_intent(log) -> dict:
    lines = log.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, lines
    return json.loads(lines[0])


@pytest.fixture
def review(boot_app, cube_df):
    if not cube_df["review_pending"].fillna(False).any():
        pytest.skip("no pending suggestions in the archive")
    return boot_app("Suggestions")


def test_given_pending_row_when_kept_then_suggestion_is_dismissed_and_row_leaves(
        review, cube_df, isolate_writes):
    before = _queue(review)
    tid = before[0].removeprefix("rv_keep_")
    row = cube_df[cube_df["transaction_id"] == tid].iloc[0]
    widget(review.button, key=before[0]).click()
    review.run()
    assert not review.exception, [str(e.value) for e in review.exception]

    it = _only_intent(isolate_writes)
    assert (it["action"], it["source"]) == ("dismiss", "ui-review")
    assert it["match"] == {"transaction_id": tid}
    assert (it["dismissed"]["primary"], it["dismissed"]["detailed"]) == (
        row["review_primary"], row["review_detailed"])
    assert before[0] not in _queue(review) and len(_queue(review)) == len(before) - 1


def test_given_pending_row_when_accepted_then_suggestion_becomes_the_category(
        review, cube_df, isolate_writes):
    key = [b.key for b in review.button if (b.key or "").startswith("rv_accept_")][0]
    tid = key.removeprefix("rv_accept_")
    row = cube_df[cube_df["transaction_id"] == tid].iloc[0]
    widget(review.button, key=key).click()
    review.run()
    assert not review.exception, [str(e.value) for e in review.exception]

    it = _only_intent(isolate_writes)
    assert (it["set"]["primary"], it["set"]["detailed"]) == (
        row["review_primary"], row["review_detailed"])
    assert f"rv_keep_{tid}" not in _queue(review)
