"""Sheet-vs-ledger comparison: matching tiers, windowing, detectors, parsers, and the CLI.

Everything is synthetic: workbooks are built in tmp_path and no real data is read.
"""
import json
import os
import stat
from datetime import date

import pytest

from tests.xlsx_builder import make_xlsx
from tools import sheet_compare as sc
from tools.sheet_ledger import Row, budget_tab, ledger_rows, parse_date, parse_money, year_tab_rows

D = date


def row(day, desc, net, category="Dining Out", source="Ethan", ref=1):
    return Row(source, day, desc, category, net, "", ref)


def _compare(sheet, ledger):
    return sc.compare(sheet, ledger)


def given_identical_transaction_when_compared_then_paired_exactly():
    c = _compare([row(D(2026, 4, 2), "Trader Joe's", 40.0)], [row(D(2026, 4, 2), "Trader Joe's", 40.0)])

    assert [t for _, _, t in c.pairs] == ["exact"]
    assert c.sheet_only == [] and c.ledger_only == []


def given_description_retyped_by_hand_when_compared_then_paired_on_amount_alone():
    c = _compare([row(D(2026, 5, 21), "Gift Fund", 164.33)],
                 [row(D(2026, 5, 22), "Arcade Bar", 164.33)])

    assert [t for _, _, t in c.pairs] == ["amount-only"]


def given_bank_masked_description_when_compared_then_paired_on_amount_and_date():
    c = _compare([row(D(2026, 4, 25), "WF* WAYFAIR1234567890", 163.22)],
                 [row(D(2026, 4, 27), "WF* *****************", 163.22)])

    assert [t for _, _, t in c.pairs] == ["near"]


def given_two_identical_amounts_when_compared_then_each_pairs_with_its_own_counterpart():
    sheet = [row(D(2026, 4, 2), "MTA", 3.0, ref=1), row(D(2026, 4, 9), "MTA", 3.0, ref=2)]
    ledger = [row(D(2026, 4, 9), "MTA", 3.0, ref=7), row(D(2026, 4, 2), "MTA", 3.0, ref=8)]

    c = _compare(sheet, ledger)

    assert sorted((s.date, l.date) for s, l, _ in c.pairs) == [
        (D(2026, 4, 2), D(2026, 4, 2)), (D(2026, 4, 9), D(2026, 4, 9))]


def given_sheet_rows_before_ledger_coverage_when_compared_then_not_reported_missing():
    sheet = [row(D(2026, 1, 5), "Old", 10.0), row(D(2026, 4, 2), "New", 20.0)]
    ledger = [row(D(2026, 4, 2), "New", 20.0)]

    assert _compare(sheet, ledger).sheet_only == []


def given_ledger_rows_after_the_sheets_last_day_when_compared_then_newer_not_unmatched():
    sheet = [row(D(2026, 4, 2), "A", 20.0)]
    ledger = [row(D(2026, 4, 2), "A", 20.0), row(D(2026, 6, 1), "Later", 9.0)]

    c = _compare(sheet, ledger)

    assert [r.desc for r in c.ledger_after_sheet] == ["Later"] and c.ledger_only == []


def given_no_rows_on_a_side_when_compared_then_error():
    with pytest.raises(ValueError):
        sc.compare([], [row(D(2026, 4, 2), "A", 1.0)])


def given_categories_that_differ_when_grouped_then_counted_by_direction():
    sheet = [row(D(2026, 4, i), "Prep Fund", 21.0, "Gifts/Charity", ref=i) for i in (1, 2, 3)]
    ledger = [row(D(2026, 4, i), "Prep Fund", 21.0, "Other", ref=i) for i in (1, 2, 3)]

    (group,) = sc.category_diffs(_compare(sheet, ledger))

    assert (group["ledger_category"], group["sheet_category"], group["rows"]) == (
        "Other", "Gifts/Charity", 3)


def given_card_attributed_to_different_people_when_compared_then_person_diff_counted():
    sheet = [row(D(2026, 6, 2), "A", 5.0, source="Ethan")]
    ledger = [row(D(2026, 6, 2), "A", 5.0, source="Jordan")]

    assert sc.source_diffs(_compare(sheet, ledger)) == {("Jordan", "Ethan"): 1}


def given_pending_charge_exported_again_when_settled_then_flagged_as_duplicate():
    sheet = [row(D(2026, 6, 1), "Anchor", 1.0), row(D(2026, 6, 30), "Amazon", 42.37, ref=1),
             row(D(2026, 7, 1), "Amazon", 42.37, ref=2)]
    ledger = [row(D(2026, 6, 1), "Anchor", 1.0), row(D(2026, 7, 1), "Amazon", 42.37)]

    ((extra, twin),) = sc.duplicate_suspects(_compare(sheet, ledger))

    assert (extra.ref, twin.ref) == (1, 2)


def given_refund_booked_with_opposite_sign_when_compared_then_flagged_as_sign_flip():
    anchors = [row(D(2026, 4, 1), "First", 1.0), row(D(2026, 4, 30), "Last", 2.0)]
    sheet = anchors + [row(D(2026, 4, 13), "United Airlines", -333.6, "Travel")]
    ledger = anchors + [row(D(2026, 4, 15), "United Airlines", 333.6, "Travel")]

    ((s, l),) = sc.sign_flip_suspects(_compare(sheet, ledger))

    assert (s.net, l.net) == (-333.6, 333.6)


def _moved(n, merchant="Corner Bistro", frm="Dining Out", to="Entertainment"):
    sheet = [row(D(2026, 5, i + 1), merchant, 50.0 + i, to, ref=i) for i in range(n)]
    ledger = [row(D(2026, 5, i + 1), merchant, 50.0 + i, frm, ref=i) for i in range(n)]
    return sheet, ledger


def given_merchant_always_moved_the_same_way_when_three_rows_then_rule_candidate():
    candidates, contested = sc.rule_suggestions(_compare(*_moved(3)))

    assert [(e["merchant"], e["rows"]) for e in candidates] == [("corner bistro", 3)] and contested == []


def given_merchant_moved_in_only_two_rows_when_suggesting_then_nothing_yet():
    assert sc.rule_suggestions(_compare(*_moved(2))) == ([], [])


def given_merchant_with_an_agreeing_row_when_suggesting_then_contested_not_candidate():
    sheet, ledger = _moved(3)
    sheet.append(row(D(2026, 5, 20), "Corner Bistro", 12.0, "Dining Out", ref=9))
    ledger.append(row(D(2026, 5, 20), "Corner Bistro", 12.0, "Dining Out", ref=9))

    candidates, contested = sc.rule_suggestions(_compare(sheet, ledger))

    assert candidates == [] and contested[0]["agreeing_rows"] == 1


def given_window_inside_months_when_listing_complete_months_then_partial_ends_dropped():
    assert sc.complete_months(D(2026, 3, 9), D(2026, 9, 28)) == [
        "2026-04", "2026-05", "2026-06", "2026-07", "2026-08"]


def given_window_spanning_new_year_when_listing_complete_months_then_december_and_january():
    assert sc.complete_months(D(2025, 12, 1), D(2026, 1, 31)) == ["2025-12", "2026-01"]


def given_a_partial_month_when_averaging_then_present_and_complete_modes_differ():
    rows = [row(D(2026, 4, 5), "A", 300.0), row(D(2026, 5, 5), "A", 100.0),
            row(D(2026, 6, 2), "A", 100.0)]

    assert sc.monthly_averages(rows, None) == {"Dining Out": 500.0 / 3}
    assert sc.monthly_averages(rows, ["2026-04", "2026-05"]) == {"Dining Out": 200.0}


def given_money_in_sheet_and_csv_notations_when_parsed_then_signed_floats():
    assert parse_money("-$58.28") == -58.28
    assert parse_money("$1,234.50") == 1234.5
    assert parse_money("(5.00)") == -5.0
    assert parse_money("'-12.5") == -12.5
    assert parse_money("") is None and parse_money(True) is None


def given_serial_slash_and_iso_dates_when_parsed_then_same_calendar_day():
    assert parse_date(46126.0) == parse_date("4/14/2026") == parse_date("2026-04-14") == D(2026, 4, 14)
    assert parse_date("not a date") is None


def _year_book(path):
    make_xlsx(path, {
        "Budget": [[], ["", "2026 Budget", "2026 Running Avg. Expenses", "Goal"],
                   ["", "Mortgage", 4500.0, 4500.0], ["", "Dining Out", 1162.0, 1167.0],
                   ["", "Total", 5662.0, 5667.0]],
        "2026": [["Source", "Date", "Description", "Category", "Debit", "Credit", "Net", "Note"],
                 ["Ethan", D(2026, 4, 2), "Trader Joe's", "Groceries", None, None, 40.0, "Europe"],
                 ["DUMMY", D(2026, 1, 1), None, "Mortgage", 0.0, None, 0.0],
                 ["Jordan", "4/14/2026", "Café 'Chez Jordan'", "Dining Out", None, None, "-12.50"],
                 ["Jordan", None, "no date", "Dining Out", None, None, 9.0]],
    })


def given_year_tab_when_read_then_dated_nonzero_rows_with_their_fields(tmp_path):
    book = tmp_path / "f.xlsx"
    _year_book(book)

    rows = year_tab_rows(book, 2026)

    assert [(r.source, r.date, r.desc, r.net, r.note) for r in rows] == [
        ("Ethan", D(2026, 4, 2), "Trader Joe's", 40.0, "Europe"),
        ("Jordan", D(2026, 4, 14), "Café 'Chez Jordan'", -12.5, "")]


def given_budget_tab_when_read_then_categories_with_average_and_goal(tmp_path):
    book = tmp_path / "f.xlsx"
    _year_book(book)

    assert budget_tab(book) == [("Mortgage", 4500.0, 4500.0), ("Dining Out", 1162.0, 1167.0)]


LEDGER_HEADER = "Source,Date,Description,Category,Debit,Credit,Debit Less Credit,Account,Note\n"


def _ledger_csv(path, *lines):
    path.write_text(LEDGER_HEADER + "\n".join(lines) + "\n", encoding="utf-8")


def given_ledger_csv_when_read_then_rows_parsed_and_warning_rows_counted(tmp_path):
    csv_path = tmp_path / "l.csv"
    _ledger_csv(csv_path,
                "Ethan,04/02/2026,Trader Joe's,Groceries,$40.00,,$40.00,Visa (1111),",
                "Jordan,04/03/2026,'=SUM(A1),Other,$5.00,,$5.00,,PENDING",
                ",04/04/2026,Old row,Other,,,,,⚠ REMOVED SINCE LAST EXECUTION",
                "Ethan,not-a-date,Bad,Other,$1.00,,$1.00,,")

    rows, warnings = ledger_rows(csv_path)

    assert [(r.desc, r.net, r.note) for r in rows] == [
        ("Trader Joe's", 40.0, ""), ("=SUM(A1)", 5.0, "PENDING")] and warnings == 1


def given_workbook_and_ledger_when_cli_runs_then_report_printed_and_findings_written_private(
        tmp_path, capsys):
    book, data_root = tmp_path / "f.xlsx", tmp_path / "data"
    _year_book(book)
    ledger = tmp_path / "l.csv"
    _ledger_csv(ledger, "Ethan,04/02/2026,Trader Joe's,Groceries,$40.00,,$40.00,Visa (1111),",
                "Jordan,04/14/2026,Cafe Chez Jordan,Entertainment,,$12.50,-$12.50,Visa (1111),")

    assert sc.main(["--xlsx", str(book), "--year", "2026", "--ledger", str(ledger),
                    "--data-root", str(data_root)]) == 0

    printed = capsys.readouterr().out
    assert "paired 2" in printed and "Dining Out -> " not in printed
    (written,) = list((data_root / "sheet_compare").glob("*.jsonl"))
    kinds = [json.loads(line)["kind"] for line in written.read_text().splitlines()]
    assert kinds[0] == "summary" and "category_diff" in kinds
    assert stat.S_IMODE(os.stat(written).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(written.parent).st_mode) == 0o700


def given_no_write_flag_when_cli_runs_then_nothing_is_written(tmp_path, capsys):
    book, data_root = tmp_path / "f.xlsx", tmp_path / "data"
    _year_book(book)
    ledger = tmp_path / "l.csv"
    _ledger_csv(ledger, "Ethan,04/02/2026,Trader Joe's,Groceries,$40.00,,$40.00,,")

    sc.main(["--xlsx", str(book), "--year", "2026", "--ledger", str(ledger),
             "--data-root", str(data_root), "--no-write"])

    assert not (data_root / "sheet_compare").exists()
