"""Budget edits: building, resolving against the shared intent log, matching with the
transformer's precedence, and what they do to the annotated frame."""
import json

import pandas as pd
import pytest

import budget_edits
from budget_model import BudgetModel, annotate

VENMO = {"transaction_id": "t1", "merchant_entity_id": "ent_venmo", "merchant_name": "Venmo",
         "name": "Venmo", "original_description": "VENMO PAYMENT 1234"}
OTHER_VENMO = {**VENMO, "transaction_id": "t2"}
UNRELATED = {"transaction_id": "t9", "merchant_entity_id": "ent_x", "merchant_name": "Acme",
             "name": "Acme", "original_description": ""}


def _log(tmp_path, *entries):
    path = tmp_path / "manual_edits.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    return str(path)


def _edit(record=VENMO, scope="transaction", **kw):
    return budget_edits.build_edit(record, scope=scope, **kw)


def test_given_category_when_built_then_a_budget_edit_with_the_transformers_match():
    edit = _edit(category="Travel", note="trip")

    assert edit["action"] == "budget_edit" and edit["set"] == {"budget_category": "Travel"}
    assert edit["match"] == {"transaction_id": "t1"} and edit["note"] == "trip"


def test_given_merchant_scope_when_built_then_entity_and_name_are_the_match():
    assert _edit(scope="merchant", category="Travel")["match"] == {
        "merchant_entity_id": "ent_venmo", "merchant_name_normalized": "venmo"}


@pytest.mark.parametrize("kwargs", [
    {}, {"note": "   "}, {"category": "A", "exclude": True}])
def test_given_nothing_to_apply_or_conflicting_choices_when_built_then_value_error(kwargs):
    with pytest.raises(ValueError):
        _edit(**kwargs)


def test_given_exclusion_or_note_only_when_built_then_allowed():
    assert _edit(exclude=True)["set"] == {"exclude": True}
    assert _edit(note="Europe")["set"] == {}


def test_given_edits_appended_when_resolved_then_only_applicable_ones_in_append_order(tmp_path):
    first, second = _edit(category="A"), _edit(category="B")
    revoked = _edit(category="C")
    revoke = {"id": "r1", "action": "revoke", "revokes": revoked["id"]}
    pfc_edit = {"id": "e1", "action": "edit", "scope": "transaction",
                "match": {"transaction_id": "t1"}, "set": {"primary": "X", "detailed": "Y"}}
    broken = {**_edit(category="D"), "match": {"transaction_id": ""}}
    empty = {**_edit(category="E"), "set": {}, "note": ""}
    path = _log(tmp_path, first, revoked, second, revoke, pfc_edit, broken, empty)

    assert [e["id"] for e in budget_edits.current(path)] == [first["id"], second["id"]]


def test_given_overlapping_scopes_when_matched_then_transaction_beats_merchant_and_latest_wins(tmp_path):
    merchant_old = _edit(scope="merchant", category="Dining Out")
    merchant_new = _edit(scope="merchant", category="Travel")
    one_row = _edit(OTHER_VENMO, category="Gifts/Charity")
    path = _log(tmp_path, merchant_old, merchant_new, one_row)

    found = budget_edits.overrides_for([VENMO, OTHER_VENMO, UNRELATED], path)

    assert found["t1"]["category"] == "Travel" and found["t2"]["category"] == "Gifts/Charity"
    assert "t9" not in found


def test_given_a_note_added_after_a_category_when_matched_then_the_category_is_kept(tmp_path):
    chosen = _edit(category="Travel")
    noted = _edit(note="Europe")

    found = budget_edits.overrides_for([VENMO], _log(tmp_path, chosen, noted))["t1"]

    assert (found["category"], found["note"], found["edit"]) == ("Travel", "Europe", chosen["id"])


def test_given_note_only_when_matched_then_no_category_decision(tmp_path):
    found = budget_edits.overrides_for([VENMO], _log(tmp_path, _edit(note="Europe")))["t1"]

    assert found == {"category": None, "edit": "", "note": "Europe"}


def test_given_exclusion_when_matched_then_edit_present_with_no_category(tmp_path):
    edit = _edit(exclude=True)

    assert budget_edits.overrides_for([VENMO], _log(tmp_path, edit))["t1"] == {
        "category": None, "edit": edit["id"], "note": ""}


def test_given_no_log_when_matched_then_nothing(tmp_path):
    assert budget_edits.overrides_for([VENMO], str(tmp_path / "absent.jsonl")) == {}


def test_given_a_revoked_edit_when_matched_then_the_row_is_free_again(tmp_path):
    edit = _edit(category="Travel")
    path = _log(tmp_path, edit, {"id": "r", "action": "revoke", "revokes": edit["id"]})

    assert budget_edits.overrides_for([VENMO], path) == {}


def _frame():
    df = pd.DataFrame({
        "transaction_id": ["spend", "transfer_out", "transfer_in", "income", "plain_transfer"],
        "flow": ["spend", "excluded", "excluded", "income", "excluded"],
        "tier1": ["Dining Out", "Transfer Out", "Transfer In", "Income", "Transfer Out"],
        "atom": ["FOOD", "TRANSFER_OUT_X", "TRANSFER_IN_X", "INCOME_WAGES", "TRANSFER_OUT_X"],
        "description": ["Cafe", "Venmo", "Venmo", "Payroll", "Zelle"],
        "amount": [30.0, 120.0, -400.0, -5000.0, 15.0]})
    return df


MODEL = BudgetModel(drop_atoms=("INCOME_*", "TRANSFER_*"))


def test_given_edits_when_annotated_then_they_count_transfers_signed_and_beat_the_model():
    overrides = {"spend": {"category": None, "edit": "x1", "note": ""},
                 "transfer_out": {"category": "Travel", "edit": "x2", "note": "boat"},
                 "transfer_in": {"category": "Venmo - Income", "edit": "x3", "note": ""}}

    out = annotate(_frame(), MODEL, overrides).set_index("transaction_id")

    assert out["budget_cat"].tolist() == [None, "Travel", "Venmo - Income", None, None]
    assert out["budget_amount"].tolist() == [0.0, 120.0, -400.0, 0.0, 0.0]
    assert out.loc["transfer_out", ["budget_note", "budget_edit_id"]].tolist() == ["boat", "x2"]


def test_given_note_only_override_when_annotated_then_category_comes_from_the_model():
    out = annotate(_frame(), MODEL, {"spend": {"category": None, "edit": "", "note": "dinner"}})

    assert out.loc[0, "budget_cat"] == "Dining Out" and out.loc[0, "budget_note"] == "dinner"
    assert out.loc[0, "budget_edit_id"] == ""


def test_given_no_overrides_when_annotated_then_only_model_spend_is_counted():
    out = annotate(_frame(), MODEL)

    assert out["budget_cat"].tolist() == ["Dining Out", None, None, None, None]
    assert out["budget_amount"].tolist() == [30.0, 0.0, 0.0, 0.0, 0.0]
