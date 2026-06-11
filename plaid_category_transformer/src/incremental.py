"""Incremental delta: audit only NEW / CHANGED Plaid rows; prune ones gone upstream.

Re-auditing the entire Plaid history every run is wasteful — the audit makes a local LLM
call per row — and, worse, it would re-flag rows a human already adjudicated. This module
diffs the current **input** (Plaid's truth, ``../transactions/data/transactions.jsonl``)
against the **prior categorized store** so the transformer:

  * runs the (expensive) audit ONLY on rows whose raw Plaid content is new or has changed;
  * carries already-audited rows forward untouched (preserving corrections + pending flags);
  * identifies rows that vanished upstream — a **hard delete**, or a **pending row that
    settled** (Plaid drops the pending id and adds a new posted one) — so the caller can
    prune them from BOTH the local committed file and the Drive copy.

Change detection is a stable content hash of the *raw Plaid fields only* (our own
provenance/review/bookkeeping columns excluded), stamped onto each record as
``source_content_hash`` when it is audited. A differing hash ⇒ Plaid changed the row ⇒
re-audit. The hash is taken over the PRISTINE input, which always carries Plaid's original
category — our in-place correction lives only in the output store — so an unchanged input
keeps matching its stored hash run after run.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

from .schema import NEW_COLUMNS

# Bookkeeping field stamped on every audited record: the hash of the input it came from.
SOURCE_HASH_FIELD = "source_content_hash"

# Sentinel stamped INSTEAD of the real hash when the LLM stage was requested but did not
# actually run (Ollama down / crashed mid-run). It never equals a real hash, so ``classify``
# re-processes the row next run — an implicit LLM outage must not silently mark rows as
# fully audited forever. (An explicit ``--no-llm`` run stamps real hashes: rules-only was
# the user's deliberate choice there.)
HASH_PENDING_LLM = "pending:llm-stage-skipped"

# Sentinel stamped when a row's manual-edit intent was REVOKED: the override is rolled
# back to the pre-manual category, and the row must go through the full audit again next
# run (the manual stage runs after the audit, so it cannot re-run Stages 1–2 itself).
HASH_PENDING_REVOKED = "pending:manual-edit-revoked"

# Fields that are OURS, not Plaid's — excluded from the content hash so our own edits (a
# correction, a review flag, the hash itself) never masquerade as an upstream change.
_NON_SOURCE_FIELDS = frozenset(NEW_COLUMNS) | {SOURCE_HASH_FIELD}


def source_hash(record: dict) -> str:
    """Stable SHA-256 of a record's raw Plaid content (our own columns excluded)."""
    payload = {k: v for k, v in record.items() if k not in _NON_SOURCE_FIELDS}
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass
class Delta:
    """The result of diffing the input store against the prior categorized store.

    ``to_process`` — pristine input rows to run the audit on (NEW + CHANGED).
    ``carryover``  — already-audited rows kept verbatim from the prior store (UNCHANGED);
                     their pending review flags and corrections are preserved.
    ``removed``    — keys present in the prior store but gone from the input (hard-deleted,
                     or a pending row that has since settled). The caller drops these.
    ``new`` / ``changed`` — id lists for reporting (``to_process`` is their union).
    """
    to_process: dict[str, dict]
    carryover: dict[str, dict]
    removed: list[str]
    new: list[str] = field(default_factory=list)
    changed: list[str] = field(default_factory=list)


def classify(input_store: dict[str, dict], prior_store: dict[str, dict],
             *, full: bool = False) -> Delta:
    """Diff ``input_store`` (Plaid truth) against ``prior_store`` (last categorized output).

    Each input row is NEW (no prior), CHANGED (prior hash differs), or UNCHANGED (hash
    matches → carried over). A prior row absent from the input is REMOVED. ``full=True``
    forces every input row through the audit (re-categorize everything, e.g. after editing
    the rules) while still computing removals.

    A prior row that predates content hashing (no ``source_content_hash``) is adopted as the
    baseline *without* re-auditing — its current input content becomes its stamped hash — so
    enabling incremental mode never triggers a disruptive full re-audit and never discards
    human review decisions already recorded in the prior store.
    """
    to_process: dict[str, dict] = {}
    carryover: dict[str, dict] = {}
    new: list[str] = []
    changed: list[str] = []

    for tid, rec in input_store.items():
        prev = prior_store.get(tid)
        if full or prev is None:
            to_process[tid] = rec
            (changed if prev is not None else new).append(tid)
            continue

        h = source_hash(rec)
        prev_h = prev.get(SOURCE_HASH_FIELD)
        if prev_h is None:
            # Prior store predates hashing → adopt as baseline, don't re-audit.
            carried = dict(prev)
            carried[SOURCE_HASH_FIELD] = h
            carryover[tid] = carried
        elif prev_h != h:
            to_process[tid] = rec
            changed.append(tid)
        else:
            carryover[tid] = prev

    removed = [tid for tid in prior_store if tid not in input_store]
    return Delta(to_process, carryover, removed, new, changed)
