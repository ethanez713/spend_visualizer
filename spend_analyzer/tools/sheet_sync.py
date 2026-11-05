#!/usr/bin/env python3
"""Sync household edits in uploaded Google Sheets with budget-edit intents.

Reads ``sheet_edits/edits.jsonl``, ``sheet_exports/manifest.jsonl`` and
``sheet_sync/base.jsonl`` under the data root. A Sheet change the app has not
made becomes a budget intent (pull). An app change the Sheet has not made
becomes a line in ``sheet_sync/outbox.jsonl`` (push) for another tool to apply.
No network and no model calls.

``edits.jsonl`` is the sheet's whole diff against the upload, re-derived every
run. A field the mine does not mention still shows its uploaded value. Base
advances on a pull and when both sides already agree; a push leaves base
alone. The next mine then either shows the write (both sides agree, base
advances) or still shows the upload (the write missed, so it is pushed again).
A cell the household puts back to the upload disagrees with base and is pulled.

The first run, when ``sheet_sync/base.jsonl`` does not exist yet, only prints
the listing. Pass ``--accept-backfill`` to apply it and create that file.
Later runs are ordinary, and the flag then changes nothing.

Usage:
    python -m tools.sheet_sync [--data-root D] [--dry-run] [--accept-backfill]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import budget_edits  # noqa: E402
import budget_table  # noqa: E402
import manual_edits  # noqa: E402
from config_io import data_root, load_app_config, load_budget  # noqa: E402
from ingest.load import load_sources  # noqa: E402

EXCLUDED = "EXCLUDED"
WARNING = "⚠"
PENDING_NOTE = "PENDING"
_ADJUSTMENT = "adjustment"

KINDS = (
    "pulled",
    "pushed",
    "agreed",
    "conflicts",
    "adjustments",
    "skipped_warning",
    "skipped_unmatched",
    "skipped_ambiguous",
    "skipped_pending_delete",
    "skipped_unknown_category",
    "skipped_cross_sheet",
    "skipped_no_amount",
    "skipped_incomplete",
    "skipped_missing_record",
)

_DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y")


@dataclass
class SyncPlan:
    intents: list[dict]
    outbox: list[dict]
    conflicts: list[dict]
    base: list[dict]
    counts: dict[str, int]
    # Row facts the listing prints. Not written anywhere.
    rows: list[dict] = field(default_factory=list)
    # Base as this run received it. The listing's "before" is this, or the
    # upload when a field was never stored. ``base`` is the base to save.
    prior: list[dict] = field(default_factory=list)


@dataclass
class _Row:
    file_id: str
    title: str
    row_id: str
    ledger: dict
    pending: bool


@dataclass
class _Cell:
    file_id: str
    title: str
    row_id: str
    txn: str
    uploaded: str
    ledger: dict
    in_manifest: bool
    changed: dict = field(default_factory=dict)
    after: dict | None = None


class _Index:
    """Manifest rows keyed by sheet, plus which transaction each row shows."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], _Row] = {}
        # txn -> (file_id, row_id, order). Later assignments win, so a silent
        # successor points at its predecessor's row until a real row replaces it.
        self.by_txn: dict[str, tuple[str, str, int]] = {}
        self._n = 0

    def _tick(self) -> int:
        self._n += 1
        return self._n

    def add_row(self, file_id: str, title: str, row: dict) -> None:
        ledger = row.get("ledger") or {}
        if _is_warning(ledger):
            return
        txn = row.get("transaction_id")
        if not txn:
            return
        self.rows[(file_id, txn)] = _Row(file_id, title, txn, dict(ledger), bool(row.get("pending")))
        self.by_txn[txn] = (file_id, txn, self._tick())

    def add_silent(self, entry: dict) -> None:
        settled, predecessor = entry.get("transaction_id"), entry.get("replaces")
        if not settled or not predecessor:
            return
        loc = self.by_txn.get(predecessor)
        if loc:
            self.by_txn[settled] = (loc[0], loc[1], self._tick())

    def row_id_in(self, file_id: str, txn: str) -> str:
        if (file_id, txn) in self.rows:
            return txn
        loc = self.by_txn.get(txn)
        if loc and loc[0] == file_id:
            return loc[1]
        return txn

    def is_latest(self, txn: str, file_id: str, row_id: str) -> bool:
        loc = self.by_txn.get(txn)
        return bool(loc) and loc[0] == file_id and loc[1] == row_id

    def shown(self) -> dict[tuple[str, str], str]:
        """The transaction a sheet row currently stands for (latest alias wins)."""
        best: dict[tuple[str, str], tuple[int, str]] = {}
        for txn, (file_id, row_id, order) in self.by_txn.items():
            key = (file_id, row_id)
            if key not in best or order >= best[key][0]:
                best[key] = (order, txn)
        return {key: txn for key, (_, txn) in best.items()}


def _clean(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text[:1] == "'":
        text = text[1:].strip()
    return text


def _is_warning(row: dict | None) -> bool:
    return _clean((row or {}).get("Note")).startswith(WARNING)


def parse_money(value) -> float | None:
    """Ledger money → a float. Positive is money out, matching Debit Less Credit."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        if number != number:  # NaN
            return None
        return round(number, 2)
    text = _clean(value).replace("$", "").replace(",", "").strip()
    if not text or text.lower() in {"nan", "n/a", "--"}:
        return None
    negative = text.startswith("(") and text.endswith(")")
    text = text.strip("()")
    try:
        amount = round(float(text), 2)
    except ValueError:
        return None
    return -abs(amount) if negative else amount


def parse_date(value) -> str | None:
    text = _clean(value)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def _sheet_note(before: dict | None, after: dict | None) -> str:
    """The note a sheet row carries: Note, else Annotation, else a renamed description.

    ``PENDING`` and notes that start with the warning mark are the pipeline's, not
    the household's, so they do not count.
    """
    before = before or {}
    after = after or {}
    note = _clean(after.get("Note"))
    if not note or note == PENDING_NOTE or note.startswith(WARNING):
        note = ""
    if note:
        return note
    annotation = _clean(after.get("Annotation"))
    if annotation.startswith(WARNING):
        annotation = ""
    if annotation:
        return annotation
    described = _clean(after.get("Description"))
    if described != _clean(before.get("Description")) and (described or _clean(before.get("Description"))):
        return f"Sheet description: {described}"
    return ""


def _changed_fields(edit: dict) -> dict[str, str]:
    """Fields this mined edit actually moves. Anything else still says what was uploaded."""
    types = set(edit.get("edit_types") or [])
    before = edit.get("before") or {}
    after = edit.get("after") or {}
    changed: dict[str, str] = {}
    if "row_deleted" in types:
        changed["category"] = EXCLUDED
        return changed
    if after and _clean(after.get("Category")) != _clean(before.get("Category")):
        changed["category"] = _clean(after.get("Category"))
    if after:
        note = _sheet_note(before, after)
        if note != _sheet_note(before, before):
            changed["note"] = note
    return changed


def _index_manifest(manifest: list[dict]) -> _Index:
    index = _Index()
    for record in manifest:
        file_id = record.get("file_id")
        if not file_id:
            continue
        title = record.get("title") or ""
        for row in record.get("rows") or []:
            if isinstance(row, dict):
                index.add_row(file_id, title, row)
        for entry in record.get("silent") or []:
            if isinstance(entry, dict):
                index.add_silent(entry)
    return index


def _app_values(override: dict | None) -> dict[str, str | None]:
    """Category and note the household has decided, or None where it has no opinion.

    An exclusion is the sentinel. A note with no category edit is not an exclusion,
    and a computed budget category is not an opinion — callers pass ``overrides_for``
    only.
    """
    if not override:
        return {"category": None, "note": None}
    category = None
    if override.get("edit"):
        chosen = override.get("category")
        category = _clean(chosen) if isinstance(chosen, str) and _clean(chosen) else EXCLUDED
    note = override.get("note")
    note = _clean(note) if isinstance(note, str) and _clean(note) else None
    return {"category": category, "note": note}


def _decide(base: str, sheet: str, opinion: str | None) -> tuple[str, str | None]:
    app = base if opinion is None else opinion
    sheet_changed = sheet != base
    app_changed = app != base
    if not sheet_changed and not app_changed:
        return "same", None
    if sheet_changed and not app_changed:
        return "pull", sheet
    if app_changed and not sheet_changed:
        return "push", app
    if sheet == app:
        return "agree", sheet
    return "conflict", None


def _added_id(row: dict) -> str:
    ident = _clean(row.get("ID"))
    if not ident:
        ident = "|".join(_clean(row.get(key)) for key in
                         ("Date", "Debit Less Credit", "Description", "Source"))
    return "added:" + ident


def _adjustment_imported(base: dict[tuple[str, str, str], str], row_id: str) -> bool:
    return any(field == _ADJUSTMENT and rid == row_id for (_, rid, field) in base)


def _record_for(raw: dict[str, dict], cell: _Cell) -> dict | None:
    for key in (cell.txn, cell.row_id):
        if key in raw:
            return raw[key]
    return None


def _intent(record: dict, field: str, value: str, cell: _Cell) -> dict:
    if field == "category":
        if value == EXCLUDED:
            intent = budget_edits.build_edit(record, scope="transaction", exclude=True, source="sheet")
        else:
            intent = budget_edits.build_edit(record, scope="transaction", category=value, source="sheet")
    else:
        intent = budget_edits.build_edit(record, scope="transaction", note=value, source="sheet")
    intent["sheet"] = {"file_id": cell.file_id, "title": cell.title}
    return intent


def _outbox(cell: _Cell, field: str, value: str) -> dict:
    ledger = dict(cell.after) if cell.after else dict(cell.ledger)
    line = {"file_id": cell.file_id, "title": cell.title, "row_id": cell.row_id, "ledger": ledger}
    if field == "category" and value == EXCLUDED:
        line["action"] = "delete_row"
    elif field == "category":
        line["action"] = "set_category"
        line["value"] = value
    else:
        line["action"] = "set_note"
        line["value"] = value
    return line


def _observed(cell: _Cell, field: str) -> str:
    if field in cell.changed:
        return cell.changed[field]
    if field == "category":
        return cell.uploaded
    return _sheet_note(cell.ledger, cell.ledger)


def _display_row(cell: _Cell) -> dict:
    """What the listing prints for one sheet row: the row as it looks now, and
    the uploaded category the before-value falls back to."""
    ledger = cell.after if cell.after else cell.ledger
    return {"file_id": cell.file_id, "title": cell.title, "row_id": cell.row_id,
            "txn": cell.txn, "date": _clean(ledger.get("Date")),
            "amount": _clean(ledger.get("Debit Less Credit")),
            "description": _clean(ledger.get("Description")),
            "category": cell.uploaded, "note": ""}


def _blank_counts() -> dict[str, int]:
    return {kind: 0 for kind in KINDS}


def _base_rows(base: dict[tuple[str, str, str], str]) -> list[dict]:
    return [{"file_id": file_id, "row_id": row_id, "field": name, "value": value}
            for (file_id, row_id, name), value in sorted(base.items())]


def _take_adjustment(edit: dict, *, base: dict, known: set[str],
                     intents: list[dict], conflicts: list[dict], counts: dict[str, int]) -> None:
    row = edit.get("after") or {}
    sheet = edit.get("sheet") or {}
    file_id = sheet.get("file_id") or ""
    title = sheet.get("title") or ""
    if _is_warning(row):
        counts["skipped_warning"] += 1
        return
    if not file_id:
        counts["skipped_unmatched"] += 1
        return
    amount = parse_money(row.get("Debit Less Credit"))
    if amount is None or amount == 0:
        counts["skipped_no_amount"] += 1
        return
    day = parse_date(row.get("Date"))
    description = _clean(row.get("Description"))
    category = _clean(row.get("Category"))
    if not day or not description or not category:
        counts["skipped_incomplete"] += 1
        return
    row_id = _added_id(row)
    if category not in known:
        counts["skipped_unknown_category"] += 1
        conflicts.append({"kind": "unknown_category", "file_id": file_id, "title": title,
                          "row_id": row_id, "field": "category", "value": category})
        return
    if _adjustment_imported(base, row_id):
        return
    try:
        intent = budget_edits.build_adjustment(
            day=day, description=description, category=category, amount=amount,
            person=_clean(row.get("Source")), source="sheet")
    except ValueError:
        counts["skipped_incomplete"] += 1
        return
    intent["sheet"] = {"file_id": file_id, "title": title}
    intents.append(intent)
    base[(file_id, row_id, _ADJUSTMENT)] = "1"
    counts["adjustments"] += 1


def _deletion_blocked(index: _Index, file_id: str, txn: str, raw: dict[str, dict],
                      shown: dict[tuple[str, str], str]) -> bool:
    row_id = index.row_id_in(file_id, txn)
    row = index.rows.get((file_id, row_id))
    if row is not None and row.pending:
        return True
    key = shown.get((file_id, row_id), txn)
    record = raw.get(key) or raw.get(txn) or raw.get(row_id)
    return record is None or bool(record.get("pending"))


def _usable_edits(edits: list[dict], index: _Index, raw: dict[str, dict],
                  shown: dict[tuple[str, str], str], counts: dict[str, int],
                  base: dict, known: set[str], intents: list[dict],
                  conflicts: list[dict]) -> list[dict]:
    usable = []
    for edit in edits:
        types = set(edit.get("edit_types") or [])
        if "row_added" in types:
            _take_adjustment(edit, base=base, known=known, intents=intents,
                             conflicts=conflicts, counts=counts)
            continue
        sheet = edit.get("sheet") or {}
        file_id = sheet.get("file_id")
        if not file_id:
            counts["skipped_unmatched"] += 1
            continue
        # A warning row is one the pipeline uploaded (the baseline note carries the
        # mark). A household note that starts with the mark is ignored later, as a note.
        if _is_warning(edit.get("before")):
            counts["skipped_warning"] += 1
            continue
        upstream = edit.get("upstream") or {}
        if not upstream.get("matched"):
            counts["skipped_unmatched"] += 1
            continue
        if upstream.get("ambiguous"):
            counts["skipped_ambiguous"] += 1
            continue
        txn = upstream.get("transaction_id")
        if not txn:
            counts["skipped_unmatched"] += 1
            continue
        row_id = index.row_id_in(file_id, txn)
        manifest_row = index.rows.get((file_id, row_id))
        if manifest_row is not None and _is_warning(manifest_row.ledger):
            counts["skipped_warning"] += 1
            continue
        if "row_deleted" in types and _deletion_blocked(index, file_id, txn, raw, shown):
            counts["skipped_pending_delete"] += 1
            continue
        usable.append(edit)
    return usable


def _seed_cells(index: _Index, shown: dict[tuple[str, str], str]) -> dict[tuple[str, str], _Cell]:
    cells = {}
    for key, row in index.rows.items():
        cells[key] = _Cell(
            file_id=row.file_id, title=row.title, row_id=row.row_id,
            txn=shown.get(key, row.row_id), uploaded=_clean(row.ledger.get("Category")),
            ledger=dict(row.ledger), in_manifest=True)
    return cells


def _overlay_edits(cells: dict[tuple[str, str], _Cell], edits: list[dict],
                   index: _Index, shown: dict[tuple[str, str], str]) -> None:
    for edit in edits:
        sheet = edit["sheet"]
        file_id = sheet["file_id"]
        txn = edit["upstream"]["transaction_id"]
        row_id = index.row_id_in(file_id, txn)
        key = (file_id, row_id)
        changed = _changed_fields(edit)
        after = edit.get("after") if isinstance(edit.get("after"), dict) else None
        cell = cells.get(key)
        if cell is None:
            before = edit.get("before") or {}
            cells[key] = _Cell(
                file_id=file_id, title=sheet.get("title") or "", row_id=row_id,
                txn=shown.get(key, txn), uploaded=_clean(before.get("Category")),
                ledger=dict(before), in_manifest=False, changed=changed, after=after)
            continue
        cell.changed.update(changed)
        if after is not None:
            cell.after = after
        if not cell.title:
            cell.title = sheet.get("title") or ""


def _cross_sheet(cells: dict[tuple[str, str], _Cell], conflicts: list[dict],
                 counts: dict[str, int]) -> set[tuple[str, str, str]]:
    """One transaction, two sheets, two values: import neither."""
    grouped: dict[str, list[_Cell]] = defaultdict(list)
    for cell in cells.values():
        grouped[cell.txn].append(cell)
    blocked = set()
    for txn, group in grouped.items():
        if len({cell.file_id for cell in group}) < 2:
            continue
        for name in ("category", "note"):
            observed = {}
            for cell in group:
                observed[cell.file_id] = (cell, _observed(cell, name))
            if len({value for _, value in observed.values()}) < 2:
                continue
            values = [{"file_id": cell.file_id, "title": cell.title, "row_id": cell.row_id,
                       "value": value}
                      for _, (cell, value) in sorted(observed.items())]
            conflicts.append({"kind": "cross_sheet", "row_id": txn, "field": name, "values": values})
            counts["skipped_cross_sheet"] += 1
            for cell, _ in observed.values():
                blocked.add((cell.file_id, cell.row_id, name))
    return blocked


def _restore(base: dict, original: dict, key: tuple[str, str, str]) -> None:
    if key in original:
        base[key] = original[key]
    else:
        base.pop(key, None)


def _merge(cells: dict[tuple[str, str], _Cell], *, index: _Index, raw: dict[str, dict],
           overrides: dict[str, dict], known: set[str], base: dict, original: dict,
           blocked: set[tuple[str, str, str]], intents: list[dict], outbox: list[dict],
           conflicts: list[dict], counts: dict[str, int]) -> None:
    pulled: set[tuple[str, str, str]] = set()
    for key in sorted(cells):
        cell = cells[key]
        opinion = _app_values(overrides.get(cell.txn))
        for name in ("category", "note"):
            field_key = (cell.file_id, cell.row_id, name)
            if field_key in blocked:
                continue
            uploaded = cell.uploaded if name == "category" else ""
            base_val = original.get(field_key, uploaded)
            # No mined edit means the cell still shows the upload. The mine is the
            # sheet's whole diff against that upload, not a delta from base.
            sheet_val = cell.changed[name] if name in cell.changed else uploaded
            if (name == "category" and name in cell.changed and sheet_val != EXCLUDED
                    and sheet_val not in known):
                counts["skipped_unknown_category"] += 1
                conflicts.append({"kind": "unknown_category", "file_id": cell.file_id,
                                  "title": cell.title, "row_id": cell.row_id,
                                  "field": "category", "value": sheet_val})
                continue
            action, agreed = _decide(base_val, sheet_val, opinion[name])
            if action == "same":
                continue
            if action == "conflict":
                conflicts.append({"kind": "value_conflict", "file_id": cell.file_id,
                                  "title": cell.title, "row_id": cell.row_id, "field": name,
                                  "base": base_val, "sheet": sheet_val, "app": opinion[name]})
                continue
            if action == "agree":
                base[field_key] = agreed
                counts["agreed"] += 1
                continue
            if action == "pull":
                # build_edit cannot record an empty note, and there is no "clear note"
                # intent. Leave the previous note where it is.
                if name == "note" and not agreed:
                    continue
                marker = (cell.txn, name, agreed)
                record = _record_for(raw, cell)
                if marker not in pulled and record is None:
                    counts["skipped_missing_record"] += 1
                    continue
                base[field_key] = agreed
                if marker not in pulled:
                    try:
                        intents.append(_intent(record, name, agreed, cell))
                    except ValueError:
                        _restore(base, original, field_key)
                        counts["skipped_incomplete"] += 1
                        continue
                    pulled.add(marker)
                    counts["pulled"] += 1
                continue
            if action != "push":
                continue
            if not cell.in_manifest or not index.is_latest(cell.txn, cell.file_id, cell.row_id):
                continue
            # Leave base. The next mine shows the write if it landed (then the
            # two sides agree); if it did not, the sheet still shows the upload
            # and this push is planned again.
            outbox.append(_outbox(cell, name, agreed))
            counts["pushed"] += 1


def plan(edits: list[dict], manifest: list[dict], base_rows: list[dict],
         raw_records, overrides: dict[str, dict], known_categories) -> SyncPlan:
    """Three-way merge. Pure: every input is a value, nothing is read or written.

    ``raw_records`` is ``{transaction_id: raw record}`` (or a list of those records).
    ``overrides`` is the ``budget_edits.overrides_for`` map. ``known_categories`` is
    the budget vocabulary; a Sheet category outside it is reported, not imported.
    """
    if isinstance(raw_records, dict):
        raw = raw_records
    else:
        raw = {record["transaction_id"]: record for record in raw_records
               if isinstance(record, dict) and record.get("transaction_id")}
    known = {_clean(name) for name in known_categories if _clean(name)}
    original = {(row["file_id"], row["row_id"], row["field"]): row["value"]
                for row in base_rows
                if row.get("file_id") and row.get("row_id") and row.get("field")}
    base = dict(original)
    counts = _blank_counts()
    intents: list[dict] = []
    conflicts: list[dict] = []
    outbox: list[dict] = []

    index = _index_manifest(manifest)
    shown = index.shown()
    usable = _usable_edits(edits, index, raw, shown, counts, base, known, intents, conflicts)
    cells = _seed_cells(index, shown)
    _overlay_edits(cells, usable, index, shown)
    blocked = _cross_sheet(cells, conflicts, counts)
    _merge(cells, index=index, raw=raw, overrides=overrides, known=known, base=base,
           original=original, blocked=blocked, intents=intents, outbox=outbox,
           conflicts=conflicts, counts=counts)

    outbox.sort(key=lambda line: (line["file_id"], line["row_id"], line["action"], line.get("value", "")))
    conflicts.sort(key=lambda line: (line.get("kind", ""), line.get("file_id", ""),
                                    line.get("row_id", ""), line.get("field", ""),
                                    json.dumps(line.get("values"), sort_keys=True, default=str)))
    counts["conflicts"] = len(conflicts)
    return SyncPlan(intents=intents, outbox=outbox, conflicts=conflicts,
                    base=_base_rows(base), counts=counts,
                    rows=[_display_row(cells[key]) for key in sorted(cells)],
                    prior=_base_rows(original))


def format_counts(counts: dict[str, int], *, dry_run: bool = False) -> str:
    lines = ["dry-run"] if dry_run else []
    lines += [f"{kind}: {counts.get(kind, 0)}" for kind in KINDS]
    return "\n".join(lines) + "\n"


def _show_date(value) -> str:
    text = _clean(value)
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).strftime("%m/%d/%Y")
        except ValueError:
            continue
    return text


def _money(amount: float) -> str:
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


def _listing_line(date, amount, description, field, change) -> str:
    return f"{date}  {amount}  {description}  {field}  {change}"


def _arrow(before: str, after: str) -> str:
    if before:
        return f"{before} → {after}"
    return f"→ {after}"


def _prior_map(plan: SyncPlan) -> dict[tuple[str, str, str], str]:
    return {(row["file_id"], row["row_id"], row["field"]): row["value"]
            for row in plan.prior
            if row.get("file_id") and row.get("row_id") and row.get("field")}


def _uploaded(facts: dict | None, field: str) -> str:
    if not facts:
        return ""
    if field == "category":
        return facts.get("category") or ""
    return facts.get("note") or ""


def _before(prior: dict, facts: dict | None, field: str) -> str:
    if facts is not None:
        stored = prior.get((facts["file_id"], facts["row_id"], field))
        if stored is not None:
            return stored
    return _uploaded(facts, field)


def _row_by_id(plan: SyncPlan) -> dict[tuple[str, str], dict]:
    return {(row["file_id"], row["row_id"]): row for row in plan.rows}


def _row_by_txn(plan: SyncPlan) -> dict[tuple[str, str], dict]:
    return {(row["file_id"], row["txn"]): row for row in plan.rows}


def _sort_date(value: str) -> str:
    return parse_date(value) or "9999-99-99"


def format_plan(plan: SyncPlan, *, dry_run: bool = False) -> str:
    """The household's review of one plan: intents, outbox lines, and conflicts.

    Grouped by sheet title. Each change is date, amount, description, field,
    then the base or uploaded value and the new value. A conflict names both
    of its values. Counts follow. Nothing is read or written.
    """
    prior = _prior_map(plan)
    by_id = _row_by_id(plan)
    by_txn = _row_by_txn(plan)
    sections: dict[tuple[str, str], list[tuple]] = defaultdict(list)
    cross: list[tuple] = []

    def place(file_id: str, title: str, date: str, amount: str, description: str,
              field: str, change: str, *, conflict: bool = False) -> None:
        text = _listing_line(_show_date(date), amount, description, field, change)
        bucket = sections[(title or "(untitled)", file_id or "")]
        bucket.append((_sort_date(date), description, 1 if conflict else 0, field, text))

    for intent in plan.intents:
        sheet = intent.get("sheet") or {}
        file_id = sheet.get("file_id") or ""
        title = sheet.get("title") or ""
        if intent.get("action") == budget_edits.ADJUST:
            chosen = intent.get("set") or {}
            place(file_id, title, chosen.get("date") or "", _money(float(chosen.get("amount") or 0)),
                  chosen.get("description") or "", "category",
                  _arrow("", chosen.get("budget_category") or ""))
            continue
        txn = (intent.get("match") or {}).get("transaction_id") or ""
        facts = by_txn.get((file_id, txn))
        date = (facts or {}).get("date") or ""
        amount = (facts or {}).get("amount") or ""
        description = (facts or {}).get("description") or ""
        chosen = intent.get("set") or {}
        if chosen.get("exclude") is True or chosen.get("budget_category"):
            after = EXCLUDED if chosen.get("exclude") is True else chosen["budget_category"]
            before = _before(prior, facts, "category")
            place(file_id, title, date, amount, description, "category", _arrow(before, after))
        note = (intent.get("note") or "").strip()
        if note:
            before = _before(prior, facts, "note")
            place(file_id, title, date, amount, description, "note", _arrow(before, note))

    for line in plan.outbox:
        ledger = line.get("ledger") or {}
        facts = {"file_id": line.get("file_id") or "", "row_id": line.get("row_id") or "",
                 "category": _clean(ledger.get("Category")), "note": ""}
        action = line.get("action")
        if action == "delete_row":
            field, after = "category", EXCLUDED
        elif action == "set_note":
            field, after = "note", line.get("value") or ""
        else:
            field, after = "category", line.get("value") or ""
        before = _before(prior, facts, field)
        place(facts["file_id"], line.get("title") or "", ledger.get("Date") or "",
              _clean(ledger.get("Debit Less Credit")), _clean(ledger.get("Description")),
              field, _arrow(before, after))

    for conflict in plan.conflicts:
        kind = conflict.get("kind")
        if kind == "cross_sheet":
            values = conflict.get("values") or []
            facts = None
            for value in values:
                facts = by_id.get((value.get("file_id"), value.get("row_id")))
                if facts:
                    break
            shown = " ≠ ".join(f"{value.get('title') or value.get('file_id')}: {value.get('value') or ''}"
                               for value in values)
            text = _listing_line(_show_date((facts or {}).get("date") or ""),
                                 (facts or {}).get("amount") or "",
                                 (facts or {}).get("description") or "",
                                 conflict.get("field") or "", f"conflict: {shown}")
            cross.append((_sort_date((facts or {}).get("date") or ""), text))
            continue
        facts = by_id.get((conflict.get("file_id"), conflict.get("row_id")))
        field = conflict.get("field") or ""
        if kind == "value_conflict":
            change = f"conflict: sheet {conflict.get('sheet') or ''} ≠ app {conflict.get('app') or ''}"
        else:
            before = _before(prior, facts, field)
            value = conflict.get("value") or ""
            change = (f"conflict: {_arrow(before, value)}" if before
                      else f"conflict: {value}")
        place(conflict.get("file_id") or "", conflict.get("title") or "",
              (facts or {}).get("date") or "", (facts or {}).get("amount") or "",
              (facts or {}).get("description") or "", field, change, conflict=True)

    titles = [title for title, _file in sections]
    chunks = ["dry-run"] if dry_run else []
    for title, file_id in sorted(sections):
        heading = title if titles.count(title) == 1 else f"{title} ({file_id})"
        chunks.append(heading)
        for entry in sorted(sections[(title, file_id)]):
            chunks.append(f"  {entry[-1]}")
    if cross:
        chunks.append("cross-sheet")
        for _sort, text in sorted(cross):
            chunks.append(f"  {text}")
    body = "\n".join(chunks)
    counts = format_counts(plan.counts, dry_run=False).rstrip("\n")
    if body:
        return body + "\n" + counts + "\n"
    return (("dry-run\n" if dry_run else "") + counts + "\n")


def read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            rows.append(item)
    return rows


def _dumps(rows: list[dict]) -> str:
    return "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows)


def write_private(path: Path, text: str) -> None:
    """Atomic replace, directory 0700, file 0600."""
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    fd, tmp_name = tempfile.mkstemp(dir=directory, suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.chmod(path, 0o600)


def _write_if_changed(path: Path, text: str) -> None:
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return
    write_private(path, text)


def intent_path(root: Path) -> Path:
    return root / "plaid_category_transformer" / "data" / "manual_edits.jsonl"


def resolve_root(override: Path | None) -> Path:
    if override is not None:
        return Path(override).expanduser()
    return data_root()


def load_raw_records(root: Path) -> dict[str, dict]:
    """Archive rows with the live intent log replayed, the way ``raw_index`` loads them."""
    paths = []
    for entry in load_app_config().archive_paths:
        path = Path(entry).expanduser()
        if not path.is_absolute():
            path = (root / entry).resolve()
        paths.append(str(path))
    records = load_sources(paths)
    if manual_edits.status() is None:
        manual_edits.overlay(records, str(intent_path(root)))
    return {record["transaction_id"]: record for record in records if record.get("transaction_id")}


def categories_for(root: Path, manifest: list[dict], overrides: dict[str, dict]) -> list[str]:
    """Budget vocabulary plus categories the sheets and the household already use."""
    budget = load_budget(root / "spend_analyzer" / "config" / "budget.yaml")
    present = set()
    for record in manifest:
        for row in record.get("rows") or []:
            ledger = (row or {}).get("ledger") or {}
            if _is_warning(ledger):
                continue
            category = _clean(ledger.get("Category"))
            if category:
                present.add(category)
    for override in overrides.values():
        category = override.get("category")
        if isinstance(category, str) and _clean(category):
            present.add(_clean(category))
    return budget_table.known_categories(budget, present)


# Printed after a first-run preview. The pipeline keeps going either way.
BACKFILL_HINT = (
    "Review the listing above and run `python -m tools.sheet_sync --accept-backfill` "
    "from spend_analyzer to apply it."
)


def execute(root: Path, *, dry_run: bool = False, accept_backfill: bool = False) -> SyncPlan:
    root = Path(root)
    base_path = root / "sheet_sync" / "base.jsonl"
    # No base file means no sync has run. Applying now would import every edit
    # already in the sheets, so hold for review unless the household accepts it.
    first_run = not base_path.is_file()
    edits = read_jsonl(root / "sheet_edits" / "edits.jsonl")
    manifest = read_jsonl(root / "sheet_exports" / "manifest.jsonl")
    base_rows = read_jsonl(base_path)
    raw = load_raw_records(root)
    log = intent_path(root)
    overrides = {}
    if manual_edits.status() is None:
        overrides = budget_edits.overrides_for(list(raw.values()), str(log))
    result = plan(edits, manifest, base_rows, raw, overrides, categories_for(root, manifest, overrides))
    if dry_run or (first_run and not accept_backfill):
        print(format_plan(result, dry_run=True), end="")
        if first_run and not accept_backfill:
            print(BACKFILL_HINT)
        return result
    if result.intents:
        log.parent.mkdir(parents=True, exist_ok=True)
        budget_edits.append_edits(result.intents, str(log))
    directory = root / "sheet_sync"
    _write_if_changed(directory / "base.jsonl", _dumps(result.base))
    _write_if_changed(directory / "outbox.jsonl", _dumps(result.outbox))
    _write_if_changed(directory / "conflicts.jsonl", _dumps(result.conflicts))
    print(format_plan(result), end="")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sync Google Sheet edits with budget-edit intents.")
    parser.add_argument("--data-root", type=Path, default=None,
                        help="data root (default: the same resolution config_io uses)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the merge and write nothing")
    parser.add_argument("--accept-backfill", action="store_true",
                        help="apply the first run, importing every edit already in the sheets")
    args = parser.parse_args(argv)
    execute(resolve_root(args.data_root), dry_run=args.dry_run,
            accept_backfill=args.accept_backfill)
    return 0


if __name__ == "__main__":
    sys.exit(main())
