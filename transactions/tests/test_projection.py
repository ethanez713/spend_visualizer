"""Tests for the raw-transaction -> flat-CSV projection in fetch_transactions."""
import csv
import json
from datetime import date, datetime

import pytest

from conftest import ACCOUNT_META
from src.fetch_transactions import CSV_COLUMNS, _csv_safe, _g, _v, txn_to_row, write_csv


# --- _v: CSV-safe scalar coercion -------------------------------------------

def given_none_when_v_then_empty_string():
    assert _v(None) == ""


def given_bool_when_v_then_passthrough_unchanged():
    # bools must stay bool (not become "1"/"") so the CSV reads True/False.
    assert _v(True) is True
    assert _v(False) is False


def given_date_when_v_then_isoformat_string():
    assert _v(date(2026, 1, 2)) == "2026-01-02"
    assert _v(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05"


def given_plain_scalar_when_v_then_unchanged():
    assert _v(42.5) == 42.5
    assert _v("hello") == "hello"


# --- _g: safe nested get -----------------------------------------------------

def given_none_obj_when_g_then_none():
    assert _g(None, "k") is None


def given_dict_when_g_then_value_or_none():
    assert _g({"k": 1}, "k") == 1
    assert _g({}, "k") is None


# --- _csv_safe: spreadsheet formula-injection guard (regression) -------------

@pytest.mark.parametrize(
    "value, expected",
    [
        ("=SUM(A1)", "'=SUM(A1)"),
        ("+1", "'+1"),
        ("-cmd|/c", "'-cmd|/c"),
        ("@import", "'@import"),
        ("\tx", "'\tx"),
        ("\rx", "'\rx"),
    ],
)
def given_formula_trigger_text_when_csv_safe_then_quote_prefixed(value, expected):
    assert _csv_safe(value) == expected


@pytest.mark.parametrize("value", ["Trader Joe's", "", "USD", "47 Main St"])
def given_ordinary_text_when_csv_safe_then_unchanged(value):
    assert _csv_safe(value) == value


def given_negative_float_when_csv_safe_then_unchanged():
    # Negative amounts are floats, not str -> must NOT be quoted (stays numeric).
    assert _csv_safe(-50.0) == -50.0
    assert _csv_safe(True) is True
    assert _csv_safe(None) is None


# --- txn_to_row: the core projection ----------------------------------------

def given_full_txn_when_projected_then_fields_flattened(make_txn):
    row = txn_to_row(make_txn(), ACCOUNT_META)

    assert row["institution"] == "Chase"
    assert row["txn_owner"] == "u1"
    assert row["account_name"] == "Checking"
    assert row["transaction_id"] == "txn_1"
    assert row["amount"] == 42.5  # numeric preserved, not stringified
    assert row["pf_category_primary"] == "FOOD_AND_DRINK"
    assert row["location_city"] == "Seattle"
    assert row["payment_reference_number"] == "REF1"
    assert row["counterparty_name"] == "Trader Joe's"
    assert row["counterparty_type"] == "merchant"
    assert json.loads(row["counterparties_json"])[0]["entity_id"] == "ent_tj"


def given_counterparty_without_type_when_projected_then_empty_not_none(make_txn):
    # Regression: the literal string "None" used to leak in when type was absent.
    txn = make_txn(counterparties=[{"name": "Acme"}])
    row = txn_to_row(txn, ACCOUNT_META)
    assert row["counterparty_name"] == "Acme"
    assert row["counterparty_type"] == ""


def given_no_counterparties_when_projected_then_empty(make_txn):
    row = txn_to_row(make_txn(counterparties=[]), ACCOUNT_META)
    assert row["counterparty_name"] == ""
    assert row["counterparty_type"] == ""
    assert row["counterparties_json"] == ""


def given_unknown_account_when_projected_then_blank_account_fields(make_txn):
    # No metadata for this account_id -> account columns degrade to "" (not KeyError).
    row = txn_to_row(make_txn(account_id="ghost"), ACCOUNT_META)
    assert row["institution"] == ""
    assert row["account_name"] == ""
    assert row["transaction_id"] == "txn_1"  # txn fields still populated


def given_null_nested_objects_when_projected_then_empty(make_txn):
    txn = make_txn(location=None, payment_meta=None, personal_finance_category=None)
    row = txn_to_row(txn, ACCOUNT_META)
    assert row["location_city"] == ""
    assert row["payment_reference_number"] == ""
    assert row["pf_category_primary"] == ""


# --- write_csv: file output -------------------------------------------------

def _read_csv(path):
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        return reader.fieldnames, list(reader)


def given_rows_when_write_csv_then_header_is_all_columns(state, make_txn):
    write_csv({"txn_1": make_txn()}, ACCOUNT_META)
    fieldnames, rows = _read_csv(state.csv)
    assert fieldnames == CSV_COLUMNS
    assert len(rows) == 1


def given_multiple_dates_when_write_csv_then_sorted_ascending(state, make_txn):
    store = {
        "late": make_txn(transaction_id="late", account_id="acc_1", date="2026-02-01"),
        "early": make_txn(transaction_id="early", account_id="acc_1", date="2026-01-01"),
    }
    write_csv(store, ACCOUNT_META)
    _, rows = _read_csv(state.csv)
    assert [r["date"] for r in rows] == ["2026-01-01", "2026-02-01"]


def given_formula_like_name_when_write_csv_then_escaped_on_disk(state, make_txn):
    store = {"t": make_txn(name="=2+5", amount=-50.0)}
    write_csv(store, ACCOUNT_META)
    _, rows = _read_csv(state.csv)
    assert rows[0]["name"] == "'=2+5"      # text neutralized
    assert rows[0]["amount"] == "-50.0"    # negative amount stays a number
