"""Append-only reconcile audit log.

A cheap, human-readable trail that complements git + Drive revision history: one JSONL
line per reconcile run with counts, the conflicting keys, and a UTC timestamp. Lives in
``.secrets/reconcile_log.jsonl`` (0600) so it is gitignored and owner-only.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from .reconcile import ReconcileReport


def log_reconcile(path: str, report: ReconcileReport, *, source: str) -> None:
    """Append one JSONL line summarising a reconcile run.

    ``source`` records where the run came from (e.g. ``"cli"``, ``"persist_runner"``).
    Creates the parent dir and forces ``0600`` perms (the log mirrors transaction ids).
    Never raises on a logging failure — auditing must not break the data path.
    """
    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "in_sync": len(report.in_sync),
        "local_only": len(report.local_only),
        "remote_only": len(report.remote_only),
        "conflicts": len(report.conflicts),
        "conflict_keys": report.conflicts,
    }
    try:
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, mode=0o700, exist_ok=True)
        # Create owner-only if new; append the line.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False))
            f.write("\n")
        os.chmod(path, 0o600)  # re-assert in case the file pre-existed with looser perms
    except OSError as e:
        print(f"  audit: failed to write reconcile log: {e}")
