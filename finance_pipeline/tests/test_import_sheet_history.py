"""One-time import of the sheet's year tabs and historic goals into the data root.

Workbooks are synthetic; the only filesystem written is tmp_path.
"""
import csv
import json
import os
import stat
from datetime import date

import pytest

from tests.xlsx_builder import make_xlsx
from tools.import_sheet_history import ledger_record, main
from tools.sheet_ledger import Row, historic_goals

HEADER = ["Source", "Date", "Description", "Category", "Debit", "Credit", "Net", "Note"]


def _book(path):
    make_xlsx(path, {
        "Budget": [
            [], [],
            [None, "Historic Data (Detailed)", 2024.0, None, None, None, 2025.0],
            [None, None, "Actual", "Goal", "% of Budget", "Diff", "Actual", "Goal"],
            [None, "Rent + Parking", 3957.0, 3965.0, None, None, 4016.0, 4083.95],
            [None, "Dining Out", 1445.0, 1100.0, None, None, 773.0, 1133.0],
            [None, "TOTAL", 5402.0, 5065.0],
            [None, "Later row", 1.0, 2.0],
        ],
        "2024": [HEADER, ["Ethan", date(2024, 8, 1), "Rent", "Rent", None, None, 3802.0, None],
                 ["Jordan", date(2024, 8, 2), "=SUM(1,1)", "Other", None, None, -12.5, "see note"]],
        "2025": [HEADER, ["Jordan", date(2025, 1, 2), "Coffee", "Dining Out", None, None, 4.5, None],
                 ["DUMMY", date(2025, 1, 3), None, "Mortgage", None, None, 0.0, None]],
        "Earmarked Spending": [["Item"]],
    })


def _run(tmp_path, *extra):
    book, root = tmp_path / "f.xlsx", tmp_path / "data"
    _book(book)
    code = main(["--xlsx", str(book), "--data-root", str(root), *extra])
    return code, root / "spend_analyzer" / "data"


def _ledger(directory):
    with (directory / "legacy_ledger.csv").open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def given_year_tabs_when_imported_then_every_dated_transaction_is_written_in_ledger_schema(tmp_path):
    code, out = _run(tmp_path)

    rows = _ledger(out)
    assert code == 0 and [r["Description"] for r in rows] == ["Rent", "'=SUM(1,1)", "Coffee"]
    assert rows[0]["Debit Less Credit"] == "$3,802.00" and rows[0]["Date"] == "08/01/2024"
    assert (rows[1]["Credit"], rows[1]["Debit"], rows[1]["Debit Less Credit"]) == ("$12.50", "", "-$12.50")


def given_years_option_when_imported_then_only_those_tabs_are_copied(tmp_path):
    _, out = _run(tmp_path, "--years", "2025")

    assert [r["Description"] for r in _ledger(out)] == ["Coffee"]


def given_year_without_a_tab_when_imported_then_it_stops_naming_the_tabs_present(tmp_path):
    with pytest.raises(SystemExit, match="2023"):
        _run(tmp_path, "--years", "2023")


def given_historic_block_when_imported_then_goals_per_year_with_parking_folded_into_rent(tmp_path):
    _, out = _run(tmp_path)

    assert json.loads((out / "legacy_goals.json").read_text()) == {
        "2024": {"Rent": 3965.0, "Dining Out": 1100.0},
        "2025": {"Rent": 4083.95, "Dining Out": 1133.0}}


def given_import_when_written_then_files_are_owner_only(tmp_path):
    _, out = _run(tmp_path)

    for name in ("legacy_ledger.csv", "legacy_goals.json"):
        assert stat.S_IMODE(os.stat(out / name).st_mode) == 0o600


def given_dry_run_when_imported_then_nothing_is_written(tmp_path, capsys):
    code, out = _run(tmp_path, "--dry-run")

    assert code == 0 and not out.exists()
    assert "3 transactions" in capsys.readouterr().out


def given_existing_legacy_files_when_reimported_then_replaced_not_appended(tmp_path):
    _run(tmp_path)
    book = tmp_path / "f.xlsx"
    main(["--xlsx", str(book), "--data-root", str(tmp_path / "data"), "--years", "2025"])

    assert [r["Description"] for r in _ledger(tmp_path / "data" / "spend_analyzer" / "data")] == ["Coffee"]


def given_workbook_without_a_historic_block_when_read_then_no_goals(tmp_path):
    book = tmp_path / "f.xlsx"
    make_xlsx(book, {"Budget": [[None, "2026 Budget", "Avg", "Goal"]]})

    assert historic_goals(book) == {}


@pytest.mark.parametrize("text", ["=1+1", "+1", "-1", "@x", "\tx", "\rx"])
def given_text_that_starts_like_a_formula_when_written_then_escaped(text):
    row = Row(text, date(2026, 1, 2), text, text, 5.0, text, 1)

    record = ledger_record(row)

    assert [record[i] for i in (0, 2, 3, 8)] == ["'" + text] * 4
