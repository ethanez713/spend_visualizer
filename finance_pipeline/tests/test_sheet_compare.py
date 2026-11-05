"""Sheet-vs-ledger comparison: matching tiers, windowing, detectors, parsers, and the CLI.

Everything is synthetic: workbooks are built in tmp_path and no real data is read.
"""
import json
import os
import stat
from datetime import date, datetime

import pytest

from tests.xlsx_builder import make_xlsx
from tools import sheet_compare as sc
from tools.sheet_ledger import Row, budget_tab, ledger_rows, parse_date, parse_money, year_tab_rows

D = date


def row(day, desc, net, category="Dining Out", source="Bob", ref=1):
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
    sheet = [row(D(2026, 6, 2), "A", 5.0, source="Bob")]
    ledger = [row(D(2026, 6, 2), "A", 5.0, source="Alice")]

    assert sc.source_diffs(_compare(sheet, ledger)) == {("Alice", "Bob"): 1}


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


def _moved(n, merchant="Sing Along Bar", frm="Dining Out", to="Entertainment"):
    sheet = [row(D(2026, 5, i + 1), merchant, 50.0 + i, to, ref=i) for i in range(n)]
    ledger = [row(D(2026, 5, i + 1), merchant, 50.0 + i, frm, ref=i) for i in range(n)]
    return sheet, ledger


def given_merchant_always_moved_the_same_way_when_three_rows_then_rule_candidate():
    candidates, contested = sc.rule_suggestions(_compare(*_moved(3)))

    assert [(e["merchant"], e["rows"]) for e in candidates] == [("sing along bar", 3)] and contested == []


def given_merchant_moved_in_only_two_rows_when_suggesting_then_nothing_yet():
    assert sc.rule_suggestions(_compare(*_moved(2))) == ([], [])


def given_merchant_with_an_agreeing_row_when_suggesting_then_contested_not_candidate():
    sheet, ledger = _moved(3)
    sheet.append(row(D(2026, 5, 20), "Sing Along Bar", 12.0, "Dining Out", ref=9))
    ledger.append(row(D(2026, 5, 20), "Sing Along Bar", 12.0, "Dining Out", ref=9))

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
                 ["Bob", D(2026, 4, 2), "Trader Joe's", "Groceries", None, None, 40.0, "Europe"],
                 ["DUMMY", D(2026, 1, 1), None, "Mortgage", 0.0, None, 0.0],
                 ["Alice", "4/14/2026", "Café 'Chez Alice'", "Dining Out", None, None, "-12.50"],
                 ["Alice", None, "no date", "Dining Out", None, None, 9.0]],
    })


def given_year_tab_when_read_then_dated_nonzero_rows_with_their_fields(tmp_path):
    book = tmp_path / "f.xlsx"
    _year_book(book)

    rows = year_tab_rows(book, 2026)

    assert [(r.source, r.date, r.desc, r.net, r.note) for r in rows] == [
        ("Bob", D(2026, 4, 2), "Trader Joe's", 40.0, "Europe"),
        ("Alice", D(2026, 4, 14), "Café 'Chez Alice'", -12.5, "")]


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
                "Bob,04/02/2026,Trader Joe's,Groceries,$40.00,,$40.00,Visa (1111),",
                "Alice,04/03/2026,'=SUM(A1),Other,$5.00,,$5.00,,PENDING",
                ",04/04/2026,Old row,Other,,,,,⚠ REMOVED SINCE LAST EXECUTION",
                "Bob,not-a-date,Bad,Other,$1.00,,$1.00,,")

    rows, warnings = ledger_rows(csv_path)

    assert [(r.desc, r.net, r.note) for r in rows] == [
        ("Trader Joe's", 40.0, ""), ("=SUM(A1)", 5.0, "PENDING")] and warnings == 1


def given_workbook_and_ledger_when_cli_runs_then_report_printed_and_findings_written_private(
        tmp_path, capsys):
    book, data_root = tmp_path / "f.xlsx", tmp_path / "data"
    _year_book(book)
    ledger = tmp_path / "l.csv"
    _ledger_csv(ledger, "Bob,04/02/2026,Trader Joe's,Groceries,$40.00,,$40.00,Visa (1111),",
                "Alice,04/14/2026,Cafe Chez Alice,Entertainment,,$12.50,-$12.50,Visa (1111),")

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
    _ledger_csv(ledger, "Bob,04/02/2026,Trader Joe's,Groceries,$40.00,,$40.00,,")

    sc.main(["--xlsx", str(book), "--year", "2026", "--ledger", str(ledger),
             "--data-root", str(data_root), "--no-write"])

    assert not (data_root / "sheet_compare").exists()


ANCHORS = [
    ("Bob", D(2026, 4, 1), "Anchor", "Rent", 1.0),
    ("Bob", D(2026, 4, 30), "End", "Rent", 2.0),
]


def _workbook(path, years):
    sheets = {}
    for year, rows in years.items():
        grid = [["Source", "Date", "Description", "Category", "Debit", "Credit", "Net", "Note"]]
        for source, day, desc, cat, net in rows:
            grid.append([source, day, desc, cat, None, None, float(net), ""])
        sheets[str(year)] = grid
    make_xlsx(path, sheets)


def _csv_line(source, day, desc, cat, net, note=""):
    debit = f"${net:,.2f}" if net > 0 else ""
    credit = f"${-net:,.2f}" if net < 0 else ""
    signed = f"-${-net:,.2f}" if net < 0 else f"${net:,.2f}"
    return f"{source},{day:%m/%d/%Y},{desc},{cat},{debit},{credit},{signed},,{note}"


def _lines(rows):
    return [_csv_line(*row) for row in rows]


def _touch(path, day):
    stamp = datetime(day.year, day.month, day.day, 12).timestamp()
    os.utime(path, (stamp, stamp))


def _run_three(tmp_path, capsys, years, plaid_lines, app_lines, *, mtime=None, extra=(),
               snapshots=None, downloads=False):
    data = tmp_path / "data"
    book_dir = tmp_path / "downloads" if downloads else tmp_path
    book_dir.mkdir(parents=True, exist_ok=True)
    book = book_dir / "Financials.xlsx"
    _workbook(book, years)
    if mtime is not None:
        _touch(book, mtime)
    plaid_dir = tmp_path / "snapshots"
    plaid_dir.mkdir()
    for name, lines in snapshots or [("one.head.csv", plaid_lines)]:
        _ledger_csv(plaid_dir / name, *lines)
    app = tmp_path / "app.csv"
    _ledger_csv(app, *app_lines)
    argv = ["--three-way", "--app-csv", str(app), "--plaid-dir", str(plaid_dir),
            "--data-root", str(data), *extra]
    if downloads:
        argv.extend(["--downloads", str(book_dir)])
    else:
        argv.extend(["--xlsx", str(book)])
    code = sc.main(argv)
    printed = capsys.readouterr().out
    folder = data / "sheet_compare"
    written = sorted(folder.glob("*.jsonl")) if folder.exists() else []
    found = [json.loads(line) for line in written[0].read_text().splitlines()] if written else []
    return code, printed, found, written


def given_row_only_on_the_master_when_three_way_then_only_one_side_finding(tmp_path, capsys):
    master = ANCHORS + [("Bob", D(2026, 4, 15), "Only Here", "Groceries", 20.0)]
    code, printed, found, written = _run_three(
        tmp_path, capsys, {2026: master}, _lines(ANCHORS), _lines(ANCHORS))

    assert code == 0
    assert "M-P  only_one_side 1  category_disagreement 0  total_diff 1" in printed
    assert "M-A  only_one_side 1  category_disagreement 0  total_diff 1" in printed
    assert "P-A  only_one_side 0  category_disagreement 0  total_diff 0" in printed
    assert {(row["pair"], row["side"], row["description"])
            for row in found if row["kind"] == "only_one_side"} == {
                ("M-P", "M", "Only Here"), ("M-A", "M", "Only Here")}
    assert stat.S_IMODE(os.stat(written[0]).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(written[0].parent).st_mode) == 0o700


def given_paired_rows_in_different_categories_when_three_way_then_category_disagreement(
        tmp_path, capsys):
    master = ANCHORS + [("Bob", D(2026, 4, 15), "Cafe", "Dining Out", 40.0)]
    plaid = ANCHORS + [("Bob", D(2026, 4, 15), "Cafe", "Entertainment", 40.0)]
    _code, printed, found, _written = _run_three(
        tmp_path, capsys, {2026: master}, _lines(plaid), _lines(master))

    disagree = [row for row in found if row["kind"] == "category_disagreement" and row["pair"] == "M-P"]
    assert [(row["left_category"], row["right_category"], row["net"]) for row in disagree] == [
        ("Dining Out", "Entertainment", 40.0)]
    assert "M-P  only_one_side 0  category_disagreement 1  total_diff 2" in printed


def given_category_month_off_by_a_dollar_when_three_way_then_no_total_finding(tmp_path, capsys):
    master = ANCHORS + [("Bob", D(2026, 4, 15), "Cafe", "Groceries", 10.0)]
    plaid = ANCHORS + [("Bob", D(2026, 4, 15), "Cafe", "Groceries", 11.0)]
    _code, printed, _found, _written = _run_three(
        tmp_path, capsys, {2026: master}, _lines(plaid), _lines(master))

    assert "M-P  only_one_side 2  category_disagreement 0  total_diff 0" in printed
    assert "P-A  only_one_side 2  category_disagreement 0  total_diff 0" in printed


def given_category_month_off_by_more_than_a_dollar_when_three_way_then_total_finding(
        tmp_path, capsys):
    master = ANCHORS + [("Bob", D(2026, 4, 15), "Cafe", "Groceries", 10.0)]
    plaid = ANCHORS + [("Bob", D(2026, 4, 15), "Cafe", "Groceries", 11.01)]
    _code, printed, found, _written = _run_three(
        tmp_path, capsys, {2026: master}, _lines(plaid), _lines(master))

    diffs = [row for row in found if row["kind"] == "total_diff" and row["pair"] == "M-P"]
    assert len(diffs) == 1
    assert diffs[0]["category"] == "Groceries" and diffs[0]["month"] == "2026-04"
    assert diffs[0]["left"] == 10
    assert diffs[0]["right"] == pytest.approx(11.01)
    assert diffs[0]["difference"] == pytest.approx(-1.01)
    assert "M-P  only_one_side 2  category_disagreement 0  total_diff 1" in printed


def given_many_total_differences_when_three_way_then_five_largest_are_printed(tmp_path, capsys):
    extra = [("Bob", D(2026, 4, 15), f"Band{amount}", f"Band{amount}", float(amount))
             for amount in (10, 20, 30, 40, 50, 60)]
    _code, printed, _found, _written = _run_three(
        tmp_path, capsys, {2026: ANCHORS + extra}, _lines(ANCHORS), _lines(ANCHORS))

    detail = printed.split("Largest total differences\n", 1)[1].split("\n\n", 1)[0].splitlines()
    assert detail == [
        "  M-A  Band60  2026-04  60.00 vs 0.00  (+60.00)",
        "  M-P  Band60  2026-04  60.00 vs 0.00  (+60.00)",
        "  M-A  Band50  2026-04  50.00 vs 0.00  (+50.00)",
        "  M-P  Band50  2026-04  50.00 vs 0.00  (+50.00)",
        "  M-A  Band40  2026-04  40.00 vs 0.00  (+40.00)",
    ]


def given_several_downloads_when_three_way_then_newest_financials_workbook_is_used(tmp_path, capsys):
    downloads = tmp_path / "dl"
    downloads.mkdir()
    old, new, other = (downloads / "Financials_old.xlsx", downloads / "Financials_new.xlsx",
                       downloads / "Budget.xlsx")
    _workbook(old, {2026: ANCHORS + [("Bob", D(2026, 4, 15), "Old Book Only", "Groceries", 20.0)]})
    _workbook(new, {2026: ANCHORS})
    _workbook(other, {2026: ANCHORS + [("Bob", D(2026, 4, 15), "Not A Financials", "Groceries", 20.0)]})
    _touch(old, D(2026, 1, 1))
    _touch(new, D(2026, 2, 1))
    _touch(other, D(2026, 3, 1))
    plaid_dir, app, data = tmp_path / "snapshots", tmp_path / "app.csv", tmp_path / "data"
    plaid_dir.mkdir()
    _ledger_csv(plaid_dir / "one.head.csv", *_lines(ANCHORS))
    _ledger_csv(app, *_lines(ANCHORS))

    code = sc.main(["--three-way", "--downloads", str(downloads), "--app-csv", str(app),
                    "--plaid-dir", str(plaid_dir), "--data-root", str(data)])
    printed = capsys.readouterr().out

    assert code == 0
    assert "Financials_new.xlsx modified 2026-02-01" in printed
    assert "M-P  only_one_side 0  category_disagreement 0  total_diff 0" in printed
    assert "Old Book Only" not in printed and "Not A Financials" not in printed


def given_no_financials_workbook_when_three_way_then_prints_the_search_path_and_exits_zero(
        tmp_path, capsys):
    downloads = tmp_path / "empty-downloads"
    downloads.mkdir()

    code = sc.main(["--three-way", "--downloads", str(downloads), "--data-root", str(tmp_path / "data"),
                    "--no-write"])

    printed = capsys.readouterr().out
    assert code == 0
    assert printed.strip() == f"no Financials workbook in {downloads} (looked for Financials*.xlsx)"
    assert not (tmp_path / "data" / "sheet_compare").exists()


def given_workbook_more_than_a_week_behind_when_three_way_then_stale_master_line(tmp_path, capsys):
    _code, printed, _found, _written = _run_three(
        tmp_path, capsys, {2026: ANCHORS}, _lines(ANCHORS), _lines(ANCHORS), mtime=D(2026, 4, 22))

    assert "modified 2026-04-22" in printed
    assert "master is 8 days old" in printed
    assert "M-P  only_one_side 0  category_disagreement 0  total_diff 0" in printed


def given_workbook_a_week_behind_when_three_way_then_no_stale_line(tmp_path, capsys):
    _code, printed, _found, _written = _run_three(
        tmp_path, capsys, {2026: ANCHORS}, _lines(ANCHORS), _lines(ANCHORS), mtime=D(2026, 4, 23))

    assert "modified 2026-04-23" in printed
    assert "master is" not in printed


def given_pending_and_warning_notes_when_three_way_then_those_plaid_rows_are_dropped(
        tmp_path, capsys):
    plaid = _lines(ANCHORS) + [
        _csv_line("Bob", D(2026, 4, 15), "Pending Shop", "Dining Out", 9.0, "PENDING"),
        _csv_line("Bob", D(2026, 4, 16), "Stale Row", "Dining Out", 8.0, "⚠ REMOVED SINCE LAST EXECUTION"),
    ]
    _code, printed, found, _written = _run_three(
        tmp_path, capsys, {2026: ANCHORS}, plaid, _lines(ANCHORS))

    assert "M-P  only_one_side 0  category_disagreement 0  total_diff 0" in printed
    assert "Pending Shop" not in printed and "Stale Row" not in json.dumps(found)


def given_two_year_tabs_when_three_way_then_both_years_are_compared(tmp_path, capsys):
    rows = {2025: [("Bob", D(2025, 6, 1), "Last Year", "Groceries", 15.0)], 2026: ANCHORS}
    lines = _lines(rows[2025] + rows[2026])
    _code, printed, _found, _written = _run_three(tmp_path, capsys, rows, lines, lines)

    assert "M-P  only_one_side 0  category_disagreement 0  total_diff 0" in printed


def given_two_snapshot_files_when_three_way_then_rows_from_both_are_compared(tmp_path, capsys):
    north = ("Bob", D(2026, 4, 10), "North Shop", "Groceries", 15.0)
    south = ("Bob", D(2026, 4, 20), "South Shop", "Groceries", 16.0)
    everyone = ANCHORS + [north, south]
    _code, printed, _found, _written = _run_three(
        tmp_path, capsys, {2026: everyone}, [], _lines(everyone),
        snapshots=[("a.head.csv", _lines(ANCHORS + [north])), ("b.head.csv", _lines([south]))])

    assert "M-P  only_one_side 0  category_disagreement 0  total_diff 0" in printed


def given_no_write_when_three_way_then_nothing_is_written(tmp_path, capsys):
    _code, _printed, _found, written = _run_three(
        tmp_path, capsys, {2026: ANCHORS}, _lines(ANCHORS), _lines(ANCHORS), extra=("--no-write",))

    assert written == []


def given_plaid_covering_one_month_and_app_covering_two_years_when_three_way_then_earlier_app_rows_are_not_findings(
        tmp_path, capsys):
    june = [
        ("Bob", D(2026, 6, 1), "Boundary Cafe", "Dining Out", 12.0),
        ("Bob", D(2026, 6, 15), "Mid Groceries", "Groceries", 20.0),
        ("Bob", D(2026, 6, 30), "End", "Rent", 2.0),
    ]
    # Authorized date six days before the posted date, inside the edge slack.
    app = [
        ("Bob", D(2024, 7, 4), "Summer Market", "Groceries", 25.0),
        ("Bob", D(2025, 1, 15), "Winter Rent", "Rent", 100.0),
        ("Bob", D(2025, 11, 20), "November Shop", "Dining Out", 18.0),
        ("Bob", D(2026, 5, 26), "Boundary Cafe", "Dining Out", 12.0),
        ("Bob", D(2026, 6, 15), "Mid Groceries", "Groceries", 20.0),
        ("Bob", D(2026, 6, 20), "June Extra", "Groceries", 30.0),
        ("Bob", D(2026, 6, 30), "End", "Rent", 2.0),
    ]
    _code, _printed, found, _written = _run_three(
        tmp_path, capsys, {2026: june}, _lines(june), _lines(app))

    early_only = [row for row in found if row["kind"] == "only_one_side" and row["side"] == "A"
                  and row["date"] < "2026-06-01"]
    early_totals = [row for row in found if row["kind"] == "total_diff" and row["month"] < "2026-06"]
    assert early_only == [] and early_totals == []
    assert not any(row["kind"] == "only_one_side" and row["description"] == "Boundary Cafe"
                   for row in found)
    assert any(row["kind"] == "only_one_side" and row["side"] == "A"
               and row["description"] == "June Extra" for row in found)


def given_pair_dates_do_not_overlap_when_three_way_then_pair_is_skipped(tmp_path, capsys):
    master = [
        ("Bob", D(2026, 4, 15), "April Bill", "Rent", 10.0),
        ("Bob", D(2026, 5, 20), "May Bill", "Rent", 11.0),
    ]
    plaid = [
        ("Bob", D(2026, 4, 1), "April Bill", "Rent", 10.0),
        ("Bob", D(2026, 4, 30), "April End", "Rent", 2.0),
    ]
    app = [
        ("Bob", D(2026, 5, 5), "May Bill", "Rent", 11.0),
        ("Bob", D(2026, 5, 31), "May End", "Rent", 2.0),
    ]
    code, printed, found, _written = _run_three(
        tmp_path, capsys, {2026: master}, _lines(plaid), _lines(app))

    assert code == 0
    assert printed.count("P-A skipped (no overlap)") == 1
    assert "M-P skipped" not in printed and "M-A skipped" not in printed
    assert not any(row["pair"] == "P-A" for row in found)
