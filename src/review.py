"""Interactive review of flagged rows — a human adjudicates the LLM's suggestions.

The audit pipeline (``transformer``) never lets the noisy local LLM silently overwrite a
category; instead it writes ``category_review_*`` columns flagging rows where the LLM (or a
loose mechanical rule) disagreed. This module walks those flags so a human decides:

  [a]ccept  — apply the suggested category (records provenance) and TEACH merchant memory,
              so the same merchant is a trusted ``trust="auto"`` mechanical hit next run.
  [r]eject  — keep the current category; just drop the flag.
  [e]dit    — pick a different valid category by hand (also taught to memory).
  [s]kip    — leave the flag in place to revisit later.
  [q]uit    — stop reviewing (remaining flags are left untouched).

The per-row actions (``accept_flag`` / ``reject_flag`` / ``repick_flag``) are pure and
unit-tested; ``run_review`` is the thin interactive loop around them (line-based, not
curses — simpler and testable; it no-ops off a TTY). Decisions append to a 0600 review log.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime

from .pfc_taxonomy import DETAILED, PRIMARY, is_valid
from .rules import MerchantMemory
from .schema import clear_review_flag, set_provenance

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_REVIEW_LOG = os.path.join(_PROJECT_ROOT, ".secrets", "review_log.jsonl")


# ── Pure per-row actions ──────────────────────────────────────────────────────

def flagged_rows(store: dict[str, dict]) -> list[tuple[str, dict]]:
    """The ``(transaction_id, record)`` pairs carrying a pending review flag."""
    return [(tid, rec) for tid, rec in store.items() if rec.get("category_review_flag")]


def accept_flag(record: dict, memory: MerchantMemory | None = None) -> bool:
    """Apply the row's pending suggestion as a correction; teach memory. Returns applied?."""
    primary = record.get("category_review_primary")
    detailed = record.get("category_review_detailed")
    reason = record.get("category_review_reason") or ""
    conf = record.get("category_review_confidence") or ""
    source = record.get("category_review_source") or "llm"
    applied = set_provenance(record, primary, detailed, "review",
                             f"accepted {source}: {reason}".strip(), conf)
    clear_review_flag(record)
    if applied and memory is not None:
        memory.remember(record, primary, detailed)
    return applied


def reject_flag(record: dict) -> None:
    """Keep the current category; drop the flag."""
    clear_review_flag(record)


def repick_flag(record: dict, primary: str, detailed: str,
                memory: MerchantMemory | None = None) -> bool:
    """Apply a hand-picked category; teach memory. Raises on an invalid taxonomy pair."""
    if not is_valid(primary, detailed):
        raise ValueError(f"{primary}/{detailed} is not a valid PFC category")
    applied = set_provenance(record, primary, detailed, "review", "manual re-pick", "HIGH")
    clear_review_flag(record)
    if applied and memory is not None:
        memory.remember(record, primary, detailed)
    return applied


# ── Review logging ────────────────────────────────────────────────────────────

def write_review_log(path: str, entries: list[dict]) -> None:
    """Append one JSONL line per adjudicated row (0600 — financial data)."""
    if not entries:
        return
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, mode=0o700, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps(e) + "\n")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


# ── Interactive loop ──────────────────────────────────────────────────────────

def _render(rec: dict, n: int, total: int, out) -> None:
    pfc = rec.get("personal_finance_category") or {}
    out(f"\n── Flag {n}/{total} " + "─" * 40)
    out(f"  merchant_name : {rec.get('merchant_name')}")
    out(f"  name          : {rec.get('name')}")
    out(f"  original_desc : {rec.get('original_description')}")
    out(f"  website       : {rec.get('website')}")
    out(f"  amount        : {rec.get('amount')}")
    out(f"  current       : {pfc.get('primary')} / {pfc.get('detailed')} "
        f"({pfc.get('confidence_level')})")
    out(f"  SUGGESTED     : {rec.get('category_review_primary')} / "
        f"{rec.get('category_review_detailed')}  "
        f"[{rec.get('category_review_source')}, conf={rec.get('category_review_confidence')}]")
    out(f"  reason        : {rec.get('category_review_reason')}")


def prompt_pick_category(input_fn, out) -> tuple[str, str] | None:
    """Prompt for a valid (primary, detailed); None if the user backs out.

    Public: the manual --edit session reuses this picker.
    """
    for i, p in enumerate(PRIMARY):
        out(f"    {i:2}. {p}")
    raw = input_fn("  primary # (or blank to cancel): ").strip()
    if not raw.isdigit() or not (0 <= int(raw) < len(PRIMARY)):
        return None
    primary = PRIMARY[int(raw)]
    details = DETAILED[primary]
    for i, d in enumerate(details):
        out(f"    {i:2}. {d}")
    raw = input_fn("  detailed # (or blank to cancel): ").strip()
    if not raw.isdigit() or not (0 <= int(raw) < len(details)):
        return None
    return primary, details[int(raw)]


def run_review(store: dict[str, dict], memory: MerchantMemory | None = None, *,
               input_fn=input, out=print) -> dict:
    """Walk flagged rows interactively. Returns a summary dict of counts + a log list.

    Mutates ``store`` in place (accepted/re-picked rows get provenance; flags are cleared).
    No-ops with a notice when there are no flags or stdin isn't a TTY (so a piped run can't
    block). ``input_fn``/``out`` are injectable for tests.
    """
    flags = flagged_rows(store)
    summary = {"accepted": 0, "rejected": 0, "repicked": 0, "skipped": 0, "log": []}
    if not flags:
        out("No flagged rows to review.")
        return summary
    if not sys.stdin.isatty():
        out(f"{len(flags)} flagged row(s), but stdin is not a TTY — skipping interactive "
            "review. Run in a terminal to adjudicate.")
        summary["skipped"] = len(flags)
        return summary

    out(f"Reviewing {len(flags)} flagged row(s). [a]ccept / [r]eject / [e]dit / [s]kip / [q]uit")
    for n, (tid, rec) in enumerate(flags, 1):
        _render(rec, n, len(flags), out)
        choice = input_fn("  > ").strip().lower()[:1]
        action = None
        if choice == "a":
            sp, sd = rec.get("category_review_primary"), rec.get("category_review_detailed")
            accept_flag(rec, memory)
            action = "accepted"
        elif choice == "e":
            picked = prompt_pick_category(input_fn, out)
            if picked is None:
                out("  (cancelled — left flagged)")
                summary["skipped"] += 1
                continue
            sp, sd = picked
            repick_flag(rec, sp, sd, memory)
            action = "repicked"
        elif choice == "r":
            sp = sd = None
            reject_flag(rec)
            action = "rejected"
        elif choice == "q":
            out("  Stopping; remaining flags left in place.")
            summary["skipped"] += len(flags) - n + 1
            break
        else:  # skip / unrecognized
            summary["skipped"] += 1
            continue

        summary[action] += 1
        summary["log"].append({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "transaction_id": tid, "action": action,
            "applied_primary": sp, "applied_detailed": sd,
        })

    out(f"\nDone: {summary['accepted']} accepted, {summary['repicked']} re-picked, "
        f"{summary['rejected']} rejected, {summary['skipped']} skipped.")
    return summary
