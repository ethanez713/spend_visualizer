"""LLM accuracy on a tiered golden set (real Ollama qwen2.5:7b).

A small hand-labeled set of realistic LOW/MEDIUM-confidence rows spanning easy → medium
→ hard → ambiguous. We assert *threshold* accuracy (never exact-match): the model is
greedy-decoded (temperature=0, seed=0) but minor hardware-driven drift is expected, so
exact equality would be flaky. The model/sampling/batch come from ``src.llm`` (the single
source of truth) — these tests never re-tune the prompt to the golden set (no overfitting).

Thresholds are deliberately slack (well below observed performance) so a healthy model
passes reliably and only a real regression (or a broken prompt/taxonomy) fails.
"""
import urllib.request

import pytest

from src.llm import LLM_HOST, CategoryLLM
from src.transformer import _build_item


def _ollama_up() -> bool:
    try:
        urllib.request.urlopen(f"{LLM_HOST}/api/tags", timeout=3)
        return True
    except Exception:
        return False


if not _ollama_up():
    pytest.skip("Ollama not reachable — skipping LLM integration tests.",
                allow_module_level=True)


# (tier, record-signals, expected_primary, expected_detailed)
# Records carry only the fields the LLM sees; current pf is a plausible WRONG low-conf guess.
GOLDEN = [
    # ── easy: unambiguous well-known merchants ────────────────────────────────
    ("easy", {
        "merchant_name": "Starbucks", "name": "STARBUCKS STORE 123",
        "website": "starbucks.com", "payment_channel": "in store", "amount": 5.75,
        "personal_finance_category": {"primary": "GENERAL_MERCHANDISE",
            "detailed": "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE",
            "confidence_level": "LOW"}},
     "FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE"),
    ("easy", {
        "merchant_name": "Shell", "name": "SHELL OIL 5567",
        "payment_channel": "in store", "amount": 48.20,
        "personal_finance_category": {"primary": "GENERAL_MERCHANDISE",
            "detailed": "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE",
            "confidence_level": "LOW"}},
     "TRANSPORTATION", "TRANSPORTATION_GAS"),
    ("easy", {
        "merchant_name": "Netflix", "name": "NETFLIX.COM", "website": "netflix.com",
        "payment_channel": "online", "amount": 15.49,
        "personal_finance_category": {"primary": "GENERAL_SERVICES",
            "detailed": "GENERAL_SERVICES_OTHER_GENERAL_SERVICES",
            "confidence_level": "MEDIUM"}},
     "ENTERTAINMENT", "ENTERTAINMENT_TV_AND_MOVIES"),
    ("easy", {
        "merchant_name": "Delta Air Lines", "name": "DELTA AIR 0061234567",
        "payment_channel": "online", "amount": 410.00,
        "personal_finance_category": {"primary": "GENERAL_MERCHANDISE",
            "detailed": "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE",
            "confidence_level": "LOW"}},
     "TRAVEL", "TRAVEL_FLIGHTS"),

    # ── medium: needs world knowledge, clear once known ───────────────────────
    ("medium", {
        "merchant_name": "Trader Joe's", "name": "TRADER JOES #455",
        "payment_channel": "in store", "amount": 63.10,
        "personal_finance_category": {"primary": "GENERAL_MERCHANDISE",
            "detailed": "GENERAL_MERCHANDISE_SUPERSTORES", "confidence_level": "MEDIUM"}},
     "FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES"),
    ("medium", {
        "merchant_name": "CVS", "name": "CVS/PHARMACY #04122",
        "payment_channel": "in store", "amount": 24.30,
        "personal_finance_category": {"primary": "GENERAL_MERCHANDISE",
            "detailed": "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE",
            "confidence_level": "LOW"}},
     "MEDICAL", "MEDICAL_PHARMACIES_AND_SUPPLEMENTS"),
    ("medium", {
        "merchant_name": "Planet Fitness", "name": "PLANET FIT 8001234567",
        "payment_channel": "online", "amount": 24.99,
        "personal_finance_category": {"primary": "GENERAL_SERVICES",
            "detailed": "GENERAL_SERVICES_OTHER_GENERAL_SERVICES",
            "confidence_level": "LOW"}},
     "PERSONAL_CARE", "PERSONAL_CARE_GYMS_AND_FITNESS_CENTERS"),
    ("medium", {
        "merchant_name": "Comcast", "name": "COMCAST CALIFORNIA",
        "payment_channel": "online", "amount": 89.99,
        "personal_finance_category": {"primary": "GENERAL_SERVICES",
            "detailed": "GENERAL_SERVICES_OTHER_GENERAL_SERVICES",
            "confidence_level": "MEDIUM"}},
     "RENT_AND_UTILITIES", "RENT_AND_UTILITIES_INTERNET_AND_CABLE"),

    # ── hard: cryptic text / POS prefixes ─────────────────────────────────────
    ("hard", {
        "merchant_name": None, "name": "TST* CIELO ROJO",
        "original_description": "TST* CIELO ROJO HEALDSBURG",
        "payment_channel": "in store", "amount": 72.40,
        "personal_finance_category": {"primary": "GENERAL_MERCHANDISE",
            "detailed": "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE",
            "confidence_level": "LOW"}},
     "FOOD_AND_DRINK", "FOOD_AND_DRINK_RESTAURANT"),
    ("hard", {
        "merchant_name": "Chevron", "name": "CHEVRON 0094521",
        "payment_channel": "in store", "amount": 51.00,
        "personal_finance_category": {"primary": "FOOD_AND_DRINK",
            "detailed": "FOOD_AND_DRINK_OTHER_FOOD_AND_DRINK", "confidence_level": "LOW"}},
     "TRANSPORTATION", "TRANSPORTATION_GAS"),

    # ── ambiguous: primary should be right; detailed may legitimately vary ─────
    ("ambiguous", {
        "merchant_name": "Amazon", "name": "AMZN MKTP US*2X9",
        "website": "amazon.com", "payment_channel": "online", "amount": 31.99,
        "personal_finance_category": {"primary": "GENERAL_SERVICES",
            "detailed": "GENERAL_SERVICES_OTHER_GENERAL_SERVICES",
            "confidence_level": "LOW"}},
     "GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES"),
    ("ambiguous", {
        "merchant_name": "Target", "name": "TARGET T-1842",
        "website": "target.com", "payment_channel": "in store", "amount": 88.12,
        "personal_finance_category": {"primary": "FOOD_AND_DRINK",
            "detailed": "FOOD_AND_DRINK_GROCERIES", "confidence_level": "LOW"}},
     "GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_SUPERSTORES"),
]


@pytest.fixture(scope="module")
def decisions():
    """Run the real LLM once over the whole golden set; reuse across assertions."""
    items = []
    for i, (_tier, rec, _p, _d) in enumerate(GOLDEN):
        pfc = rec.get("personal_finance_category") or {}
        full = {**rec}
        full.setdefault("transaction_id", f"g_{i}")
        items.append(_build_item(i, full, None))
    return CategoryLLM().categorize(items)


def given_golden_set_when_categorized_then_primary_accuracy_above_threshold(decisions):
    correct = sum(1 for i, (_t, _r, p, _d) in enumerate(GOLDEN)
                  if i in decisions and decisions[i].primary == p)
    acc = correct / len(GOLDEN)
    print(f"\nPRIMARY accuracy: {correct}/{len(GOLDEN)} = {acc:.0%}")
    assert acc >= 0.70


def given_golden_set_when_categorized_then_detailed_accuracy_above_threshold(decisions):
    correct = sum(1 for i, (_t, _r, _p, d) in enumerate(GOLDEN)
                  if i in decisions and decisions[i].detailed == d)
    acc = correct / len(GOLDEN)
    print(f"DETAILED accuracy: {correct}/{len(GOLDEN)} = {acc:.0%}")
    assert acc >= 0.55


def given_easy_tier_when_categorized_then_high_primary_accuracy(decisions):
    easy = [(i, p) for i, (t, _r, p, _d) in enumerate(GOLDEN) if t == "easy"]
    correct = sum(1 for i, p in easy if i in decisions and decisions[i].primary == p)
    acc = correct / len(easy)
    print(f"EASY primary accuracy: {correct}/{len(easy)} = {acc:.0%}")
    assert acc >= 0.75


def given_every_decision_when_returned_then_within_taxonomy(decisions):
    from src import pfc_taxonomy
    for i, dec in decisions.items():
        assert pfc_taxonomy.is_valid(dec.primary, dec.detailed), \
            f"row {i}: {dec.primary}/{dec.detailed} not in taxonomy"
