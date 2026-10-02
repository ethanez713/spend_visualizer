"""An INCOME-labelled credit that reverses an earlier charge on the same account is a
refund of that purchase, not income: it takes the charge's category."""
import json

import persister
import src.transformer as tr
from src.rules import RefundIndex, apply_rules

TRAVEL = ("TRAVEL", "TRAVEL_LODGING")
INCOME = {"primary": "INCOME", "detailed": "INCOME_CONTRACTOR", "confidence_level": "LOW"}


def _charge(make_record, **over):
    base = dict(transaction_id="charge", account_id="card", date="2026-05-08", amount=500.0,
                name="Harbor View Inn G", merchant_name="Harbor View Inn",
                merchant_entity_id=None, website=None,
                personal_finance_category={"primary": TRAVEL[0], "detailed": TRAVEL[1],
                                           "confidence_level": "HIGH"})
    return make_record(**{**base, **over})


def _credit(make_record, **over):
    base = dict(transaction_id="credit", account_id="card", date="2026-05-22", amount=-480.0,
                name="Harbor View Inn G", merchant_name=None, merchant_entity_id=None,
                website=None, personal_finance_category=INCOME)
    return make_record(**{**base, **over})


def _hit(credit, *charges):
    index = RefundIndex({c["transaction_id"]: c for c in charges})
    return apply_rules(credit, None, refunds=index)


def given_partial_refund_labelled_income_when_charge_precedes_then_charge_category(make_record):
    hit = _hit(_credit(make_record), _charge(make_record))
    assert (hit.primary, hit.detailed) == TRAVEL
    assert hit.trust == "auto" and hit.rule_name == "refund:same-merchant"


def given_masked_credit_name_when_charge_has_reference_suffix_then_matched_by_merchant(make_record):
    charge = _charge(make_record, name="AIRBNB * HM1234ABCD", merchant_name="Airbnb", amount=900.0)
    credit = _credit(make_record, name="AIRBNB * **********", amount=-900.0)
    assert _hit(credit, charge).detailed == "TRAVEL_LODGING"


def given_same_day_charge_and_credit_when_applied_then_charge_category(make_record):
    charge = _charge(make_record, date="2026-08-22")
    credit = _credit(make_record, date="2026-08-22")
    assert _hit(credit, charge) is not None


def given_credit_larger_than_every_charge_when_applied_then_still_income(make_record):
    credit = _credit(make_record, amount=-5000.0)
    assert _hit(credit, _charge(make_record)) is None


def given_charge_on_another_account_when_applied_then_still_income(make_record):
    assert _hit(_credit(make_record), _charge(make_record, account_id="other_card")) is None


def given_charge_dated_after_credit_when_applied_then_still_income(make_record):
    assert _hit(_credit(make_record), _charge(make_record, date="2026-06-30")) is None


def given_no_matching_merchant_when_applied_then_still_income(make_record):
    assert _hit(_credit(make_record), _charge(make_record, name="Other Co",
                                              merchant_name="Other Co")) is None


def given_only_income_history_for_merchant_when_applied_then_still_income(make_record):
    prior_pay = _charge(make_record, personal_finance_category=INCOME)
    assert _hit(_credit(make_record), prior_pay) is None


def given_ordinary_refund_when_applied_then_not_reclassified(make_record):
    refund = _credit(make_record, personal_finance_category={
        "primary": "GENERAL_MERCHANDISE", "detailed": "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE",
        "confidence_level": "LOW"})
    assert _hit(refund, _charge(make_record)) is None


def given_latest_matching_charge_when_several_then_its_category_wins(make_record):
    older = _charge(make_record, transaction_id="old", date="2026-04-01")
    newer = _charge(make_record, transaction_id="new", date="2026-05-08",
                    personal_finance_category={"primary": "GENERAL_MERCHANDISE",
                                               "detailed": "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE",
                                               "confidence_level": "HIGH"})
    assert _hit(_credit(make_record), older, newer).primary == "GENERAL_MERCHANDISE"


def given_income_credit_in_input_when_run_then_refund_category_applied_and_logged(
        tmp_path, make_record):
    store = {r["transaction_id"]: r for r in (_charge(make_record), _credit(make_record))}
    src = tmp_path / "input.jsonl"
    src.write_text("\n".join(json.dumps(r) for r in store.values()) + "\n")
    out = str(tmp_path / "out.jsonl")

    tr.run(input_path=str(src), out_jsonl=out, out_csv=str(tmp_path / "o.csv"),
           flags_csv=str(tmp_path / "f.csv"), log_path=str(tmp_path / "l.jsonl"),
           levels={"LOW", "MEDIUM", "HIGH", "VERY_HIGH", "UNKNOWN"}, memory_path=None,
           do_drive=False, no_llm=True, debug=False)

    credit = persister.load_jsonl(out)["credit"]
    assert credit["personal_finance_category"]["detailed"] == "TRAVEL_LODGING"
    assert credit["original_pf_category_primary"] == "INCOME"
    assert credit["category_update_reason"] == "refund:same-merchant"
