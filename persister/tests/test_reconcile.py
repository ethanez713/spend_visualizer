"""Tests for reconcile.py — classification + preserved union."""
from persister.reconcile import reconcile


def given_local_and_remote_when_reconcile_then_classified(make_record):
    local = {
        "same": make_record(transaction_id="same", amount=1.0),
        "lonly": make_record(transaction_id="lonly"),
        "conf": make_record(transaction_id="conf", amount=10.0),
    }
    remote = {
        "same": make_record(transaction_id="same", amount=1.0),     # identical
        "ronly": make_record(transaction_id="ronly"),                # aged out of Plaid
        "conf": make_record(transaction_id="conf", amount=99.0),     # differs
    }
    rep = reconcile(local, remote)

    assert rep.in_sync == ["same"]
    assert rep.local_only == ["lonly"]
    assert rep.remote_only == ["ronly"]
    assert rep.conflicts == ["conf"]


def given_reconcile_when_done_then_merged_is_full_union(make_record):
    local = {"a": make_record(transaction_id="a"), "c": make_record(transaction_id="c", amount=1.0)}
    remote = {"b": make_record(transaction_id="b"), "c": make_record(transaction_id="c", amount=2.0)}
    rep = reconcile(local, remote)

    assert set(rep.merged) == {"a", "b", "c"}  # nothing dropped


def given_conflict_when_reconcile_then_remote_value_retained(make_record):
    local = {"c": make_record(transaction_id="c", amount=10.0)}
    remote = {"c": make_record(transaction_id="c", amount=99.0)}
    rep = reconcile(local, remote)
    # Conflict keeps the remote value until a golden re-fetch overwrites it.
    assert rep.merged["c"]["amount"] == 99.0


def given_equal_records_key_order_differs_when_reconcile_then_in_sync():
    # Content hash is canonical (sort_keys) → field order doesn't matter.
    local = {"x": {"transaction_id": "x", "a": 1, "b": 2}}
    remote = {"x": {"transaction_id": "x", "b": 2, "a": 1}}
    rep = reconcile(local, remote)
    assert rep.in_sync == ["x"]
    assert rep.conflicts == []
