"""The stdlib xlsx reader: cell values by sheet name, whatever encoding the export used."""
from datetime import date

import pytest

from tests.xlsx_builder import Inline, Rich, make_xlsx
from tools.xlsx_read import excel_date, read_sheet, sheet_names


def given_text_and_numbers_when_read_then_values_keyed_by_row_and_column(tmp_path):
    book = tmp_path / "b.xlsx"
    make_xlsx(book, {"S": [["Source", "Amount"], ["Ethan", 12.5]]})

    assert read_sheet(book, "S") == {1: {"A": "Source", "B": "Amount"}, 2: {"A": "Ethan", "B": 12.5}}


def given_empty_cells_when_read_then_absent_from_row(tmp_path):
    book = tmp_path / "b.xlsx"
    make_xlsx(book, {"S": [["a", None, "c"]]})

    assert read_sheet(book, "S")[1] == {"A": "a", "C": "c"}


def given_rich_text_with_phonetic_run_when_read_then_visible_text_only(tmp_path):
    book = tmp_path / "b.xlsx"
    make_xlsx(book, {"S": [[Rich(("Trader ", "Joe's"))]]})

    assert read_sheet(book, "S")[1]["A"] == "Trader Joe's"


def given_inline_string_when_read_then_text(tmp_path):
    book = tmp_path / "b.xlsx"
    make_xlsx(book, {"S": [[Inline("Café Müller & Söhne")]]})

    assert read_sheet(book, "S")[1]["A"] == "Café Müller & Söhne"


def given_part_files_numbered_against_tab_order_when_read_then_names_resolve(tmp_path):
    book = tmp_path / "b.xlsx"
    make_xlsx(book, {"First": [["one"]], "Second": [["two"]]}, reverse_part_order=True)

    assert read_sheet(book, "First")[1]["A"] == "one"
    assert read_sheet(book, "Second")[1]["A"] == "two"


def given_workbook_when_sheet_names_then_tab_order(tmp_path):
    book = tmp_path / "b.xlsx"
    make_xlsx(book, {"Budget": [["x"]], "2026": [["y"]]})

    assert sheet_names(book) == ["Budget", "2026"]


def given_missing_sheet_when_read_then_error_names_the_sheets_present(tmp_path):
    book = tmp_path / "b.xlsx"
    make_xlsx(book, {"2026": [["y"]]})

    with pytest.raises(KeyError, match="2026"):
        read_sheet(book, "2027")


def given_excel_serial_when_converted_then_calendar_date():
    assert excel_date(46059.0) == date(2026, 2, 6)
