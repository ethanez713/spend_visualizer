"""The budget-row export the pipeline's three-way compare reads.

Synthetic archive only: pending rows, excluded rows and household edits are the
cases that change which rows and categories come out.
"""
import csv
import json
import os
import stat

import pandas as pd

import budget_edits
from config_io import CONFIG_DIR, AppConfig
from budget_model import load_budget_model
from tools.export_budget_rows import export, ledger_table


def _txn(tid, amount, posted, name, detailed, primary, pending=False, authorized=None):
    return {
        "transaction_id": tid,
        "account_id": "acct_alex_cc",
        "amount": amount,
        "date": posted,
        "authorized_date": authorized or posted,
        "iso_currency_code": "USD",
        "name": name.upper(),
        "merchant_name": name,
        "merchant_entity_id": "ent_" + tid,
        "payment_channel": "in store",
        "personal_finance_category": {
            "primary": primary, "detailed": detailed, "confidence_level": "HIGH",
        },
        "txn_owner": "Alex Rivera",
        "pending": pending,
    }


def test_given_pending_excluded_and_edited_rows_when_exported_then_only_settled_budget_rows(
        tmp_path, monkeypatch):
    # The transformer's import resolves the data root. Point it at this tmp dir before that
    # import so the test never stats the real finance_data tree.
    monkeypatch.setenv("SPEND_VISUALIZER_DATA", str(tmp_path))
    grocery = _txn("grocery", 40.0, "2026-04-05", "Green Valley Market",
                   "FOOD_AND_DRINK_GROCERIES", "FOOD_AND_DRINK", authorized="2026-04-02")
    pending = _txn("pending", 22.0, "2026-04-06", "Pending Cafe",
                   "FOOD_AND_DRINK_RESTAURANT", "FOOD_AND_DRINK", pending=True)
    coffee = _txn("coffee", 5.0, "2026-04-03", "Daily Grind",
                  "FOOD_AND_DRINK_COFFEE", "FOOD_AND_DRINK")
    kettle = _txn("kettle", 18.0, "2026-04-04", "Copper Kettle",
                  "FOOD_AND_DRINK_RESTAURANT", "FOOD_AND_DRINK")
    zelle = _txn("zelle", 100.0, "2026-04-08", "Zelle to Landlord",
                 "TRANSFER_OUT_ACCOUNT_TRANSFER", "TRANSFER_OUT")
    archive = tmp_path / "transactions_categorized.jsonl"
    archive.write_text("".join(json.dumps(row) + "\n"
                               for row in (grocery, pending, coffee, kettle, zelle)),
                       encoding="utf-8")
    log = tmp_path / "manual_edits.jsonl"
    edits = [budget_edits.build_edit(coffee, scope="transaction", exclude=True),
             budget_edits.build_edit(kettle, scope="transaction", category="Travel", note="=trip"),
             budget_edits.build_edit(zelle, scope="transaction", category="Rent")]
    log.write_text("".join(json.dumps(edit) + "\n" for edit in edits), encoding="utf-8")
    accounts = tmp_path / "accounts.yaml"
    accounts.write_text(
        "accounts:\n  acct_alex_cc: {person: Alex Rivera, name: Travel Rewards Card, type: credit}\n",
        encoding="utf-8")
    out = tmp_path / "budget_rows.csv"

    export(out, app=AppConfig(archive_paths=[str(archive)]), accounts_path=accounts,
           budget_model=load_budget_model(CONFIG_DIR / "budget_model.yaml", tmp_path / "no.yaml"),
           mirror_csv=tmp_path / "no-mirror.csv", edits_path=str(log))

    with out.open(encoding="utf-8", newline="") as handle:
        loaded = list(csv.DictReader(handle))
    assert list(loaded[0]) == ["Source", "Date", "Description", "Category", "Debit", "Credit",
                               "Debit Less Credit", "Account", "Note"]
    by_desc = {row["Description"]: row for row in loaded}
    assert set(by_desc) == {"Green Valley Market", "Copper Kettle", "Zelle to Landlord"}
    market = by_desc["Green Valley Market"]
    assert market["Date"] == "04/02/2026" and market["Category"] == "Groceries"
    assert market["Source"] == "Alex Rivera" and market["Account"] == "Travel Rewards Card"
    assert (market["Debit"], market["Credit"], market["Debit Less Credit"]) == (
        "$40.00", "", "$40.00")
    assert by_desc["Copper Kettle"]["Category"] == "Travel"
    assert by_desc["Copper Kettle"]["Note"] == "'=trip"
    assert by_desc["Zelle to Landlord"]["Category"] == "Rent"
    assert by_desc["Zelle to Landlord"]["Debit Less Credit"] == "$100.00"
    assert stat.S_IMODE(out.stat().st_mode) == 0o600


def test_given_a_refund_when_exported_then_credit_and_signed_net_are_formula_safe():
    frame = pd.DataFrame([{
        "transaction_id": "r", "pending": False, "budget_cat": "Travel",
        "budget_amount": -18.5, "date_resolved": "2026-04-02", "person": "Alex",
        "description": "SkyHigh Airways", "account_name": "Card", "budget_note": "",
    }])

    row = ledger_table(frame).iloc[0]

    assert row["Debit"] == "" and row["Credit"] == "$18.50"
    assert row["Debit Less Credit"] == "'-$18.50"
    assert row["Date"] == "04/02/2026" and row["Category"] == "Travel" and row["Source"] == "Alex"
