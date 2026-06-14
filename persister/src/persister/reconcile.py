"""Reconcile a local store against a remote (Drive) store.

Classifies every key into in_sync / local_only / remote_only / conflict and builds a
``merged`` union. The guiding policy is **preserve as much as possible** — durable
history that has aged out of Plaid's window lives only in the remote and must NEVER be
deleted. Conflicts keep the remote value until a golden Plaid re-fetch overwrites them
via :func:`persister.merge.merge_golden`.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


def _content_hash(record: dict, metadata_fields: tuple = ()) -> str:
    """Stable content hash of a record (canonical JSON, key order independent).

    ``metadata_fields`` are stripped before hashing: locally-stamped bookkeeping
    (e.g. an owner tag) must not register as a content difference against a remote
    written before the field existed.
    """
    if metadata_fields:
        record = {k: v for k, v in record.items() if k not in metadata_fields}
    blob = json.dumps(record, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class ReconcileReport:
    in_sync: list[str]      # keys identical in both
    local_only: list[str]   # keys only local  → keep (new data to push)
    remote_only: list[str]  # keys only remote → keep (history aged out of Plaid; NEVER delete)
    conflicts: list[str]    # keys in both but content differs → Plaid golden → re-fetch
    merged: dict[str, dict] # union; for conflicts, remote value retained pending re-fetch


def reconcile(local: dict[str, dict], remote: dict[str, dict],
              key_field: str = "transaction_id",
              *, metadata_fields: tuple = (),
              conflict_resolver=None) -> ReconcileReport:
    """Classify keys by membership + content hash and build the preserved union.

    - in both & equal  → in_sync   (keep either; we keep local)
    - in both & differ → conflict  (resolved below)
    - local only       → keep in merged (new data, push it)
    - remote only      → keep in merged (durable history beyond Plaid's window)

    ``key_field`` is accepted for API symmetry; both inputs are already keyed dicts.
    Lists are sorted for deterministic, diff-friendly reports.

    ``metadata_fields`` names caller-owned bookkeeping fields (e.g. an owner tag)
    excluded from conflict detection. Records equal modulo those fields are in_sync,
    and merged keeps the LOCAL copy — the side that carries the stamps.

    ``conflict_resolver(local_rec, remote_rec) -> dict`` lets the caller pick the
    winning record per conflict (domain logic — e.g. a newest-audit-stamp rule —
    stays in consumers). Default: keep the remote value (pending a golden
    re-fetch, the raw-store policy). The resolver must return one of its two
    arguments; the conflict is still reported either way.
    """
    in_sync: list[str] = []
    local_only: list[str] = []
    remote_only: list[str] = []
    conflicts: list[str] = []
    merged: dict[str, dict] = {}

    for key in sorted(set(local) | set(remote)):
        in_local = key in local
        in_remote = key in remote
        if in_local and in_remote:
            if (_content_hash(local[key], metadata_fields)
                    == _content_hash(remote[key], metadata_fields)):
                in_sync.append(key)
                merged[key] = local[key]
            else:
                conflicts.append(key)
                merged[key] = (conflict_resolver(local[key], remote[key])
                               if conflict_resolver is not None else remote[key])
        elif in_local:
            local_only.append(key)
            merged[key] = local[key]
        else:
            remote_only.append(key)
            merged[key] = remote[key]

    return ReconcileReport(in_sync, local_only, remote_only, conflicts, merged)
