"""Stage op 4 (partial): raw Plaid object -> CanonicalTransaction.

Sets the signed amount and provisional flow *direction*. The final ``flow``
(spend/income/excluded) is decided downstream by the taxonomy resolver, which
owns the exclusion list (transfers / CC payments). Keeping exclusions out of
normalize keeps INGEST independent of the taxonomy config.
"""
from __future__ import annotations

from typing import Optional

from models import CanonicalTransaction


def _g(d: Optional[dict], *keys, default=None):
    cur = d or {}
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def normalize_one(raw: dict) -> CanonicalTransaction:
    amount = float(raw.get("amount") or 0.0)
    pfc = raw.get("personal_finance_category") or {}
    tx = CanonicalTransaction(
        transaction_id=raw["transaction_id"],
        account_id=raw.get("account_id"),
        institution=raw.get("institution"),
        owner=raw.get("txn_owner"),
        posted_date=raw.get("date"),
        authorized_date=raw.get("authorized_date"),
        amount=amount,
        currency=raw.get("iso_currency_code") or raw.get("unofficial_currency_code"),
        name=raw.get("name"),
        merchant_name=raw.get("merchant_name"),
        merchant_entity_id=raw.get("merchant_entity_id"),
        pfc_primary=pfc.get("primary"),
        pfc_detailed=pfc.get("detailed"),
        pfc_confidence=pfc.get("confidence_level"),
        counterparties=raw.get("counterparties") or [],
        location=raw.get("location") or {},
        payment_channel=raw.get("payment_channel"),
        website=raw.get("website"),
        logo_url=raw.get("logo_url"),
        pending=bool(raw.get("pending")),
        pending_transaction_id=raw.get("pending_transaction_id"),
        direction="in" if amount < 0 else "out",
        dedupe_key=raw["transaction_id"],
        raw=raw,
    )
    return tx


def normalize(raw_rows: list[dict]) -> list[CanonicalTransaction]:
    return [normalize_one(r) for r in raw_rows]
