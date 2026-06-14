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


def given_metadata_only_difference_when_reconcile_then_in_sync_local_kept(make_record):
    # A locally-stamped bookkeeping field (e.g. txn_owner) must not read as a
    # conflict against a remote written before the field existed — and the merged
    # store must keep the LOCAL copy, the side carrying the stamp.
    local = {"x": make_record(transaction_id="x", txn_owner="Alice")}
    remote = {"x": make_record(transaction_id="x")}
    rep = reconcile(local, remote, metadata_fields=("txn_owner",))

    assert rep.in_sync == ["x"]
    assert rep.conflicts == []
    assert rep.merged["x"]["txn_owner"] == "Alice"


def given_real_difference_when_reconcile_with_metadata_fields_then_still_conflict(make_record):
    # metadata_fields only masks the named fields — genuine content drift still conflicts.
    local = {"x": make_record(transaction_id="x", amount=10.0, txn_owner="Alice")}
    remote = {"x": make_record(transaction_id="x", amount=99.0)}
    rep = reconcile(local, remote, metadata_fields=("txn_owner",))

    assert rep.conflicts == ["x"]
    assert rep.merged["x"]["amount"] == 99.0  # remote retained pending golden re-fetch


def given_metadata_only_difference_when_reconcile_without_param_then_conflict(make_record):
    # Default () keeps the exact pre-existing behavior: any field difference conflicts.
    local = {"x": make_record(transaction_id="x", txn_owner="Alice")}
    remote = {"x": make_record(transaction_id="x")}
    rep = reconcile(local, remote)
    assert rep.conflicts == ["x"]


def given_conflict_resolver_when_reconcile_then_resolver_picks_winner(make_record):
    # Consumers own conflict policy (e.g. newest-audit-stamp): the resolver's pick
    # lands in merged, while the conflict is still reported for audit.
    local = {"x": make_record(transaction_id="x", amount=10.0)}
    remote = {"x": make_record(transaction_id="x", amount=99.0)}

    rep = reconcile(local, remote, conflict_resolver=lambda lo, re: lo)

    assert rep.conflicts == ["x"]
    assert rep.merged["x"]["amount"] == 10.0   # resolver chose LOCAL


def given_conflict_resolver_when_no_conflicts_then_resolver_never_called(make_record):
    # in_sync / local_only / remote_only are not the resolver's business.
    calls = []
    local = {"same": make_record(transaction_id="same"),
             "mine": make_record(transaction_id="mine")}
    remote = {"same": make_record(transaction_id="same"),
              "theirs": make_record(transaction_id="theirs")}

    rep = reconcile(local, remote,
                    conflict_resolver=lambda lo, re: calls.append(1) or lo)

    assert calls == []
    assert set(rep.merged) == {"same", "mine", "theirs"}
