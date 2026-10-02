"""Read cell values out of an .xlsx workbook with the standard library only.

Values are the cached results Excel/Sheets stored, so a formula cell yields what the
sheet showed when it was exported. Text, numbers and booleans come back as
``str`` / ``float`` / ``bool``; an empty cell is absent from its row.
"""
from __future__ import annotations

import re
import zipfile
import xml.etree.ElementTree as ET
from datetime import date, timedelta

_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_DOC_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_CELL_REF = re.compile(r"([A-Z]+)(\d+)")


def _rich_text(node) -> str:
    """Concatenate the visible text of a string item; phonetic runs are skipped."""
    parts = []
    for child in node:
        if child.tag == _MAIN + "t":
            parts.append(child.text or "")
        elif child.tag == _MAIN + "r":
            parts.extend(t.text or "" for t in child.findall(_MAIN + "t"))
    return "".join(parts)


def _shared_strings(book: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in book.namelist():
        return []
    root = ET.fromstring(book.read("xl/sharedStrings.xml"))
    return [_rich_text(si) for si in root.findall(_MAIN + "si")]


def _sheet_paths(book: zipfile.ZipFile) -> dict[str, str]:
    """Sheet name -> part path, resolved through the workbook relationships (the
    numbering of ``sheetN.xml`` follows creation order, not tab order)."""
    rels = ET.fromstring(book.read("xl/_rels/workbook.xml.rels"))
    targets = {r.get("Id"): r.get("Target") for r in rels.findall(_PKG_REL + "Relationship")}
    workbook = ET.fromstring(book.read("xl/workbook.xml"))
    paths = {}
    for sheet in workbook.find(_MAIN + "sheets"):
        target = targets[sheet.get(_DOC_REL + "id")]
        paths[sheet.get("name")] = target.lstrip("/") if target.startswith("/") else "xl/" + target
    return paths


def sheet_names(path) -> list[str]:
    with zipfile.ZipFile(path) as book:
        return list(_sheet_paths(book))


def _cell_value(cell, strings: list[str]):
    kind = cell.get("t")
    if kind == "inlineStr":
        return _rich_text(cell.find(_MAIN + "is"))
    raw = cell.find(_MAIN + "v")
    if raw is None or raw.text is None:
        return None
    if kind == "s":
        return strings[int(raw.text)]
    if kind in ("str", "e"):
        return raw.text
    if kind == "b":
        return raw.text == "1"
    return float(raw.text)


def read_sheet(path, name: str) -> dict[int, dict[str, object]]:
    """``{row number: {column letters: value}}`` for the named sheet."""
    with zipfile.ZipFile(path) as book:
        paths = _sheet_paths(book)
        if name not in paths:
            raise KeyError(f"no sheet named {name!r}; the workbook has {sorted(paths)}")
        strings = _shared_strings(book)
        root = ET.fromstring(book.read(paths[name]))
    rows: dict[int, dict[str, object]] = {}
    for cell in root.iter(_MAIN + "c"):
        value = _cell_value(cell, strings)
        if value is None:
            continue
        column, row = _CELL_REF.fullmatch(cell.get("r")).groups()
        rows.setdefault(int(row), {})[column] = value
    return rows


def excel_date(serial: float) -> date:
    return date(1899, 12, 30) + timedelta(days=int(serial))
