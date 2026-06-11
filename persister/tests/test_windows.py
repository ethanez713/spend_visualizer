"""Tests for windows.py — bounded repair-fetch date window."""
from datetime import date, timedelta

from persister.windows import EMPTY_BACKFILL_DAYS, compute_window


def given_settled_rows_when_window_then_start_is_latest_settled_minus_7d(make_record):
    store = {
        "a": make_record(transaction_id="a", date="2026-05-01", pending=False),
        "b": make_record(transaction_id="b", date="2026-05-20", pending=False),
    }
    win = compute_window(store)
    assert win.start_date == (date(2026, 5, 20) - timedelta(days=7)).isoformat()
    assert win.end_date == date.today().isoformat()
    assert win.pending_ids == []


def given_pending_older_than_window_when_window_then_start_covers_pending(make_record):
    store = {
        "settled": make_record(transaction_id="settled", date="2026-05-20", pending=False),
        "old_pend": make_record(transaction_id="old_pend", date="2026-01-01", pending=True),
    }
    win = compute_window(store)
    assert win.start_date == "2026-01-01"  # pulled back to cover the pending row
    assert win.pending_ids == ["old_pend"]


def given_extra_tids_when_window_then_start_covers_them(make_record):
    store = {
        "settled": make_record(transaction_id="settled", date="2026-05-20", pending=False),
        "conflict": make_record(transaction_id="conflict", date="2025-12-15", pending=False),
    }
    win = compute_window(store, extra_tids=["conflict"])
    assert win.start_date == "2025-12-15"


def given_empty_store_when_window_then_backfill_default():
    win = compute_window({})
    expected = (date.today() - timedelta(days=EMPTY_BACKFILL_DAYS)).isoformat()
    assert win.start_date == expected
    assert win.end_date == date.today().isoformat()
    assert win.pending_ids == []


def given_date_with_time_component_when_window_then_parsed(make_record):
    store = {"a": make_record(transaction_id="a", date="2026-05-20T12:00:00", pending=False)}
    win = compute_window(store)
    assert win.start_date == (date(2026, 5, 20) - timedelta(days=7)).isoformat()
