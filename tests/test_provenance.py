"""Provenance writing (set_provenance) and stage attribution (_decide)."""
from src.llm import CategoryDecision
from src.rules import RuleHit
from src.schema import CORRECTED_CONFIDENCE, NEW_COLUMNS, ensure_new_columns, set_provenance
from src.transformer import _decide


# ── set_provenance ─────────────────────────────────────────────────────────────

def given_a_change_when_provenance_set_then_originals_saved_and_value_overwritten(make_record):
    rec = make_record(personal_finance_category={
        "primary": "GENERAL_MERCHANDISE",
        "detailed": "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE",
        "confidence_level": "LOW"})

    changed = set_provenance(rec, "FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE",
                             "llm", "merchant is a coffee shop", "HIGH")
    assert changed is True

    assert rec["original_pf_category_primary"] == "GENERAL_MERCHANDISE"
    assert rec["original_pf_category_detailed"] == "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE"
    assert rec["original_pf_category_confidence"] == "LOW"

    pfc = rec["personal_finance_category"]
    assert pfc["primary"] == "FOOD_AND_DRINK"
    assert pfc["detailed"] == "FOOD_AND_DRINK_COFFEE"
    assert pfc["confidence_level"] == CORRECTED_CONFIDENCE

    assert rec["category_update_step"] == "llm"
    assert rec["category_update_reason"] == "merchant is a coffee shop"
    assert rec["category_update_confidence"] == "HIGH"


def given_no_change_when_provenance_set_then_columns_stay_empty(make_record):
    rec = make_record(personal_finance_category={
        "primary": "FOOD_AND_DRINK", "detailed": "FOOD_AND_DRINK_COFFEE",
        "confidence_level": "LOW"})

    changed = set_provenance(rec, "FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE",
                             "llm", "still coffee", "HIGH")
    assert changed is False
    assert rec["original_pf_category_primary"] is None
    assert rec["category_update_step"] == ""
    # original confidence untouched (NOT the CORRECTED sentinel)
    assert rec["personal_finance_category"]["confidence_level"] == "LOW"


def given_a_record_when_ensure_new_columns_then_all_present(make_record):
    rec = make_record()
    ensure_new_columns(rec)
    for col in NEW_COLUMNS:
        assert col in rec


# ── _decide attribution ────────────────────────────────────────────────────────

def _rec(primary="GENERAL_MERCHANDISE",
         detailed="GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE", conf="LOW"):
    return {"personal_finance_category": {"primary": primary, "detailed": detailed,
                                          "confidence_level": conf}}


def _decision(primary, detailed, reason="r", conf="HIGH"):
    return CategoryDecision(row_index=0, primary=primary, detailed=detailed,
                            changed=True, confidence=conf, reason=reason)


def _auto(primary, detailed, name="memory:entity_id"):
    return RuleHit(primary, detailed, name, "HIGH", "auto")


def _flag(primary, detailed, name="keyword:x"):
    return RuleHit(primary, detailed, name, "MEDIUM", "flag")


# Default authority is "flag": the LLM never auto-applies, it only flags.

def given_llm_differs_on_untrusted_row_when_flag_authority_then_flagged_not_applied():
    rec = _rec()
    dec = _decision("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE", reason="coffee shop")
    d = _decide(rec, None, dec)  # default authority "flag"
    assert d.action == "flag"
    assert (d.primary, d.detailed, d.source) == ("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE", "llm")
    assert d.reason == "coffee shop"


def given_llm_high_on_untrusted_row_when_apply_when_high_then_applied():
    rec = _rec()
    dec = _decision("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE", conf="HIGH")
    d = _decide(rec, None, dec, authority="apply_when_high")
    assert d.action == "apply"
    assert d.source == "llm"


def given_llm_medium_on_untrusted_row_when_apply_when_high_then_flagged():
    rec = _rec()
    dec = _decision("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE", conf="MEDIUM")
    d = _decide(rec, None, dec, authority="apply_when_high")
    assert d.action == "flag"


def given_llm_high_on_trusted_row_when_apply_when_high_then_only_flagged():
    # Trusted Plaid labels are never auto-changed by the LLM, even at HIGH confidence.
    rec = _rec(conf="VERY_HIGH")
    dec = _decision("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE", conf="HIGH")
    d = _decide(rec, None, dec, authority="apply_when_high")
    assert d.action == "flag"


def given_mechanical_auto_rule_when_decided_then_applied_in_place():
    rec = _rec()
    mech = _auto("TRAVEL", "TRAVEL_FLIGHTS", "pos:cot*flt")
    d = _decide(rec, mech, None)
    assert d.action == "apply"
    assert (d.primary, d.detailed, d.source) == ("TRAVEL", "TRAVEL_FLIGHTS", "mechanical")


def given_mechanical_auto_rule_on_trusted_row_when_decided_then_still_applied():
    rec = _rec(conf="VERY_HIGH")
    mech = _auto("TRAVEL", "TRAVEL_FLIGHTS", "pos:cot*flt")
    d = _decide(rec, mech, None)
    assert d.action == "apply"


def given_mechanical_flag_rule_without_llm_when_decided_then_flagged():
    rec = _rec()
    mech = _flag("FOOD_AND_DRINK", "FOOD_AND_DRINK_RESTAURANT", "pos:tst")
    d = _decide(rec, mech, None)
    assert d.action == "flag"
    assert d.source == "mechanical"


def given_llm_concurs_with_current_when_loose_rule_disagrees_then_no_flag():
    # The LLM saw the mechanical suggestion and kept the current category → its verdict
    # governs; a loose mechanical rule must NOT re-raise a flag.
    rec = _rec()
    mech = _flag("FOOD_AND_DRINK", "FOOD_AND_DRINK_RESTAURANT", "keyword:x")
    dec = _decision("GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE")
    d = _decide(rec, mech, dec)
    assert d.action == "none"


def given_no_llm_and_no_mechanical_when_decided_then_none():
    d = _decide(_rec(), None, None)
    assert d.action == "none"
