"""Tests for fetch_window: the bounded /transactions/get repair fetch.

A fake Plaid client returns canned, server-paginated pages so the pagination loop,
.to_dict() shaping, and the "skip an item on ApiException" guarantee are exercised
without any network. No account metadata is baked into the returned records — they must
match the raw-store shape so they reconcile cleanly with the sync path.
"""
from datetime import date

from plaid.exceptions import ApiException

from src.fetch_window import fetch_window


class FakeTxn(dict):
    """A transaction supporting both txn['x'] and txn.to_dict(), like Plaid's model."""
    def to_dict(self):
        return dict(self)


class FakeClient:
    """transactions_get returns `page_size`-sized slices keyed by the request offset.

    Simulates Plaid's server-side page cap: the request's count is ignored, the server
    decides how many to return, and total_transactions drives when the caller stops.
    per_token maps access_token → either a list of FakeTxn or an Exception to raise.
    """
    def __init__(self, per_token, page_size=500):
        self.per_token = per_token
        self.page_size = page_size
        self.calls = 0

    def transactions_get(self, req):
        self.calls += 1
        data = self.per_token[req.access_token]
        if isinstance(data, Exception):
            raise data
        offset = req.options.offset
        page = data[offset:offset + self.page_size]
        return {"transactions": page, "total_transactions": len(data)}


def _token(access_token, institution="Chase", item_id="it1", owner="u1"):
    return {"access_token": access_token, "item_id": item_id,
            "institution": institution, "owner": owner}


def _txns(*ids):
    return [FakeTxn(transaction_id=i, date="2026-01-10", pending=False, amount=1.0) for i in ids]


def given_paginated_window_when_fetch_then_all_pages_collected_and_to_dict_shaped():
    client = FakeClient({"tok": _txns("a", "b", "c")}, page_size=2)

    out = fetch_window(client, [_token("tok")], "2026-01-01", "2026-02-01")

    # 3 records across 2 pages (2 + 1), pagination stops at total_transactions.
    assert {r["transaction_id"] for r in out} == {"a", "b", "c"}
    assert client.calls == 2
    # Records are plain dicts (to_dict()ed), not FakeTxn / Plaid objects…
    assert all(type(r) is dict for r in out)
    # …and carry NO joined account metadata (raw-store shape → reconcile parity)…
    assert all("institution" not in r and "account_name" not in r for r in out)
    # …but DO carry the owner stamp, exactly like the sync path (a golden
    # overwrite must never strip txn_owner).
    assert all(r["txn_owner"] == "u1" for r in out)


def given_accepts_date_objects_when_fetch_then_works():
    client = FakeClient({"tok": _txns("a")})
    out = fetch_window(client, [_token("tok")], date(2026, 1, 1), date(2026, 2, 1))
    assert [r["transaction_id"] for r in out] == ["a"]


def given_one_item_errors_when_fetch_then_skipped_others_returned():
    client = FakeClient({
        "ok1": _txns("a", "b"),
        "boom": ApiException(status=400, reason="INVALID_ACCESS_TOKEN"),
        "ok2": _txns("c"),
    })
    tokens = [_token("ok1", "Bank1"), _token("boom", "Bank2"), _token("ok2", "Bank3")]

    out = fetch_window(client, tokens, "2026-01-01", "2026-02-01")

    # The erroring item is skipped (no crash); the healthy items still return.
    assert {r["transaction_id"] for r in out} == {"a", "b", "c"}


def given_empty_window_when_fetch_then_no_records_single_call():
    client = FakeClient({"tok": []})
    out = fetch_window(client, [_token("tok")], "2026-01-01", "2026-02-01")
    assert out == []
    assert client.calls == 1  # one call returns an empty page → stop
