"""Vendored Plaid Personal Finance Category (PFC) taxonomy — offline-first.

Source: Plaid's published taxonomy CSV
``https://plaid.com/documents/transactions-personal-finance-category-taxonomy.csv``
obtained ONCE and committed alongside this module as ``pfc_taxonomy.csv`` (so the
transformer never fetches at runtime — the core path works with zero network).

Parsed at import into three structures used by the rules engine and the LLM prompt:
  * ``PRIMARY``  — list of the 16 primary categories (CSV order).
  * ``DETAILED`` — ``{primary: [detailed, ...]}`` (the only valid detailed values
    per primary; the LLM/rules must pick a detailed that belongs to its primary).
  * ``GLOSS``    — ``{detailed: one-line description}`` injected into the LLM system
    prompt so the model knows what each category means.

To refresh the taxonomy: re-download the CSV over the committed file and re-run the
tests (``test_taxonomy`` asserts the 16-primary invariant and the ``PRIMARY_*`` naming).
"""
from __future__ import annotations

import csv
import os

_CSV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pfc_taxonomy.csv")

# The 16 PFC primaries, per Plaid's docs. Used to validate the vendored CSV at import
# (a silently truncated / re-shaped download should fail loudly, not skew categories).
EXPECTED_PRIMARY = {
    "INCOME", "TRANSFER_IN", "TRANSFER_OUT", "LOAN_PAYMENTS", "BANK_FEES",
    "ENTERTAINMENT", "FOOD_AND_DRINK", "GENERAL_MERCHANDISE", "HOME_IMPROVEMENT",
    "MEDICAL", "PERSONAL_CARE", "GENERAL_SERVICES", "GOVERNMENT_AND_NON_PROFIT",
    "TRANSPORTATION", "TRAVEL", "RENT_AND_UTILITIES",
}


def _load():
    primary: list[str] = []
    detailed: dict[str, list[str]] = {}
    gloss: dict[str, str] = {}
    with open(_CSV_PATH, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            p = (row.get("PRIMARY") or "").strip()
            d = (row.get("DETAILED") or "").strip()
            g = (row.get("DESCRIPTION") or "").strip()
            if not p or not d:
                continue
            if p not in detailed:
                detailed[p] = []
                primary.append(p)
            detailed[p].append(d)
            gloss[d] = g
    if not primary:
        raise RuntimeError(f"PFC taxonomy CSV is empty or malformed: {_CSV_PATH}")
    if set(primary) != EXPECTED_PRIMARY:
        raise RuntimeError(
            "Vendored PFC taxonomy primaries do not match Plaid's known 16 — "
            f"got {sorted(primary)}; refusing to load a skewed taxonomy."
        )
    return primary, detailed, gloss


PRIMARY, DETAILED, GLOSS = _load()

# Flat set of every valid detailed value (across all primaries) for quick membership.
ALL_DETAILED = {d for ds in DETAILED.values() for d in ds}


def is_valid(primary: str, detailed: str) -> bool:
    """True iff ``primary`` is a PFC primary and ``detailed`` belongs to that primary."""
    return primary in DETAILED and detailed in DETAILED[primary]


def primary_other(primary: str) -> str | None:
    """The catch-all 'OTHER' detailed for a primary (every primary has exactly one).

    Used to salvage an LLM decision whose primary is valid but whose detailed isn't in
    that primary (a small model picking a real category but the wrong leaf): snapping to
    the primary's OTHER bucket keeps the (correct) primary instead of dropping the row.
    """
    if primary not in DETAILED:
        return None
    for d in DETAILED[primary]:
        if "_OTHER_" in d or d.endswith("_OTHER"):
            return d
    return DETAILED[primary][-1]


def taxonomy_block() -> str:
    """Render the full taxonomy as text for injection into the LLM system prompt.

    One section per primary, each detailed value with its one-line gloss — the model's
    complete menu of legal ``(primary, detailed)`` choices.
    """
    lines: list[str] = []
    for p in PRIMARY:
        lines.append(f"{p}:")
        for d in DETAILED[p]:
            lines.append(f"  - {d}: {GLOSS.get(d, '')}")
    return "\n".join(lines)
