"""Source-agnostic data contracts shared across stages.

The ``CanonicalTransaction`` is the stable seam between INGEST and ANALYZE
(see PLAN.md §2, §4). Keep it source-agnostic: nothing Plaid-specific should
leak past normalization except inside the retained ``raw`` blob.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional


@dataclass
class CanonicalTransaction:
    # identity / source
    transaction_id: str
    account_id: str
    institution: Optional[str] = None
    owner: Optional[str] = None              # who the linked Item belongs to (txn_owner)

    # dates
    posted_date: Optional[str] = None        # ISO yyyy-mm-dd
    authorized_date: Optional[str] = None     # ISO yyyy-mm-dd (often null in data)

    # money (Plaid convention: amount > 0 == money OUT)
    amount: float = 0.0
    currency: Optional[str] = None

    # descriptors
    name: Optional[str] = None
    merchant_name: Optional[str] = None
    merchant_entity_id: Optional[str] = None

    # Plaid personal_finance_category
    pfc_primary: Optional[str] = None
    pfc_detailed: Optional[str] = None
    pfc_confidence: Optional[str] = None

    # rich signals (retained for synthesized categories / facets)
    counterparties: list = field(default_factory=list)
    location: dict = field(default_factory=dict)
    payment_channel: Optional[str] = None
    website: Optional[str] = None
    logo_url: Optional[str] = None

    # account metadata (not in the txn object; filled from accounts.yaml)
    account_name: Optional[str] = None
    account_mask: Optional[str] = None
    account_type: Optional[str] = None
    account_subtype: Optional[str] = None

    # pending handling
    pending: bool = False
    pending_transaction_id: Optional[str] = None

    # ingest-computed
    direction: str = "out"          # "out" (amount>0) | "in" (amount<0)
    dedupe_key: Optional[str] = None

    # the original object, retained losslessly
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
