"""Tests for the periodic 90-day safety-net overfetch.

A fake Plaid client (the test_fetch_window pattern) returns canned /transactions/get
windows so the cadence logic, golden add/overwrite merging, and the report-only stale
detection (flag, NEVER delete) are exercised offline. Dates are built relative to the
real today because the overfetch anchors its window on date.today().
"""
from datetime import date, timedelta

from plaid.exceptions import ApiException

from src.overfetch import (
    OVERFETCH_INTERVAL_DAYS,
    overfetch_due,
    run_overfetch,
)
from src.plaid_client import load_overfetch_state, save_overfetch_state


class FakeTxn(dict):
    """A transaction supporting both txn['x'] and txn.to_dict(), like Plaid's model."""
    def to_dict(self):
        return dict(self)


class FakeClient:
    """transactions_get returns offset-keyed slices; a token may map to an Exception."""
    def __init__(self, per_token, page_size=500):
        self.per_token = per_token
        self.page_size = page_size

    def transactions_get(self, req):
        data = self.per_token[req.access_token]
        if isinstance(data, Exception):
            raise data
        offset = req.options.offset
        page = data[offset:offset + self.page_size]
        return {"transactions": page, "total_transactions": len(data)}


def _token(access_token, institution="Chase", item_id="it1"):
    return {"access_token": access_token, "item_id": item_id, "institution": institution}


def _days_ago(n):
    return (date.today() - timedelta(days=n)).isoformat()


def _txn(tid, days_ago=10, account_id="acc_1", pending=False, amount=1.0):
    return FakeTxn(
        transaction_id=tid,
        account_id=account_id,
        date=_days_ago(days_ago),
        pending=pending,
        amount=amount,
    )


def _rec(tid, **over):
    """A raw-store record (plain dict, the shape normalize_txn emits)."""
    return dict(_txn(tid, **over))


# --- cadence -------------------------------------------------------------------------

def given_no_state_when_due_checked_then_due(state):
    assert overfetch_due(None) is True


def given_recent_overfetch_when_due_checked_then_not_due(state):
    save_overfetch_state(_days_ago(5))
    assert overfetch_due(None) is False


def given_old_overfetch_when_due_checked_then_due(state):
    save_overfetch_state(_days_ago(OVERFETCH_INTERVAL_DAYS + 1))
    assert overfetch_due(None) is True


def given_force_flags_when_due_checked_then_cadence_overridden(state):
    save_overfetch_state(_days_ago(5))
    assert overfetch_due(True) is True       # --overfetch: run even though recent
    save_overfetch_state(_days_ago(400))
    assert overfetch_due(False) is False     # --no-overfetch: skip even though due


def given_saved_state_when_loaded_then_round_trips(state):
    save_overfetch_state("2026-01-01")
    assert load_overfetch_state() == {"last_overfetch": "2026-01-01"}


# --- golden merge: add + overwrite ---------------------------------------------------

def given_record_missing_locally_when_overfetch_then_added(state):
    client = FakeClient({"tok": [_txn("a")]})
    raw = {}

    counts = run_overfetch(client, [_token("tok")], raw)

    assert counts == {"added": 1, "changed": 0, "stale": 0, "items_ok": 1}
    assert raw["a"]["transaction_id"] == "a"
    # The cadence clock advanced and a log entry was appended.
    assert load_overfetch_state()["last_overfetch"] == date.today().isoformat()
    assert state.overfetch_log.exists()


def given_locally_changed_record_when_overfetch_then_overwritten_to_golden(state):
    client = FakeClient({"tok": [_txn("a", amount=9.0)]})
    raw = {"a": _rec("a", amount=1.0)}

    counts = run_overfetch(client, [_token("tok")], raw)

    assert counts["changed"] == 1 and counts["added"] == 0
    assert raw["a"]["amount"] == 9.0


def given_in_sync_store_when_overfetch_then_all_counts_zero(state):
    client = FakeClient({"tok": [_txn("a")]})
    raw = {"a": _rec("a")}

    counts = run_overfetch(client, [_token("tok")], raw)

    assert counts == {"added": 0, "changed": 0, "stale": 0, "items_ok": 1}


# --- stale detection: flag, never delete ---------------------------------------------

def given_record_absent_from_golden_when_overfetch_then_flagged_not_deleted(state):
    # Plaid's golden window covers acc_1 but no longer returns "ghost".
    client = FakeClient({"tok": [_txn("a")]})
    raw = {"a": _rec("a"), "ghost": _rec("ghost")}

    counts = run_overfetch(client, [_token("tok")], raw)

    assert counts["stale"] == 1
    assert "ghost" in raw  # flagged in the log, NEVER deleted from the store
    log_line = state.overfetch_log.read_text().strip()
    assert '"ghost"' in log_line


def given_failed_item_when_overfetch_then_its_records_not_flagged(state):
    # The failing bank contributes no golden records → its account is not covered →
    # its local records can never be falsely flagged stale. Healthy items still merge.
    client = FakeClient({
        "ok": [_txn("a", account_id="acc_ok")],
        "boom": ApiException(status=400, reason="ITEM_LOGIN_REQUIRED"),
    })
    raw = {"orphan": _rec("orphan", account_id="acc_boom")}

    counts = run_overfetch(client, [_token("ok", "Bank1"), _token("boom", "Bank2", "it2")], raw)

    assert counts["stale"] == 0
    assert counts["added"] == 1 and "a" in raw


def given_all_items_fail_when_overfetch_then_nothing_merged_clock_not_advanced(state):
    client = FakeClient({"boom": ApiException(status=500, reason="INTERNAL_SERVER_ERROR")})
    raw = {"a": _rec("a")}

    counts = run_overfetch(client, [_token("boom")], raw)

    assert counts == {"added": 0, "changed": 0, "stale": 0, "items_ok": 0}
    assert raw == {"a": _rec("a")}
    assert not state.overfetch_state.exists()  # transient outage: retry next run


def given_out_of_window_record_when_overfetch_then_not_flagged(state):
    client = FakeClient({"tok": [_txn("a")]})
    raw = {"old": _rec("old", days_ago=200)}  # aged out of the 90-day window

    counts = run_overfetch(client, [_token("tok")], raw)

    assert counts["stale"] == 0 and "old" in raw


def given_pending_record_when_overfetch_then_not_flagged(state):
    # A pending that settled under a new posted id is normal churn handled by
    # dedupe_supersede at persist time — not a sync bug.
    client = FakeClient({"tok": [_txn("a")]})
    raw = {"p": _rec("p", pending=True)}

    counts = run_overfetch(client, [_token("tok")], raw)

    assert counts["stale"] == 0


def given_record_within_trailing_buffer_when_overfetch_then_not_flagged(state):
    client = FakeClient({"tok": [_txn("a")]})
    raw = {"fresh_edge": _rec("fresh_edge", days_ago=1)}

    counts = run_overfetch(client, [_token("tok")], raw)

    assert counts["stale"] == 0
