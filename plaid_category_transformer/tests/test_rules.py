"""Mechanical rules + merchant memory: each signal produces the expected category."""
from src.rules import (
    MerchantMemory,
    apply_rules,
    contains_word,
    normalize_merchant,
)


def given_noisy_descriptions_when_normalized_then_stable_key():
    assert normalize_merchant("STARBUCKS #1234") == "starbucks"
    assert normalize_merchant("TST* Blue Fern 12") == "tst blue fern"


def given_keyword_when_substring_only_then_no_whole_token_match():
    assert contains_word("uber eats", "uber")
    assert not contains_word("hubertize", "uber")


def given_entity_id_in_memory_when_applied_then_high_confidence_hit(make_record):
    mem = MerchantMemory(path=None)
    rec = make_record(merchant_entity_id="ent_x", merchant_name="Whatever")
    mem.remember(rec, "FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE")

    other = make_record(merchant_entity_id="ent_x", merchant_name="diff name",
                        website=None, name="random", original_description="random")
    hit = apply_rules(other, mem)
    assert hit is not None
    assert (hit.primary, hit.detailed) == ("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE")
    assert hit.rule_name == "memory:entity_id"
    assert hit.confidence == "HIGH"


def given_name_only_in_memory_when_applied_then_medium_confidence_hit(make_record):
    mem = MerchantMemory(path=None)
    seed = make_record(merchant_entity_id=None, merchant_name="Joe's Diner")
    mem.remember(seed, "FOOD_AND_DRINK", "FOOD_AND_DRINK_RESTAURANT")

    rec = make_record(merchant_entity_id=None, merchant_name="JOE'S DINER #9",
                      website=None, name="x", original_description="x")
    hit = apply_rules(rec, mem)
    assert hit is not None
    assert hit.rule_name == "memory:name"
    assert hit.confidence == "MEDIUM"


def given_tst_prefix_when_applied_then_restaurant(make_record):
    rec = make_record(merchant_name=None, website=None,
                      name="TST* BLUE FERN", original_description="TST* BLUE FERN")
    hit = apply_rules(rec, None)
    assert (hit.primary, hit.detailed) == ("FOOD_AND_DRINK", "FOOD_AND_DRINK_RESTAURANT")
    assert hit.rule_name == "pos:tst"


def given_bank_bill_pay_to_a_card_when_applied_then_credit_card_payment_auto(make_record):
    rec = make_record(merchant_name=None, website=None, merchant_entity_id=None,
                      name="Online Scheduled Payment to ACCT# 1111 Confirmation# 123",
                      original_description="Online Scheduled Payment to ACCT# 1111",
                      personal_finance_category={
                          "primary": "LOAN_PAYMENTS",
                          "detailed": "LOAN_PAYMENTS_OTHER_PAYMENT",
                          "confidence_level": "LOW"})
    hit = apply_rules(rec, None)
    assert (hit.primary, hit.detailed) == ("LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
    assert hit.trust == "auto"


def given_uppercase_bill_pay_text_when_applied_then_still_a_card_payment(make_record):
    rec = make_record(merchant_name=None, website=None, merchant_entity_id=None,
                      name="ONLINE SCHEDULED PAYMENT TO ACCT# 2222", original_description="")
    hit = apply_rules(rec, None)
    assert hit.detailed == "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"


def given_card_cashback_deposit_when_applied_then_other_income_auto(make_record):
    rec = make_record(merchant_name=None, website=None, merchant_entity_id=None, amount=-12.34,
                      name="Bank of America DES:CASHREWARD ID:SMITH INDN:XXXXX0",
                      original_description="",
                      personal_finance_category={
                          "primary": "OTHER", "detailed": "OTHER_OTHER",
                          "confidence_level": "LOW"})
    hit = apply_rules(rec, None)
    assert (hit.primary, hit.detailed) == ("INCOME", "INCOME_OTHER_INCOME")
    assert hit.trust == "auto"


def given_other_online_payment_text_when_applied_then_no_card_payment_hit(make_record):
    rec = make_record(merchant_name=None, website=None, merchant_entity_id=None,
                      name="Online Payment to Pepco", original_description="Online Payment to Pepco")
    assert apply_rules(rec, None) is None


def given_website_when_applied_then_domain_hint(make_record):
    rec = make_record(merchant_name=None, name="charge", original_description="charge",
                      website="www.netflix.com")
    hit = apply_rules(rec, None)
    assert (hit.primary, hit.detailed) == ("ENTERTAINMENT", "ENTERTAINMENT_TV_AND_MOVIES")
    assert hit.rule_name.startswith("website:")


def given_keyword_when_applied_then_keyword_rule(make_record):
    rec = make_record(merchant_name="UBER TRIP", name="UBER TRIP HELP.UBER",
                      original_description="UBER", website=None)
    hit = apply_rules(rec, None)
    assert hit.primary == "TRANSPORTATION"
    assert hit.detailed == "TRANSPORTATION_TAXIS_AND_RIDE_SHARES"


def given_no_signal_when_applied_then_none(make_record):
    rec = make_record(merchant_name="Zzz Unknown LLC", name="POS 8841",
                      original_description="POS 8841", website=None,
                      merchant_entity_id=None)
    assert apply_rules(rec, None) is None


def given_memory_present_when_applied_then_memory_wins_over_static(make_record):
    # The record matches the 'netflix' keyword rule, but memory takes precedence.
    mem = MerchantMemory(path=None)
    rec = make_record(merchant_entity_id="ent_n", name="NETFLIX", website="netflix.com",
                      original_description="NETFLIX", merchant_name="Netflix")
    mem.remember(rec, "ENTERTAINMENT", "ENTERTAINMENT_MUSIC_AND_AUDIO")
    hit = apply_rules(rec, mem)
    assert hit.rule_name == "memory:entity_id"
    assert hit.detailed == "ENTERTAINMENT_MUSIC_AND_AUDIO"


def given_memory_saved_when_reloaded_then_round_trips_and_is_owner_only(tmp_path, make_record):
    import os

    path = tmp_path / "merchant_memory.json"
    mem = MerchantMemory(path=str(path))
    rec = make_record(merchant_entity_id="ent_x")
    mem.remember(rec, "FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE")
    mem.save()

    assert oct(os.stat(path).st_mode)[-3:] == "600"
    reloaded = MerchantMemory(path=str(path))
    hit = reloaded.lookup(rec)
    assert (hit.primary, hit.detailed) == ("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE")
