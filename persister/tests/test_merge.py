"""Tests for merge.py — Plaid-golden overwrite, preserve base-only, dedupe."""
from persister.merge import merge_golden


def given_fresh_when_merge_then_overwrites_by_key(make_record):
    base = {"a": make_record(transaction_id="a", amount=1.0)}
    fresh = [make_record(transaction_id="a", amount=99.0)]
    out = merge_golden(base, fresh)
    assert out["a"]["amount"] == 99.0  # golden wins


def given_base_only_key_when_merge_then_preserved(make_record):
    base = {"keep": make_record(transaction_id="keep")}
    fresh = [make_record(transaction_id="new")]
    out = merge_golden(base, fresh)
    assert set(out) == {"keep", "new"}  # base-only never dropped


def given_fresh_introduces_posted_when_merge_then_settled_pending_dropped(make_record):
    base = {"pend_1": make_record(transaction_id="pend_1", pending=True)}
    fresh = [make_record(transaction_id="post_1", pending=False,
                         pending_transaction_id="pend_1")]
    out = merge_golden(base, fresh)
    assert set(out) == {"post_1"}  # the now-settled pending is deduped away


def given_fresh_missing_key_when_merge_then_skipped(make_record):
    base = {"a": make_record(transaction_id="a")}
    fresh = [{"amount": 5.0}]  # no transaction_id
    out = merge_golden(base, fresh)
    assert set(out) == {"a"}  # malformed fresh record skipped, no crash
