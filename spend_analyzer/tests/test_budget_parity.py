"""The budget model must agree with the converter's PFC -> budget mapping.

Both are fed the same synthetic categorized rows (every taxonomy atom, money in and out,
a spread of merchant descriptions) and must give each row the same category and net
amount. Skipped when no converter is configured. ``_converter_importable`` runs the
converter's real ``transform``, so a change to its policy shows up here.
"""
import importlib
import sys

import pandas as pd
import pytest
import yaml

from budget_model import annotate, load_budget_model
from config_io import CONFIG_DIR, PERSONAL_CONFIG_DIR
from enrich import enrich
from ingest.normalize import normalize_one
from rules_bridge import converter_dir, converter_rules
from taxonomy import load_taxonomy

PRIMARIES = ["INCOME", "TRANSFER_IN", "TRANSFER_OUT", "LOAN_PAYMENTS", "BANK_FEES",
             "ENTERTAINMENT", "FOOD_AND_DRINK", "GENERAL_MERCHANDISE", "HOME_IMPROVEMENT",
             "MEDICAL", "PERSONAL_CARE", "GENERAL_SERVICES", "GOVERNMENT_AND_NON_PROFIT",
             "TRANSPORTATION", "TRAVEL", "RENT_AND_UTILITIES", "OTHER"]

DESCRIPTIONS = ["Acme Corp", "Uber Eats", "UBER TRIP", "DoorDash", "Lyft", "Lime", "Sublime Cafe",
                "WF ******************", "WF* ****", "WFM Whole Foods", "T-Mobile", "Spotify",
                "MBTA Charlie", "Apple Music", "Hubert's Diner", "Transit Authority", "Disney+"]


@pytest.fixture(scope="module")
def bridge():
    root = converter_dir()
    if root is None or not (root / "src" / "plaid_bridge.py").is_file():
        pytest.skip("no converter configured")
    before, src = set(sys.modules), str(root / "src")
    sys.path.insert(0, src)
    try:
        module = importlib.import_module("plaid_bridge")
    except Exception as exc:  # noqa: BLE001 - an optional external project
        pytest.skip(f"converter not importable here: {exc}")
    yield module
    sys.path.remove(src)
    for name in set(sys.modules) - before:
        del sys.modules[name]


def _primary(atom):
    return max((p for p in PRIMARIES if atom.startswith(p + "_")), key=len)


def _rows():
    atoms = yaml.safe_load((CONFIG_DIR / "taxonomy.yaml").read_text(encoding="utf-8"))["atoms"]
    rows, n = [], 0
    for atom in atoms:
        for amount in (10.0, -10.0):
            for description in DESCRIPTIONS:
                n += 1
                rows.append({"transaction_id": f"t{n}", "account_id": "a", "amount": amount,
                             "date": "2026-05-01", "merchant_name": description, "name": "RAW",
                             "pending": False, "pf_primary": _primary(atom), "pf_detailed": atom})
    for variant, fields in enumerate([{"merchant_name": None, "name": "UBER EATS 1234"},
                                      {"merchant_name": None, "name": None,
                                       "original_description": "T-MOBILE BILL"},
                                      {"merchant_name": "", "name": "Lyft Ride"}]):
        rows.append({"transaction_id": f"fallback{variant}", "account_id": "a", "amount": 10.0,
                     "date": "2026-05-01", "pending": False,
                     "pf_primary": "GENERAL_SERVICES", "pf_detailed": "GENERAL_SERVICES_OTHER_GENERAL_SERVICES",
                     **fields})
    return rows


def _raw(row):
    return {"transaction_id": row["transaction_id"], "account_id": row["account_id"],
            "amount": row["amount"], "date": row["date"], "pending": row["pending"],
            "name": row.get("name"), "merchant_name": row.get("merchant_name"),
            "original_description": row.get("original_description"),
            "personal_finance_category": {"primary": row["pf_primary"],
                                          "detailed": row["pf_detailed"],
                                          "confidence_level": "HIGH"}}


def _analyzer_categories(model):
    txns = [normalize_one(_raw(r)) for r in _rows()]
    df = annotate(enrich(txns, load_taxonomy(), {}), model)
    spent = df[df["budget_cat"].notna()]
    return {tid: (cat, round(float(s), 2))
            for tid, cat, s in zip(spent["transaction_id"], spent["budget_cat"],
                                   spent["budget_amount"])}


def _converter_categories(bridge):
    frame = pd.DataFrame([{**r, "pf_category_primary": r["pf_primary"],
                           "pf_category_detailed": r["pf_detailed"],
                           "original_description": r.get("original_description"),
                           "amount": str(r["amount"]), "pending": "False"} for r in _rows()])
    ledger, _, _ = bridge.transform(frame)
    return {tid: (cat, round(float(net.replace("$", "").replace(",", "")), 2))
            for tid, cat, net in zip(ledger["_id"], ledger["Category"], ledger["Debit Less Credit"])}


def _model_with_converter_pins(tmp_path):
    pins = converter_rules()["pinned"]
    personal = tmp_path / "budget.yaml"
    personal.write_text(yaml.safe_dump({"pinned": [{"category": c, "match": m} for c, m in pins]}))
    return load_budget_model(CONFIG_DIR / "budget_model.yaml", personal)


def test_given_every_atom_and_direction_when_mapped_then_budget_model_matches_converter(
        bridge, tmp_path):
    ours = _analyzer_categories(_model_with_converter_pins(tmp_path))
    theirs = _converter_categories(bridge)

    mismatches = {tid: (ours.get(tid), theirs.get(tid))
                  for tid in set(ours) | set(theirs) if ours.get(tid) != theirs.get(tid)}
    assert not mismatches, f"{len(mismatches)} rows differ, e.g. {dict(list(mismatches.items())[:5])}"


def test_given_personal_pinned_rules_when_compared_then_they_equal_the_converters(bridge):
    personal = load_budget_model(CONFIG_DIR / "budget_model.yaml", PERSONAL_CONFIG_DIR / "budget.yaml")
    if not personal.pinned:
        pytest.skip("no personal pinned rules configured")

    theirs = tuple((cat, tuple(p.lower() for p in phrases))
                   for cat, phrases in converter_rules()["pinned"])

    assert personal.pinned == theirs
