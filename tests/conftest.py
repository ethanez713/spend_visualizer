"""Shared offline fixtures — deterministic, tmp-isolated, no network, no real .secrets/.

Mirrors the ``state``/``make_txn`` fixture style in transactions/tests/conftest.py.
The LLM is never invoked here: tests inject a ``FakeLLM`` so unit runs stay fast and
offline. File I/O (memory, logs, output) is redirected to ``tmp_path``.
"""
from __future__ import annotations

import pytest

from src.llm import CategoryDecision


@pytest.fixture
def make_record():
    """Factory for a realistic raw Plaid transaction dict (as ``to_dict()`` emits)."""
    def _make(**over):
        rec = {
            "transaction_id": "txn_1",
            "account_id": "acc_1",
            "pending": False,
            "pending_transaction_id": None,
            "date": "2026-01-15",
            "authorized_date": "2026-01-14",
            "name": "SQ *BLUE BOTTLE",
            "original_description": "SQ *BLUE BOTTLE COFFEE",
            "merchant_name": "Blue Bottle Coffee",
            "merchant_entity_id": "ent_bluebottle",
            "website": "bluebottlecoffee.com",
            "amount": 6.5,
            "iso_currency_code": "USD",
            "payment_channel": "in store",
            "personal_finance_category": {
                "primary": "GENERAL_MERCHANDISE",
                "detailed": "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE",
                "confidence_level": "LOW",
            },
            "personal_finance_category_icon_url": "https://example/icon.png",
            "location": {"city": "Oakland", "region": "CA", "lat": 37.8, "lon": -122.2},
            "payment_meta": {"reference_number": "REF1"},
            "counterparties": [
                {"name": "Blue Bottle Coffee", "type": "merchant",
                 "entity_id": "ent_bluebottle", "confidence_level": "HIGH"},
            ],
        }
        rec.update(over)
        return rec
    return _make


@pytest.fixture
def store_of(make_record):
    """Build a ``{transaction_id: record}`` store from per-record kwarg dicts."""
    def _build(*records):
        out = {}
        for i, r in enumerate(records, 1):
            rec = make_record(**r) if isinstance(r, dict) else r
            rec.setdefault("transaction_id", f"txn_{i}")
            out[rec["transaction_id"]] = rec
        return out
    return _build


class FakeLLM:
    """Stand-in for ``CategoryLLM``: returns canned decisions keyed by row_index.

    Construct with ``FakeLLM({0: ("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE", "...reason")})``
    or with full ``CategoryDecision`` objects. ``categorize`` records the items it saw.
    """

    def __init__(self, decisions: dict | None = None, confidence: str = "HIGH"):
        self._spec = decisions or {}
        self._confidence = confidence
        self.seen_items: list[dict] | None = None

    def categorize(self, items):
        self.seen_items = items
        out: dict[int, CategoryDecision] = {}
        for idx, val in self._spec.items():
            if isinstance(val, CategoryDecision):
                out[idx] = val
                continue
            primary, detailed, reason = val
            out[idx] = CategoryDecision(
                row_index=idx, primary=primary, detailed=detailed,
                changed=True, confidence=self._confidence, reason=reason,
            )
        return out


@pytest.fixture
def FakeLLM_cls():
    return FakeLLM
