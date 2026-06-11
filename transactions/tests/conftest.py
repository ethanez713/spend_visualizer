"""Shared fixtures.

The `state` fixture redirects every module-global file path (tokens, cursors,
CSV, raw archive) into a per-test tmp dir, so a test can never read or clobber
the real local state / output files.
"""
from types import SimpleNamespace

import pytest

import src.fetch_transactions as fetch_transactions
import src.plaid_client as plaid_client


@pytest.fixture
def state(tmp_path, monkeypatch):
    """Point all persisted-state paths at a throwaway tmp dir for one test."""
    paths = SimpleNamespace(
        dir=tmp_path,
        tokens=tmp_path / "tokens.json",
        cursors=tmp_path / "sync_cursors.json",
        csv=tmp_path / "transactions.csv",
        raw=tmp_path / "transactions_raw.jsonl.xz",
    )
    monkeypatch.setattr(plaid_client, "TOKENS_FILE", paths.tokens)
    monkeypatch.setattr(plaid_client, "CURSORS_FILE", paths.cursors)
    monkeypatch.setattr(plaid_client, "CSV_FILE", paths.csv)
    monkeypatch.setattr(plaid_client, "RAW_FILE", paths.raw)
    # fetch_transactions imported CSV_FILE / RAW_FILE into its own namespace.
    monkeypatch.setattr(fetch_transactions, "CSV_FILE", paths.csv)
    monkeypatch.setattr(fetch_transactions, "RAW_FILE", paths.raw)
    return paths


@pytest.fixture
def make_txn():
    """Factory for a realistic raw transaction dict (as Plaid's to_dict() emits)."""
    def _make(**over):
        txn = {
            "transaction_id": "txn_1",
            "account_id": "acc_1",
            "pending": False,
            "date": "2026-01-15",
            "authorized_date": "2026-01-14",
            "name": "TRADER JOES #123",
            "merchant_name": "Trader Joe's",
            "merchant_entity_id": "ent_tj",
            "amount": 42.5,
            "iso_currency_code": "USD",
            "payment_channel": "in store",
            "personal_finance_category": {
                "primary": "FOOD_AND_DRINK",
                "detailed": "FOOD_AND_DRINK_GROCERIES",
                "confidence_level": "VERY_HIGH",
            },
            "personal_finance_category_icon_url": "https://example/icon.png",
            "location": {"city": "Seattle", "region": "WA", "lat": 47.6, "lon": -122.3},
            "payment_meta": {"reference_number": "REF1"},
            "counterparties": [
                {
                    "name": "Trader Joe's",
                    "type": "merchant",
                    "entity_id": "ent_tj",
                    "confidence_level": "HIGH",
                }
            ],
        }
        txn.update(over)
        return txn

    return _make


ACCOUNT_META = {
    "acc_1": {
        "institution": "Chase",
        "account_name": "Checking",
        "account_mask": "1234",
        "account_official_name": "Chase Total Checking",
        "account_type": "depository",
        "account_subtype": "checking",
    }
}
