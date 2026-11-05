#!/usr/bin/env python3
"""Write the Budget view's settled rows as a converter-schema ledger CSV.

    python3 tools/export_budget_rows.py --out budget_rows.csv

The frame is the one the Budget view reads (``data.build_cube``): household budget
edits already applied, ``budget_cat`` and the signed ``budget_amount``. Pending rows
and rows with no budget category (excluded, or never budget spending) are left out.
Debit Less Credit is that signed amount, positive when money goes out. The pipeline's
three-way sheet compare reads the file with ``--app-csv``.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd  # noqa: E402

from data import build_cube  # noqa: E402
from viz import sanitize_for_csv  # noqa: E402

LEDGER_COLUMNS = ["Source", "Date", "Description", "Category", "Debit", "Credit",
                  "Debit Less Credit", "Account", "Note"]


def _money(value: float) -> str:
    return f"{'-' if value < 0 else ''}${abs(value):,.2f}"


def _text(value) -> str:
    return value if isinstance(value, str) else ""


def ledger_table(df: pd.DataFrame) -> pd.DataFrame:
    """Settled rows of the Budget view's frame that have a budget category."""
    if df.empty or "budget_cat" not in df.columns:
        return pd.DataFrame(columns=LEDGER_COLUMNS)
    kept = df.loc[
        ~df["pending"].fillna(False).astype(bool)
        & df["budget_cat"].notna()
        & df["date_resolved"].notna()
        & df["budget_amount"].fillna(0).ne(0)
    ].sort_values(["date_resolved", "description", "transaction_id"], kind="mergesort")
    records = []
    for row in kept.to_dict("records"):
        net = round(float(row["budget_amount"]), 2)
        day = datetime.strptime(str(row["date_resolved"])[:10], "%Y-%m-%d")
        records.append({
            "Source": _text(row.get("person")),
            "Date": day.strftime("%m/%d/%Y"),
            "Description": _text(row.get("description")),
            "Category": _text(row.get("budget_cat")),
            "Debit": _money(net) if net > 0 else "",
            "Credit": _money(-net) if net < 0 else "",
            "Debit Less Credit": _money(net),
            "Account": _text(row.get("account_name")),
            "Note": _text(row.get("budget_note")),
        })
    table = pd.DataFrame(records, columns=LEDGER_COLUMNS)
    return sanitize_for_csv(table)


def _write_csv(path: Path, table: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        table.to_csv(handle, index=False, lineterminator="\n")


def export(out: Path, **build_kwargs) -> pd.DataFrame:
    """Build the Budget view's frame and write its settled budget rows to ``out``."""
    table = ledger_table(build_cube(**build_kwargs).df)
    _write_csv(out, table)
    return table


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", required=True, type=Path, help="where to write the ledger CSV")
    args = ap.parse_args(argv)
    table = export(args.out)
    print(f"wrote {len(table)} budget rows to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
