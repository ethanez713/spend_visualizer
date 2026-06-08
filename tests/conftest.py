"""Shared test fixtures — everything offline, deterministic, tmp-isolated.

Mirrors the `state` fixture pattern in transactions/tests/conftest.py: no test ever
touches a real var/, a real data file, or the network.
"""
import pytest


@pytest.fixture
def make_record():
    """Factory for a realistic raw transaction-shaped dict (Plaid-ish)."""
    def _make(**over):
        rec = {
            "transaction_id": "txn_1",
            "account_id": "acc_1",
            "pending": False,
            "pending_transaction_id": None,
            "date": "2026-01-15",
            "name": "TRADER JOES #123",
            "merchant_name": "Trader Joe's",
            "amount": 42.5,
            "personal_finance_category": {
                "primary": "FOOD_AND_DRINK",
                "detailed": "FOOD_AND_DRINK_GROCERIES",
                "confidence_level": "VERY_HIGH",
            },
        }
        rec.update(over)
        return rec
    return _make


@pytest.fixture
def store_of(make_record):
    """Build a {key: record} store from kwargs-overrides keyed by transaction_id."""
    def _build(*records):
        out = {}
        for r in records:
            rec = make_record(**r) if isinstance(r, dict) else r
            out[rec["transaction_id"]] = rec
        return out
    return _build
