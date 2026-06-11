"""Stage op 2 & 3: dedupe by transaction_id; resolve pending vs posted.

Even though the collector already reconciles added/modified/removed, we
re-assert uniqueness here — never trust a single source (PLAN.md §4.2).
"""
from __future__ import annotations


def dedupe_by_id(raw_rows: list[dict]) -> tuple[list[dict], int]:
    """Keep the last occurrence of each transaction_id (latest wins).

    Returns (deduped_rows, num_dropped).
    """
    by_id: dict[str, dict] = {}
    for r in raw_rows:
        tid = r.get("transaction_id")
        if tid:
            by_id[tid] = r
    deduped = list(by_id.values())
    return deduped, len(raw_rows) - len(deduped)


def drop_settled_pending(raw_rows: list[dict]) -> tuple[list[dict], int]:
    """Drop pending rows that already have a posted counterpart.

    A posted row references its prior pending row via ``pending_transaction_id``.
    We drop pending rows whose id is referenced by some posted row (PLAN.md §4.3).
    Remaining pending rows are kept (flagged) for an optional 'recent' strip.
    Returns (rows, num_dropped).
    """
    superseded = {
        r.get("pending_transaction_id")
        for r in raw_rows
        if not r.get("pending") and r.get("pending_transaction_id")
    }
    superseded.discard(None)
    kept = [
        r
        for r in raw_rows
        if not (r.get("pending") and r.get("transaction_id") in superseded)
    ]
    return kept, len(raw_rows) - len(kept)
