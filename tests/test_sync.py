"""Tests for sync_item: the cursor-based added/modified/removed delta loop.

Uses a fake Plaid client that returns canned sync pages, so the loop logic
(deltas, de-dup, pagination, first-run retry) is exercised without any network.
"""
from plaid_client import load_cursors
from fetch_transactions import sync_item

ENTRY = {"item_id": "it1", "access_token": "tok"}


class FakeTxn(dict):
    """A transaction that supports both txn['x'] and txn.to_dict(), like Plaid's."""
    def to_dict(self):
        return dict(self)


class FakeClient:
    def __init__(self, pages):
        self._pages = list(pages)
        self.calls = 0

    def transactions_sync(self, request):
        self.calls += 1
        return self._pages.pop(0)


def _page(added=(), modified=(), removed=(), has_more=False, next_cursor="c"):
    return {
        "added": list(added),
        "modified": list(modified),
        "removed": [{"transaction_id": rid} for rid in removed],
        "has_more": has_more,
        "next_cursor": next_cursor,
    }


def given_added_modified_removed_when_sync_then_store_and_counts_update(state):
    raw_store = {"old": {"transaction_id": "old", "date": "2026-01-01"}}
    pages = [_page(
        added=[FakeTxn(transaction_id="a1", date="2026-01-02"),
               FakeTxn(transaction_id="a2", date="2026-01-03")],
        modified=[FakeTxn(transaction_id="m1", date="2026-01-04")],
        removed=["old"],
        next_cursor="cur1",
    )]
    counts = sync_item(FakeClient(pages), ENTRY, raw_store)

    assert counts == {"added": 2, "modified": 1, "removed": 1}
    assert set(raw_store) == {"a1", "a2", "m1"}
    assert load_cursors()["it1"] == "cur1"  # cursor checkpointed


def given_removed_id_absent_when_sync_then_not_counted(state):
    raw_store = {}
    pages = [_page(removed=["ghost"], next_cursor="cur1")]
    counts = sync_item(FakeClient(pages), ENTRY, raw_store)
    assert counts["removed"] == 0
    assert raw_store == {}


def given_first_run_history_not_ready_when_sync_then_retry_signal(state):
    # Empty page with no cursor on the first call => Plaid still pulling history.
    raw_store = {}
    pages = [_page(has_more=False, next_cursor="")]
    counts = sync_item(FakeClient(pages), ENTRY, raw_store)

    assert counts.get("_retry") is True
    assert raw_store == {}
    assert load_cursors() == {}  # nothing checkpointed on a retry


def given_paginated_response_when_sync_then_all_pages_applied(state):
    raw_store = {}
    pages = [
        _page(added=[FakeTxn(transaction_id="p1", date="2026-01-01")],
              has_more=True, next_cursor="c1"),
        _page(added=[FakeTxn(transaction_id="p2", date="2026-01-02")],
              has_more=False, next_cursor="c2"),
    ]
    client = FakeClient(pages)
    counts = sync_item(client, ENTRY, raw_store)

    assert counts["added"] == 2
    assert set(raw_store) == {"p1", "p2"}
    assert client.calls == 2
    assert load_cursors()["it1"] == "c2"  # final cursor wins
