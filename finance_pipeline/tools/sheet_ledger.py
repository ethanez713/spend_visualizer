"""Turn the two ledgers being compared into the same ``Row`` shape.

The Financials workbook keeps one transaction tab per year (``Source, Date, Description,
Category, Debit, Credit, Debit Less Credit, note``); the app side is the converter-schema
ledger CSV (``Source, Date, Description, Category, Debit, Credit, Debit Less Credit,
Account, Note``). ``net`` is Debit Less Credit: positive is money out.
"""
from __future__ import annotations

import csv
import re
from datetime import date, datetime
from pathlib import Path
from typing import NamedTuple

from tools.xlsx_read import excel_date, read_sheet

WARNING_MARK = "⚠"
_DATE_FORMATS = ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y")


class Row(NamedTuple):
    source: str
    date: date
    desc: str
    category: str
    net: float
    note: str
    ref: int


def parse_money(value) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return round(float(value), 2)
    text = str(value or "").strip().lstrip("'")
    digits = re.sub(r"[^\d.]", "", text)
    if not digits:
        return None
    amount = round(float(digits), 2)
    return -amount if "-" in text or (text.startswith("(") and text.endswith(")")) else amount


def parse_date(value) -> date | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return excel_date(value)
    text = str(value or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def _clean(value) -> str:
    return str(value or "").strip().lstrip("'")


def year_tab_rows(workbook, year: int) -> list[Row]:
    """Transactions on the ``<year>`` tab; rows with no date or a zero net (the sheet's
    DUMMY rows exist only to keep a category in its summary) are skipped."""
    rows = []
    for number, cells in sorted(read_sheet(workbook, str(year)).items()):
        day, net = parse_date(cells.get("B")), parse_money(cells.get("G"))
        if day is None or not net:
            continue
        rows.append(Row(_clean(cells.get("A")), day, _clean(cells.get("C")),
                        _clean(cells.get("D")), net, _clean(cells.get("H")), number))
    return rows


def ledger_rows(path) -> tuple[list[Row], int]:
    """Rows of a converter-schema ledger CSV, plus how many ``⚠`` instruction rows
    (stale-pending reminders, not transactions) were left out."""
    rows, warnings = [], 0
    with Path(path).open(newline="", encoding="utf-8") as f:
        for index, record in enumerate(csv.DictReader(f), start=2):
            if WARNING_MARK in (record.get("Note") or ""):
                warnings += 1
                continue
            day, net = parse_date(record.get("Date")), parse_money(record.get("Debit Less Credit"))
            if day is None or not net:
                continue
            rows.append(Row(_clean(record.get("Source")), day, _clean(record.get("Description")),
                            _clean(record.get("Category")), net, _clean(record.get("Note")), index))
    return rows, warnings


def budget_tab(workbook) -> list[tuple[str, float, float]]:
    """``(category, running average, goal)`` from the Budget tab's table: the rows under
    the "<year> Budget" header up to its Total row."""
    cells = read_sheet(workbook, "Budget")
    table, collecting = [], False
    for _, row in sorted(cells.items()):
        label = row.get("B")
        if isinstance(label, str) and label.endswith(" Budget"):
            collecting = True
            continue
        if not collecting:
            continue
        if label == "Total" or not isinstance(label, str):
            break
        average, goal = row.get("C"), row.get("D")
        if isinstance(average, float) and isinstance(goal, float):
            table.append((label, average, goal))
    return table


# The Budget tab's historic tables fold parking into rent; the year tabs call it Rent.
HISTORIC_LABELS = {"Rent + Parking": "Rent"}


def historic_goals(workbook) -> dict[int, dict[str, float]]:
    """``{year: {category: goal}}`` from the Budget tab's "Historic Data" block: each year
    heads a pair of columns (actual, then goal), and categories run down column B to the
    TOTAL row."""
    cells = read_sheet(workbook, "Budget")
    header = next((n for n, row in sorted(cells.items())
                   if str(row.get("B", "")).startswith("Historic Data")), None)
    if header is None:
        return {}
    years = {col: int(value) for col, value in cells[header].items()
             if col != "B" and isinstance(value, float)}
    goals: dict[int, dict[str, float]] = {year: {} for year in years.values()}
    for number, row in sorted(cells.items()):
        label = row.get("B")
        if number <= header + 1 or not isinstance(label, str):
            continue
        if label.strip().upper() == "TOTAL":
            break
        for column, year in years.items():
            goal = row.get(chr(ord(column) + 1))
            if isinstance(goal, float):
                goals[year][HISTORIC_LABELS.get(label.strip(), label.strip())] = goal
    return goals
