"""Review sign-offs: which rows the household has confirmed the budget category of.

A sign-off is an intent in the transformer's shared log (``action: budget_review``) that
records, for each transaction confirmed, the budget category it had at that moment. A row
needs review until a live sign-off covers it with the category it still has. A row whose
category has moved since (a rule changed, a re-audit) comes back as *changed* unless the
household's own budget edit, newer than the sign-off, is what moved it. A posted row
inherits the sign-off of the pending row it replaced.
"""
from __future__ import annotations

import json
import uuid
from collections import Counter
from datetime import datetime
from pathlib import Path

import pandas as pd

import budget_edits
import manual_edits

ACTION = "budget_review"
NEW, REVIEWED, CHANGED = "new", "reviewed", "changed"
NOT_IN_BUDGET = "Not budget spending"


def build_signoff(reviewed: dict[str, str | None], *, note: str = "", source: str = "ui") -> dict:
    if not reviewed:
        raise ValueError("a sign-off needs at least one transaction")
    return {"id": uuid.uuid4().hex[:8], "created_at": datetime.now().isoformat(timespec="seconds"),
            "action": ACTION, "source": source, "note": note.strip(),
            "set": {"reviewed": {str(t): c for t, c in reviewed.items()}}}


def load_entries(path: str | None = None) -> list[dict]:
    manual, _ = manual_edits._import()
    return manual.load_intents(path or manual_edits.edits_path())


def append(intents: list[dict], path: str | None = None) -> None:
    manual, _ = manual_edits._import()
    for intent in intents:
        manual.append_intent(path or manual_edits.edits_path(), intent)


def live(entries: list[dict]) -> dict[str, tuple[str | None, str, str]]:
    """``{transaction_id: (category signed off, created_at, sign-off id)}``; the latest
    unrevoked sign-off of a row wins."""
    revoked = {e.get("revokes") for e in entries if e.get("action") == "revoke"}
    out = {}
    for entry in entries:
        if entry.get("action") != ACTION or entry.get("id") in revoked:
            continue
        reviewed = (entry.get("set") or {}).get("reviewed")
        if not isinstance(reviewed, dict):
            continue
        for tid, category in reviewed.items():
            out[tid] = (category, entry.get("created_at", ""), entry["id"])
    return out


def has_any(entries: list[dict]) -> bool:
    return any(e.get("action") == ACTION for e in entries)


def in_scope(df: pd.DataFrame, needs_decision) -> pd.Series:
    """Rows review asks about: budget rows, and off-budget rows whose atom needs a decision."""
    return df["budget_cat"].notna() | df["atom"].map(needs_decision).astype(bool)


def states(df: pd.DataFrame, entries: list[dict], parents: dict[str, str],
           needs_decision) -> pd.DataFrame:
    """The rows review shows or has shown, with ``review_state`` (new / reviewed /
    changed) and ``reviewed_as`` (the category signed off, for a changed row).
    ``parents`` maps a posted transaction id to the pending one it replaced."""
    signed = live(entries)
    edit_times = {e.get("id"): e.get("created_at", "") for e in entries
                  if e.get("action") == budget_edits.ACTION}
    scope = in_scope(df, needs_decision)
    rows = df[scope | df["transaction_id"].isin(signed.keys())
              | df["transaction_id"].map(lambda t: parents.get(t) in signed)].copy()
    state, was = [], []
    for tid, category, edit_id in zip(rows["transaction_id"], rows["budget_cat"],
                                      rows["budget_edit_id"]):
        hit = signed.get(tid) or signed.get(parents.get(tid, ""))
        if hit is None:
            state.append(NEW)
            was.append(None)
            continue
        signed_cat, signed_at, _ = hit
        now = None if pd.isna(category) else category
        if now == signed_cat or (edit_id and edit_times.get(edit_id, "") > signed_at):
            state.append(REVIEWED)
            was.append(None)
        else:
            state.append(CHANGED)
            was.append(signed_cat)
    rows["review_state"] = state
    rows["reviewed_as"] = pd.Series(was, index=rows.index, dtype="object")
    return rows


def pending_parents(raw_records) -> dict[str, str]:
    return {r["transaction_id"]: r["pending_transaction_id"] for r in raw_records
            if r.get("transaction_id") and r.get("pending_transaction_id")}


def manifest_path() -> Path:
    from config_io import DATA_ROOT
    return DATA_ROOT / "sheet_exports" / "manifest.jsonl"


def sheet_coverage(path: Path) -> tuple[set[str], str | None]:
    """Transaction ids the converter's Sheets have held, and the manifest's floor date
    (rows before it went out in the monthly Sheets that predate the manifest)."""
    ids, floor = set(), None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return ids, None
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        for row in (record.get("rows") or []) + (record.get("silent") or []):
            if row.get("transaction_id"):
                ids.add(row["transaction_id"])
        since = record.get("since")
        if since and (floor is None or since < floor):
            floor = since
    return ids, floor


def baseline(df: pd.DataFrame, path: Path, needs_decision) -> dict[str, str | None]:
    """``{transaction_id: budget category}`` for the in-scope rows the Sheets covered."""
    ids, floor = sheet_coverage(path)
    if not ids and floor is None:
        return {}
    rows = df[in_scope(df, needs_decision)]
    covered = rows["transaction_id"].isin(ids)
    if floor:
        covered |= rows["date_resolved"].astype(str) < floor
    rows = rows[covered]
    return {t: (None if pd.isna(c) else c) for t, c in zip(rows["transaction_id"], rows["budget_cat"])}


def category_label(category) -> str:
    return NOT_IN_BUDGET if category is None or pd.isna(category) else str(category)


def merchant_history(df: pd.DataFrame, exclude: set[str]) -> dict[str, Counter]:
    """Budget categories each merchant's other posted rows carry, by ``merchant_id``."""
    rows = df[~df["transaction_id"].isin(exclude) & ~df["pending"].fillna(False).astype(bool)
              & df["merchant_id"].fillna("").ne("")]
    out: dict[str, Counter] = {}
    for merchant, category in zip(rows["merchant_id"], rows["budget_cat"]):
        out.setdefault(merchant, Counter())[category_label(category)] += 1
    return out


def history_label(counts: Counter | None, limit: int = 3) -> str:
    if not counts:
        return ""
    return " · ".join(f"{c} {n}" for c, n in counts.most_common(limit))


def differs_from_history(category, counts: Counter | None) -> bool:
    """The merchant's other rows mostly sit in another category (two or more of them)."""
    if not counts:
        return False
    top, n = counts.most_common(1)[0]
    return n >= 2 and top != category_label(category)


def edits_from_grid(before: pd.DataFrame, after: pd.DataFrame, raw_lookup) -> list[dict]:
    """Transaction-scope budget edits for the rows whose category or note was changed in
    the review grid. Both frames share an index and carry ``transaction_id``,
    ``category`` (a budget category or ``NOT_IN_BUDGET``) and ``note``."""
    edits = []
    for idx in after.index:
        old, new = before.loc[idx], after.loc[idx]
        new_note = str(new["note"] or "").strip()
        note = new_note if new_note != str(old["note"] or "").strip() else ""
        moved = new["category"] != old["category"]
        if not (moved or note):
            continue
        record = raw_lookup(old["transaction_id"])
        if record is None:
            raise ValueError(f"transaction {old['transaction_id']} is not in the archive")
        excluded = moved and new["category"] == NOT_IN_BUDGET
        edits.append(budget_edits.build_edit(
            record, scope="transaction", note=note, exclude=excluded,
            category=new["category"] if moved and not excluded else None, source="ui-review"))
    return edits
