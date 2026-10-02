"""Imported spreadsheet history: spending from before the banks were linked, and the goals
the sheet set for earlier years.

Both files are written by ``finance_pipeline/tools/import_sheet_history.py`` into the data
root. Read-only and fail-soft: a missing or unreadable file means no legacy history.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from config_io import DATA_ROOT

DATA_DIR = DATA_ROOT / "spend_analyzer" / "data"
LEDGER = DATA_DIR / "legacy_ledger.csv"
GOALS = DATA_DIR / "legacy_goals.json"


def before_coverage(ledger: pd.DataFrame, first_date: str) -> pd.DataFrame:
    """Rows dated before ``first_date`` (the archive's first transaction): from there on
    the banks are the source of truth, so an overlapping sheet row would double count."""
    if ledger.empty:
        return ledger
    return ledger[ledger["date"] < first_date[:10]].reset_index(drop=True)


def load_goals(path: str | Path = GOALS) -> dict[int, dict[str, float]]:
    """``{year: {category: goal}}``; entries that are not numbers are skipped."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    goals: dict[int, dict[str, float]] = {}
    for year, by_category in data.items():
        if not str(year).isdigit() or not isinstance(by_category, dict):
            continue
        goals[int(year)] = {c: float(g) for c, g in by_category.items()
                            if isinstance(g, (int, float)) and not isinstance(g, bool)}
    return goals
