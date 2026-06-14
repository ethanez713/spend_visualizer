"""The Claude audit ritual — a periodic human-driven review by a strong cloud model.

The always-on pipeline is deterministic (mechanical rules + the sign guard); the local
7B LLM reviewer is OFF by default (it was noisy — see ``LLM_ASSESSMENT.md``). This module
lets you periodically hand the categorized store to Claude instead: export the rows Claude
hasn't seen, let Claude judge them, and apply its verdicts as ordinary review FLAGS that
your existing ``--review`` session adjudicates. Claude is a reviewer, never an author —
it only flags; the human stays the highest authority.

  ⚠ EGRESS: running the ritual sends the exported rows (merchant names + amounts) to
  Anthropic. That is the deliberate trade for a much stronger reviewer; the always-on
  pipeline stays fully local.

Tracking: each reviewed row is stamped ``claude_audited_at`` (schema column), so the next
ritual skips it. A settled pending row reappears under a new ``transaction_id`` (so it is
re-reviewed automatically); to force a re-review of everything, export with ``full=True``.

These functions are pure (mutate the in-memory store, no I/O / no network); the thin CLI
wrappers in ``transformer.py`` handle loading, Drive head adoption, persistence, and push.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime, timezone

from .config import INFLOW_PRIMARIES
from .pfc_taxonomy import is_valid, primary_other
from .schema import ensure_new_columns, set_review_flag, stamp_claude_audited

# Source tag written into ``category_review_source`` for a Claude-raised flag, so the
# worklist / review session shows who suggested it (vs. "llm" / "mechanical").
CLAUDE_SOURCE = "claude"

# Compact per-row view hand to Claude: the identity + amount + current category it needs to
# judge a row, and nothing else (smaller payload = less egress, fewer tokens).
_EXPORT_FIELDS = ("transaction_id", "date", "merchant_name", "name",
                  "original_description", "website", "amount", "payment_channel")


def rows_for_claude(store: dict[str, dict], *, full: bool = False) -> list[tuple[str, dict]]:
    """The ``(transaction_id, record)`` rows the next ritual should review.

    By default: posted rows Claude has not stamped yet (``claude_audited_at`` empty).
    Pending rows are skipped — they are transient and reappear posted under a new id.
    ``full=True`` returns every posted row (re-review everything, e.g. after a taxonomy or
    rules change).
    """
    out = []
    for tid, rec in store.items():
        if rec.get("pending"):
            continue
        if full or not rec.get("claude_audited_at"):
            out.append((tid, rec))
    return out


def _export_row(tid: str, rec: dict) -> dict:
    pfc = rec.get("personal_finance_category") or {}
    cps = rec.get("counterparties") or []
    return {
        **{f: rec.get(f) for f in _EXPORT_FIELDS},
        "counterparties": [c.get("name") for c in cps if c.get("name")],
        "current_primary": pfc.get("primary"),
        "current_detailed": pfc.get("detailed"),
        "current_confidence": pfc.get("confidence_level"),
    }


def export_queue(store: dict[str, dict], path: str, *, full: bool = False) -> int:
    """Write the review queue (one compact JSON row per line) to ``path``. Returns the count."""
    rows = rows_for_claude(store, full=full)
    with open(path, "w", encoding="utf-8") as f:
        for tid, rec in rows:
            f.write(json.dumps(_export_row(tid, rec), ensure_ascii=False) + "\n")
    return len(rows)


def load_verdicts(path: str) -> list[dict]:
    """Parse a Claude verdicts JSONL file (blank lines tolerated)."""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def apply_verdicts(store: dict[str, dict], verdicts: list[dict]) -> dict:
    """Apply Claude's verdicts to ``store`` in place. Returns a summary dict.

    Each verdict is ``{"transaction_id", "verdict": "flag"|"ok", ["primary","detailed",
    "reason"]}``. A ``flag`` raises an ordinary review flag (``source="claude"``) the human
    adjudicates later; an ``ok`` just records that Claude looked. Every recognised row is
    stamped ``claude_audited_at`` so the next ritual skips it.

    A flag whose ``(primary, detailed)`` is not a valid taxonomy pair is salvaged by snapping
    a valid primary to its OTHER bucket (mirrors the LLM path); an unknown primary is left
    UNSTAMPED in ``invalid`` so it can be corrected and retried. A verdict for a row not in
    the store is reported in ``unknown`` (also unstamped).
    """
    summary = {"flagged": 0, "ok": 0, "invalid": [], "unknown": [], "log": []}
    for v in verdicts:
        tid = v.get("transaction_id")
        rec = store.get(tid)
        if rec is None:
            summary["unknown"].append(tid)
            continue
        verdict = str(v.get("verdict") or "").lower()

        if verdict == "flag":
            primary, detailed = v.get("primary"), v.get("detailed")
            if not is_valid(primary, detailed):
                other = primary_other(primary)
                if other is None:            # unknown primary → leave it for correction
                    summary["invalid"].append(tid)
                    continue
                detailed = other
            ensure_new_columns(rec)
            # set_review_flag returns False when the suggestion equals the current
            # category — that's not a disagreement, so it's really an "ok", not a flag.
            if set_review_flag(rec, primary, detailed, v.get("reason") or "",
                               "", CLAUDE_SOURCE):
                summary["flagged"] += 1
                summary["log"].append({"transaction_id": tid, "verdict": "flag",
                                       "primary": primary, "detailed": detailed})
            else:
                summary["ok"] += 1
                summary["log"].append({"transaction_id": tid, "verdict": "ok"})
        else:                                # "ok" / "skip" / anything else → just reviewed
            ensure_new_columns(rec)
            summary["ok"] += 1
            summary["log"].append({"transaction_id": tid, "verdict": "ok"})

        stamp_claude_audited(rec)
    return summary


# ── Deterministic pre-scan (the "extra sweeps", computed in one pass) ──────────
# Mechanical, store-wide checks Claude shouldn't eyeball 500 rows for. Bundled with the
# review queue so the ritual is a SINGLE judgment pass: Claude reads the rows + these
# findings together. Each finding is a candidate the human/Claude confirms — never an
# automatic mutation.

def _date_key(rec: dict) -> str:
    return str(rec.get("date") or "")


def _memory_conflicts(store: dict[str, dict], memory) -> list[dict]:
    """Rows whose stored category disagrees with what merchant memory would assign — a
    stale/wrong taught entry, or a later correction that diverged from memory."""
    out = []
    for tid, rec in store.items():
        hit = memory.lookup(rec)
        if hit is None:
            continue
        pfc = rec.get("personal_finance_category") or {}
        if (hit.primary, hit.detailed) != (pfc.get("primary"), pfc.get("detailed")):
            out.append({"transaction_id": tid, "rule": hit.rule_name,
                        "memory_says": f"{hit.primary}/{hit.detailed}",
                        "stored": f"{pfc.get('primary')}/{pfc.get('detailed')}"})
    return out


def audit_scan(store: dict[str, dict], memory=None, *,
               stale_pending_days: int = 14, outlier_n: int = 10) -> dict:
    """Run the deterministic store-wide sweeps in ONE pass. Returns a findings dict.

    Sweeps (all candidates for review, never auto-applied):
      * ``taxonomy_invalid``  — category not valid in the current vendored PFC taxonomy
        (legacy values like INCOME_SALARY / OTHER_OTHER / *_FROM_APPS).
      * ``sign_violations``   — a positive (outgoing) amount stored under an INFLOW primary
        (INCOME / TRANSFER_IN) — impossible; predates the guard or came from a manual edit.
      * ``entity_inconsistent`` — one ``merchant_entity_id`` carrying >1 distinct category.
      * ``uncategorized``     — rows with no primary at all.
      * ``amount_outliers``   — the ``outlier_n`` largest rows by |amount| (sanity glance).
      * ``stale_pending``     — pending rows older than ``stale_pending_days`` (never settled).
      * ``memory_conflicts``  — stored category disagrees with merchant memory (if given).
    """
    findings: dict = {"taxonomy_invalid": [], "sign_violations": [], "entity_inconsistent": [],
                      "uncategorized": [], "amount_outliers": [], "stale_pending": [],
                      "memory_conflicts": []}
    by_entity: dict[str, set] = defaultdict(set)
    amounts: list[tuple[float, str]] = []
    today = datetime.now(timezone.utc).date()

    for tid, rec in store.items():
        pfc = rec.get("personal_finance_category") or {}
        p, d = pfc.get("primary"), pfc.get("detailed")
        amt = rec.get("amount")

        if not p:
            findings["uncategorized"].append(tid)
        else:
            if not is_valid(p, d):
                findings["taxonomy_invalid"].append({"transaction_id": tid,
                                                     "category": f"{p}/{d}"})
            if isinstance(amt, (int, float)) and amt > 0 and p in INFLOW_PRIMARIES:
                findings["sign_violations"].append({"transaction_id": tid, "amount": amt,
                                                    "category": f"{p}/{d}"})
            eid = rec.get("merchant_entity_id")
            if eid:
                by_entity[eid].add((p, d))

        if isinstance(amt, (int, float)):
            amounts.append((abs(amt), tid))

        if rec.get("pending"):
            try:
                age = (today - datetime.fromisoformat(_date_key(rec)).date()).days
            except ValueError:
                age = 0
            if age > stale_pending_days:
                findings["stale_pending"].append(tid)

    for eid, cats in by_entity.items():
        if len(cats) > 1:
            findings["entity_inconsistent"].append(
                {"merchant_entity_id": eid,
                 "categories": sorted(f"{p}/{d}" for p, d in cats)})

    amounts.sort(reverse=True)
    findings["amount_outliers"] = [{"transaction_id": tid, "abs_amount": a}
                                   for a, tid in amounts[:outlier_n]]
    if memory is not None:
        findings["memory_conflicts"] = _memory_conflicts(store, memory)
    return findings


def export_bundle(store: dict[str, dict], queue_path: str, scan_path: str, *,
                  memory=None, full: bool = False) -> tuple[int, dict]:
    """Write the review queue (JSONL) and the deterministic scan (JSON) for one ritual.

    Returns ``(queue_count, scan_findings)``. The two artifacts are read together in a
    single Claude judgment pass (see the audit-transactions skill).
    """
    n = export_queue(store, queue_path, full=full)
    scan = audit_scan(store, memory)
    with open(scan_path, "w", encoding="utf-8") as f:
        json.dump({"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "store_rows": len(store), "findings": scan}, f, ensure_ascii=False, indent=2)
    return n, scan
