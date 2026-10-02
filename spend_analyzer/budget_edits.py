"""Budget edits: where the household decides a transaction belongs in the budget, and the
budget rows that are not bank transactions at all (adjustments).

An edit is an intent in the transformer's append-only log (``action: budget_edit``), so it
syncs between machines like every other correction and never changes a record's Plaid
category. ``set`` holds a ``budget_category`` or ``exclude`` (the row is not budget
spending); a note rides along. Edits are matched with the transformer's own
``ManualIndex``: a transaction beats a merchant beats a description phrase, and within a
scope the latest append wins. The category and the note are matched separately, so
annotating a row never undoes the category chosen for it.
"""
from __future__ import annotations

import math
import uuid
from datetime import date, datetime

import pandas as pd

import manual_edits

ACTION = "budget_edit"


def build_edit(raw_record: dict, *, scope: str, category: str | None = None,
               exclude: bool = False, note: str = "", phrase: str | None = None,
               source: str = "ui") -> dict:
    """One edit written from a representative raw record. Raises ValueError on bad input."""
    if category and exclude:
        raise ValueError("a budget edit sets a category or excludes the row, not both")
    note = note.strip()
    if not (category or exclude or note):
        raise ValueError("a budget edit needs a category, an exclusion or a note")
    manual, _ = manual_edits._import()
    chosen = {"budget_category": category} if category else ({"exclude": True} if exclude else {})
    return {"id": uuid.uuid4().hex[:8], "created_at": datetime.now().isoformat(timespec="seconds"),
            "action": ACTION, "source": source, "scope": scope,
            "match": manual.build_match(scope, raw_record, phrase), "set": chosen, "note": note}


def append_edits(edits: list[dict], path: str | None = None) -> int:
    manual, _ = manual_edits._import()
    for edit in edits:
        manual.append_intent(path or manual_edits.edits_path(), edit)
    return len(edits)


def resolve(entries: list[dict]) -> list[dict]:
    """The applicable budget edits in append order: not revoked, with a usable match and
    something to apply."""
    manual, _ = manual_edits._import()
    revoked = {e.get("revokes") for e in entries if e.get("action") == "revoke"}
    out = []
    for entry in entries:
        if entry.get("action") != ACTION or entry.get("id") in revoked:
            continue
        chosen = entry.get("set") or {}
        has_effect = (isinstance(chosen.get("budget_category"), str) and chosen["budget_category"]
                      or chosen.get("exclude") is True or (entry.get("note") or "").strip())
        if has_effect and manual.match_problem(entry.get("scope"), entry.get("match") or {}) is None:
            out.append(entry)
    return out


def current(path: str | None = None) -> list[dict]:
    manual, _ = manual_edits._import()
    return resolve(manual.load_intents(path or manual_edits.edits_path()))


def overrides_for(raw_records: list[dict], path: str | None = None) -> dict[str, dict]:
    """``{transaction_id: {"category", "edit", "note"}}`` for every record a budget edit
    covers. ``category`` is the chosen budget category, ``None`` when the edit excludes
    the row; ``edit`` is the id of the edit deciding that (empty when only a note applies).
    Without the transformer bridge there are no edits to apply."""
    if manual_edits.status() is not None:
        return {}
    manual, _ = manual_edits._import()
    edits = current(path)
    decide = manual.ManualIndex([e for e in edits if e["set"]])
    annotate = manual.ManualIndex([e for e in edits if e.get("note")])
    found = {}
    for record in raw_records:
        chose, noted = decide.match(record), annotate.match(record)
        if chose is None and noted is None:
            continue
        found[record["transaction_id"]] = {
            "category": (chose or {}).get("set", {}).get("budget_category"),
            "edit": chose["id"] if chose else "",
            "note": noted["note"] if noted else ""}
    return found


ADJUST = "budget_adjust"


def build_adjustment(*, day: str, description: str, category: str, amount: float,
                     person: str = "", note: str = "", source: str = "ui") -> dict:
    """A budget row that is not a bank transaction: a gift toward a trip, a spending offset.
    ``amount`` is signed like a ledger's net: positive is money out, negative a credit."""
    try:
        date.fromisoformat(day)
    except ValueError as e:
        raise ValueError(f"{day!r} is not a date (expected YYYY-MM-DD)") from e
    if not description.strip() or not category.strip():
        raise ValueError("an adjustment needs a description and a budget category")
    if not math.isfinite(amount) or amount == 0:
        raise ValueError("an adjustment needs a non-zero amount")
    return {"id": uuid.uuid4().hex[:8], "created_at": datetime.now().isoformat(timespec="seconds"),
            "action": ADJUST, "source": source, "note": note.strip(),
            "set": {"date": day, "description": description.strip(),
                    "budget_category": category.strip(), "amount": round(float(amount), 2),
                    "person": person.strip()}}


def adjustments(path: str | None = None) -> list[dict]:
    """The applicable adjustments in append order: not revoked, well formed."""
    manual, _ = manual_edits._import()
    entries = manual.load_intents(path or manual_edits.edits_path())
    revoked = {e.get("revokes") for e in entries if e.get("action") == "revoke"}
    out = []
    for entry in entries:
        chosen = entry.get("set") or {}
        if entry.get("action") != ADJUST or entry.get("id") in revoked:
            continue
        try:
            date.fromisoformat(chosen["date"])
            usable = (chosen["budget_category"] and chosen["description"]
                      and math.isfinite(chosen["amount"]) and chosen["amount"] != 0)
        except (KeyError, TypeError, ValueError):
            continue
        if usable:
            out.append(entry)
    return out


def adjustment_rows(path: str | None = None) -> pd.DataFrame:
    """Adjustments as ledger rows (person, date, month, category, spend)."""
    rows = [{"person": a["set"].get("person", ""), "date": a["set"]["date"],
             "month": a["set"]["date"][:7], "category": a["set"]["budget_category"],
             "spend": float(a["set"]["amount"])} for a in adjustments(path)]
    return pd.DataFrame(rows, columns=["person", "date", "month", "category", "spend"])
