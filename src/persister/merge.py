"""Merge freshly-fetched golden records into an existing store.

Plaid is the system of record **only when it returns data**: each fresh record
OVERWRITES the matching key. Records present in ``base`` but absent from ``fresh`` are
KEPT — they are simply outside the fetched window (durable history), never deleted.
"""
from __future__ import annotations

import sys

from .store import dedupe_supersede


def merge_golden(base: dict[str, dict], fresh: list[dict],
                 key_field: str = "transaction_id") -> dict[str, dict]:
    """Overwrite ``base`` by key with each ``fresh`` record, preserving base-only keys.

    After merging, :func:`persister.store.dedupe_supersede` drops settled pendings.
    A fresh record missing its key field is skipped (can't be placed) rather than crashing.
    """
    merged = dict(base)
    for rec in fresh:
        key = rec.get(key_field)
        if key is None:
            print(f"  merge: skipping fresh record missing key field {key_field!r}",
                  file=sys.stderr)
            continue
        merged[key] = rec  # Plaid golden — overwrite
    return dedupe_supersede(merged)
