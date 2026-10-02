#!/usr/bin/env python3
"""Copy the Financials sheet's year tabs and historic goals into the data root.

The sheet holds spending Plaid cannot supply (everything before the banks were linked).
This writes it once, locally, in the converter's ledger schema so the app's Budget tab
can show those months:

    python3 tools/import_sheet_history.py --xlsx ~/downloads/Financials.xlsx
    python3 tools/import_sheet_history.py --xlsx F.xlsx --years 2024 2025 --dry-run

Outputs, under ``<data root>/spend_analyzer/data/`` (0600; personal data, never this repo):
``legacy_ledger.csv`` (every transaction on the chosen year tabs) and ``legacy_goals.json``
(each historic year's goal per category). The app applies a ledger row only for dates
before its own first transaction, so overlap with Plaid history is harmless. Re-running
replaces both files. No network.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import tempfile
from pathlib import Path

# A script run puts tools/ on sys.path, not the package root the imports below need.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import _data_root  # noqa: E402
from tools.sheet_ledger import Row, historic_goals, year_tab_rows  # noqa: E402
from tools.xlsx_read import sheet_names  # noqa: E402

LEDGER_COLUMNS = ["Source", "Date", "Description", "Category", "Debit", "Credit",
                  "Debit Less Credit", "Account", "Note"]
FORMULA_STARTS = ("=", "+", "-", "@", "\t", "\r")


def _safe(text: str) -> str:
    return "'" + text if text.startswith(FORMULA_STARTS) else text


def _money(value: float) -> str:
    return f"{'-' if value < 0 else ''}${abs(value):,.2f}"


def ledger_record(row: Row) -> list[str]:
    return [_safe(row.source), row.date.strftime("%m/%d/%Y"), _safe(row.desc), _safe(row.category),
            _money(row.net) if row.net > 0 else "", _money(-row.net) if row.net < 0 else "",
            _money(row.net), "", _safe(row.note)]


def _write_private(path: Path, write) -> None:
    """Atomically replace ``path`` with owner-only content produced by ``write(file)``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    with os.fdopen(fd, "w", newline="", encoding="utf-8") as f:
        write(f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def main(argv=None) -> int:
    root = Path(__file__).resolve().parent.parent.parent
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--xlsx", required=True, type=Path)
    ap.add_argument("--years", nargs="+", type=int, help="year tabs to copy (default: all)")
    ap.add_argument("--data-root", type=Path)
    ap.add_argument("--dry-run", action="store_true", help="report what would be written")
    args = ap.parse_args(argv)

    tabs = sorted(int(n) for n in sheet_names(args.xlsx) if re.fullmatch(r"\d{4}", n))
    years = args.years or tabs
    missing = [y for y in years if y not in tabs]
    if missing:
        sys.exit(f"no year tab for {missing}; the workbook has {tabs}")
    rows = [r for year in years for r in year_tab_rows(args.xlsx, year)]
    goals = {str(y): g for y, g in historic_goals(args.xlsx).items() if g}

    out = (args.data_root or _data_root(root)) / "spend_analyzer" / "data"
    print(f"{len(rows)} transactions from tabs {years}; goals for {sorted(goals)}")
    if args.dry_run:
        return 0

    def write_ledger(f):
        writer = csv.writer(f)
        writer.writerow(LEDGER_COLUMNS)
        writer.writerows(ledger_record(r) for r in rows)

    _write_private(out / "legacy_ledger.csv", write_ledger)
    _write_private(out / "legacy_goals.json", lambda f: json.dump(goals, f, indent=2))
    print(f"wrote {out / 'legacy_ledger.csv'} and {out / 'legacy_goals.json'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
