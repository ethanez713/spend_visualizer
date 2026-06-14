"""Flag-policy guards from the 2026-06 LLM audit (see LLM_ASSESSMENT.md).

Two cheap, model-agnostic guards trim the LLM auditor's false flags:

  * **Amount-sign guard** — Plaid's positive amount is money LEAVING (a debit), so an
    ``INCOME_*`` / ``TRANSFER_IN_*`` suggestion on a positive amount is sign-impossible.
    qwen2.5:7b made this exact error (labelling outgoing brokerage BUY purchases
    ``INCOME_WAGES``). The guard drops such LLM suggestions before they become flags. It is
    deliberately ONE-DIRECTIONAL: a NEGATIVE amount on a spend primary is a normal refund,
    so genuine income (negative) and refunds (negative → spend) still flag as usual.

  * **Intra-tier-1 lateral suppression** — a suggestion that keeps the current primary and
    only changes the detailed doesn't move tier-1 spend analysis, so it isn't flagged by
    default (config.FLAG_INTRA_PRIMARY_LATERALS).

Both suppress only review FLAGS; mechanical 'auto' rules and LLM auto-applies are unaffected.
"""
import pytest

from src import transformer
from src.llm import _SYSTEM_PROMPT
from src.rules import MerchantMemory
from src.transformer import _sign_violation, transform


def _record(**over):
    """A minimal untrusted (LOW) row; override amount + personal_finance_category."""
    base = {"personal_finance_category": {
        "primary": "GENERAL_MERCHANDISE",
        "detailed": "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE",
        "confidence_level": "LOW"}}
    base.update(over)
    return base


# ── _sign_violation unit ──────────────────────────────────────────────────────

@pytest.mark.parametrize("amount,primary,expected", [
    (50.0, "INCOME", True),            # money OUT can't be income
    (50.0, "TRANSFER_IN", True),       # money OUT can't be an inbound transfer
    (-50.0, "INCOME", False),          # money IN — genuine income is fine
    (-50.0, "TRANSFER_IN", False),
    (50.0, "FOOD_AND_DRINK", False),   # a debit on a spend category is normal
    (-50.0, "FOOD_AND_DRINK", False),  # a refund (negative) on a spend category is normal
    (50.0, "TRANSFER_OUT", False),     # outbound transfer on a positive amount is fine
    (0.0, "INCOME", False),            # zero is not a positive outflow
    (None, "INCOME", False),           # unknown amount → can't judge, don't suppress
    ("not-a-number", "INCOME", False),  # non-numeric → can't judge
])
def given_amount_and_primary_when_checked_then_sign_violation(amount, primary, expected):
    assert _sign_violation(amount, primary) is expected


# ── Sign guard through the engine ─────────────────────────────────────────────

@pytest.mark.parametrize("primary,detailed", [
    ("INCOME", "INCOME_WAGES"),
    ("TRANSFER_IN", "TRANSFER_IN_DEPOSIT"),
])
def given_positive_amount_when_llm_suggests_inflow_then_suppressed(
        store_of, FakeLLM_cls, primary, detailed):
    # A positive (outgoing) purchase the model wrongly reads as income/inbound transfer.
    store = store_of(_record(amount=523.0))
    llm = FakeLLM_cls({0: (primary, detailed, "positive amount, indicating income")})

    _, changes, flags = transform(store, memory=None, llm=llm)

    assert changes == [] and flags == []
    rec = store["txn_1"]
    assert rec["personal_finance_category"]["primary"] == "GENERAL_MERCHANDISE"  # untouched
    assert rec["category_review_flag"] == ""


def given_positive_amount_inflow_suggestion_when_authority_final_then_not_applied(
        store_of, FakeLLM_cls):
    # The guard drops the suggestion before step 3, so even "final" authority can't apply it.
    store = store_of(_record(amount=523.0))
    llm = FakeLLM_cls({0: ("INCOME", "INCOME_WAGES", "positive amount")}, confidence="HIGH")

    _, changes, flags = transform(store, memory=None, llm=llm, authority="final")

    assert changes == [] and flags == []
    assert store["txn_1"]["personal_finance_category"]["primary"] == "GENERAL_MERCHANDISE"


def given_negative_amount_when_llm_suggests_income_then_flagged(store_of, FakeLLM_cls):
    # Real payroll (money IN, negative) — the guard must NOT suppress it.
    store = store_of(_record(amount=-2400.0, personal_finance_category={
        "primary": "TRANSFER_IN", "detailed": "TRANSFER_IN_DEPOSIT",
        "confidence_level": "LOW"}))
    llm = FakeLLM_cls({0: ("INCOME", "INCOME_WAGES", "payroll deposit")})

    _, changes, flags = transform(store, memory=None, llm=llm)

    assert changes == [] and len(flags) == 1
    assert store["txn_1"]["category_review_primary"] == "INCOME"


def given_negative_amount_when_llm_suggests_spend_then_flagged_not_suppressed(
        store_of, FakeLLM_cls):
    # A refund (negative) mislabelled INCOME that the model correctly moves back to a spend
    # category must still flag — the guard is one-directional and never blocks negative→spend.
    store = store_of(_record(amount=-19.99, personal_finance_category={
        "primary": "INCOME", "detailed": "INCOME_OTHER_INCOME", "confidence_level": "LOW"}))
    llm = FakeLLM_cls({0: ("GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES",
                           "amazon refund")})

    _, changes, flags = transform(store, memory=None, llm=llm)

    assert len(flags) == 1
    assert store["txn_1"]["category_review_primary"] == "GENERAL_MERCHANDISE"


# ── Intra-tier-1 lateral suppression ──────────────────────────────────────────

def given_intra_primary_lateral_when_llm_suggests_then_not_flagged(store_of, FakeLLM_cls):
    # Same primary FOOD_AND_DRINK, only the detailed changes (RESTAURANT → FAST_FOOD):
    # low-value, so suppressed by default.
    store = store_of(_record(amount=12.0, personal_finance_category={
        "primary": "FOOD_AND_DRINK", "detailed": "FOOD_AND_DRINK_RESTAURANT",
        "confidence_level": "LOW"}))
    llm = FakeLLM_cls({0: ("FOOD_AND_DRINK", "FOOD_AND_DRINK_FAST_FOOD", "mcdonalds")})

    _, changes, flags = transform(store, memory=None, llm=llm)

    assert changes == [] and flags == []
    assert store["txn_1"]["category_review_flag"] == ""


def given_intra_primary_lateral_when_granularity_enabled_then_flagged(
        store_of, FakeLLM_cls, monkeypatch):
    monkeypatch.setattr(transformer, "FLAG_INTRA_PRIMARY_LATERALS", True)
    store = store_of(_record(amount=12.0, personal_finance_category={
        "primary": "FOOD_AND_DRINK", "detailed": "FOOD_AND_DRINK_RESTAURANT",
        "confidence_level": "LOW"}))
    llm = FakeLLM_cls({0: ("FOOD_AND_DRINK", "FOOD_AND_DRINK_FAST_FOOD", "mcdonalds")})

    _, changes, flags = transform(store, memory=None, llm=llm)

    assert len(flags) == 1
    assert store["txn_1"]["category_review_detailed"] == "FOOD_AND_DRINK_FAST_FOOD"


def given_cross_primary_change_when_llm_suggests_then_still_flagged(store_of, FakeLLM_cls):
    # Lateral suppression must not swallow a real tier-1 move.
    store = store_of(_record(amount=12.0))  # GENERAL_MERCHANDISE
    llm = FakeLLM_cls({0: ("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE", "coffee shop")})

    _, changes, flags = transform(store, memory=None, llm=llm)

    assert len(flags) == 1
    assert store["txn_1"]["category_review_primary"] == "FOOD_AND_DRINK"


def given_intra_primary_auto_rule_when_no_llm_then_still_applied(store_of):
    # An entity-id merchant-memory hit is trust="auto" and overwrites in place. Here it
    # remaps a SAME-primary detailed (…_RESTAURANT → …_GROCERIES) — an intra-tier-1 lateral.
    # Lateral suppression is for FLAGS only, so an "auto" correction must still apply.
    mem = MerchantMemory(path=None)
    mem.remember({"merchant_entity_id": "ent_grocer", "merchant_name": "Corner Grocery"},
                 "FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES")
    store = store_of({"merchant_name": "Corner Grocery", "merchant_entity_id": "ent_grocer",
                      "name": "CORNER GROCERY", "original_description": "POS PURCHASE",
                      "website": None, "amount": 40.0,
                      "personal_finance_category": {
                          "primary": "FOOD_AND_DRINK", "detailed": "FOOD_AND_DRINK_RESTAURANT",
                          "confidence_level": "LOW"}})

    _, changes, flags = transform(store, memory=mem, llm=None)

    assert len(changes) == 1 and flags == []
    rec = store["txn_1"]
    assert rec["personal_finance_category"]["detailed"] == "FOOD_AND_DRINK_GROCERIES"
    assert rec["category_update_step"] == "mechanical"


# ── Prompt contract (belt-and-suspenders alongside the deterministic guard) ────

def given_system_prompt_when_built_then_states_amount_sign_rule():
    # The deterministic guard is the safety net, but the prompt must still teach the
    # convention so the model's *correct* suggestions improve too. Lock the directive
    # against accidental removal.
    assert "AMOUNT SIGN" in _SYSTEM_PROMPT
    low = _SYSTEM_PROMPT.lower()
    assert "income" in low and "positive" in low and "never" in low
