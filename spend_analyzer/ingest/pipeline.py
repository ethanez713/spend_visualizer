"""Orchestrates the INGEST stage: load -> dedupe -> drop-settled -> normalize.

Returns CanonicalTransaction[] plus QC counts. No taxonomy, no enrichment —
those are ANALYZE concerns. This is the extractable seam (PLAN.md §2).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Iterable

from models import CanonicalTransaction
from .load import load_sources, stat_source, SourceStat
from .dedupe import dedupe_by_id, drop_settled_pending
from .normalize import normalize


@dataclass
class IngestResult:
    transactions: list[CanonicalTransaction]
    sources: list[SourceStat] = field(default_factory=list)
    qc: dict = field(default_factory=dict)

    @property
    def cache_key(self) -> tuple:
        return tuple(s.cache_key for s in self.sources)


def ingest(paths: Iterable[str],
           overlay: Callable[[list[dict]], None] | None = None) -> IngestResult:
    """`overlay`, when given, mutates the surviving raw records before they are
    normalized — the hook the analyzer uses to replay manual-edit intents the
    categorized store has not caught up with yet."""
    paths = list(paths)
    sources = [stat_source(p) for p in paths]
    raw = load_sources(paths)
    n_loaded = len(raw)

    deduped, n_dupes = dedupe_by_id(raw)
    kept, n_settled = drop_settled_pending(deduped)
    if overlay is not None:
        overlay(kept)
    txns = normalize(kept)

    n_pending = sum(1 for t in txns if t.pending)
    qc = {
        "n_loaded": n_loaded,
        "n_duplicate_ids_dropped": n_dupes,
        "n_settled_pending_dropped": n_settled,
        "n_transactions": len(txns),
        "n_pending_remaining": n_pending,
    }
    return IngestResult(transactions=txns, sources=sources, qc=qc)
