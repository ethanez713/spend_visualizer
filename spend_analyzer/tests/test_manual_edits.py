"""Tests for the manual-edit intent bridge (manual_edits.py).

The bridge imports the transformer's own src.manual (deliberate coupling), so these
tests exercise the REAL intent format end to end against a tmp log file. They skip
when the sibling transformer repo isn't present (the feature disables itself the
same way in the UI).
"""
import pytest

import manual_edits

pytestmark = pytest.mark.skipif(
    manual_edits.status() is not None,
    reason="plaid_category_transformer repo not available (bridge disabled)",
)

RAW = {
    "transaction_id": "txn_ui_1",
    "merchant_name": "Blue Bottle Coffee",
    "merchant_entity_id": "ent_bluebottle",
    "name": "SQ *BLUE BOTTLE",
    "original_description": "SQ *BLUE BOTTLE COFFEE",
    "website": "bluebottlecoffee.com",
    "amount": 6.5,
    "payment_channel": "in store",
    "personal_finance_category": {
        "primary": "GENERAL_MERCHANDISE",
        "detailed": "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE",
        "confidence_level": "LOW",
    },
}


def test_add_edit_roundtrips_through_the_intent_log(tmp_path):
    path = str(tmp_path / "edits.jsonl")
    it = manual_edits.add_edit(RAW, scope="transaction", primary="FOOD_AND_DRINK",
                               detailed="FOOD_AND_DRINK_COFFEE", note="cafe", path=path)
    loaded = manual_edits.intents(path)
    assert [x["id"] for x in loaded] == [it["id"]]
    assert loaded[0]["source"] == "ui"
    assert loaded[0]["set"] == {"primary": "FOOD_AND_DRINK",
                                "detailed": "FOOD_AND_DRINK_COFFEE"}
    assert loaded[0]["snapshot"]["before_primary"] == "GENERAL_MERCHANDISE"


def test_merchant_scope_captures_both_merchant_keys(tmp_path):
    path = str(tmp_path / "edits.jsonl")
    it = manual_edits.add_edit(RAW, scope="merchant", primary="FOOD_AND_DRINK",
                               detailed="FOOD_AND_DRINK_COFFEE", path=path)
    assert it["match"] == {"merchant_entity_id": "ent_bluebottle",
                           "merchant_name_normalized": "blue bottle coffee"}


def test_revoke_drops_the_intent(tmp_path):
    path = str(tmp_path / "edits.jsonl")
    it = manual_edits.add_edit(RAW, scope="transaction", primary="FOOD_AND_DRINK",
                               detailed="FOOD_AND_DRINK_COFFEE", path=path)
    manual_edits.revoke(it["id"], path=path)
    assert manual_edits.intents(path) == []


def test_invalid_category_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        manual_edits.add_edit(RAW, scope="transaction", primary="FOOD_AND_DRINK",
                              detailed="NOT_A_REAL_LEAF",
                              path=str(tmp_path / "edits.jsonl"))


def test_taxonomy_menu_comes_from_the_transformer():
    primaries, detailed = manual_edits.taxonomy()
    assert "FOOD_AND_DRINK" in primaries
    assert "FOOD_AND_DRINK_COFFEE" in detailed["FOOD_AND_DRINK"]
