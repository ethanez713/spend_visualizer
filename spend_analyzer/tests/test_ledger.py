"""Tests for the external budget-ledger source (opt-in Budget-tab path).

Covers the CSV loader's parsing/fail-soft behavior, the category × month pivot
with its supported filters, and the budget.yaml `ledger.csv` switch + resolution.
"""
import csv
from pathlib import Path

import pandas as pd

from config_io import DATA_ROOT, Budget, load_budget
from ledger import load_ledger, monthly_pivot

_HEADER = ["Source", "Date", "Description", "Category",
           "Debit", "Credit", "Debit Less Credit"]

# One row per case: mixed date formats, a comma'd amount, a refund (negative net),
# a zero amount, a missing date, and an unparseable date.
_ROWS = [
    ["Alice", "06/10/2026", "Cafe", "Dining Out", "$80.47", "", "$80.47"],
    ["Bob", "04-15-2026", "Airline", "Travel", "", "$333.60", "-$333.60"],
    ["Alice", "06/02/2026", "Grocer", "Groceries", "$1,234.56", "", "$1,234.56"],
    ["Alice", "06/01/2026", "Refund", "House", "", "$58.28", "-$58.28"],
    ["Alice", "06/03/2026", "ZeroRow", "Other", "$0.00", "", "$0.00"],
    ["Alice", "", "NoDate", "Other", "$5.00", "", "$5.00"],
    ["Alice", "not-a-date", "BadDate", "Other", "$7.00", "", "$7.00"],
]


def _write_ledger(path: Path, rows=_ROWS) -> Path:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(_HEADER)
        w.writerows(rows)
    return path


# ── loader ───────────────────────────────────────────────────────────────────

def test_load_ledger_parses_money_dates_and_drops_non_transactions(tmp_path):
    df = load_ledger(_write_ledger(tmp_path / "ledger.csv"))

    # zero amount, blank date, and unparseable date are all dropped (fail-soft).
    assert set(df["category"]) == {"Dining Out", "Travel", "Groceries", "House"}
    assert len(df) == 4

    by_cat = df.set_index("category")
    assert by_cat.loc["Dining Out", "month"] == "2026-06"
    assert by_cat.loc["Travel", "month"] == "2026-04"     # M-D-Y format also parses
    assert by_cat.loc["Travel", "person"] == "Bob"
    assert by_cat.loc["Groceries", "spend"] == 1234.56    # comma'd amount
    assert by_cat.loc["House", "spend"] == -58.28         # refund stays negative


def test_load_ledger_missing_file_returns_empty():
    df = load_ledger("/no/such/ledger.csv")
    assert df.empty
    assert list(df.columns) == ["person", "month", "category", "spend"]


def test_load_ledger_wrong_schema_returns_empty(tmp_path):
    p = tmp_path / "notledger.csv"
    p.write_text("a,b,c\n1,2,3\n", encoding="utf-8")
    assert load_ledger(p).empty


# ── pivot + filters ────────────────────────────────────────────────────────────

def test_monthly_pivot_groups_category_by_month(tmp_path):
    df = load_ledger(_write_ledger(tmp_path / "ledger.csv"))
    piv = monthly_pivot(df)

    assert sorted(piv.index) == ["Dining Out", "Groceries", "House", "Travel"]
    assert sorted(piv.columns) == ["2026-04", "2026-06"]
    assert piv.loc["Dining Out", "2026-06"] == 80.47
    assert piv.loc["Travel", "2026-04"] == -333.60
    assert piv.loc["Dining Out", "2026-04"] == 0.0        # filled, not NaN


def test_monthly_pivot_person_filter(tmp_path):
    df = load_ledger(_write_ledger(tmp_path / "ledger.csv"))
    piv = monthly_pivot(df, persons=["Bob"])
    assert list(piv.index) == ["Travel"]


def test_monthly_pivot_date_window_excludes_earlier_months(tmp_path):
    df = load_ledger(_write_ledger(tmp_path / "ledger.csv"))
    piv = monthly_pivot(df, date_from="2026-05-01")
    assert list(piv.columns) == ["2026-06"]               # April (Travel) dropped
    assert "Travel" not in piv.index


# ── budget.yaml switch ──────────────────────────────────────────────────────────

def test_budget_without_ledger_key_is_none(tmp_path):
    p = tmp_path / "budget.yaml"
    p.write_text("period: monthly\ngoals:\n  Mortgage: 4403\n", encoding="utf-8")
    assert load_budget(p).resolved_ledger_csv is None


def test_budget_ledger_key_absolute_path_passes_through(tmp_path):
    p = tmp_path / "budget.yaml"
    p.write_text(f"goals: {{}}\nledger:\n  csv: {tmp_path / 'x.csv'}\n", encoding="utf-8")
    assert load_budget(p).resolved_ledger_csv == str(tmp_path / "x.csv")


def test_budget_ledger_relative_path_resolves_from_data_root():
    b = Budget(ledger_csv="spend_analyzer/data/budget_ledger.csv")
    resolved = Path(b.resolved_ledger_csv)
    assert resolved.is_absolute()
    assert resolved == (DATA_ROOT / "spend_analyzer/data/budget_ledger.csv").resolve()
