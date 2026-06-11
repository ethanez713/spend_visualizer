"""Tests for taxonomy resolution, exclusions, merchant overrides (PLAN.md §6)."""
from taxonomy import load_taxonomy


def tax():
    return load_taxonomy()


def test_exclusions_glob():
    t = tax()
    assert t.is_excluded("TRANSFER_IN_DEPOSIT")
    assert t.is_excluded("TRANSFER_OUT_WITHDRAWAL")
    assert t.is_excluded("LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")
    # the #1 double-count trap: mortgage is real spend, NOT excluded
    assert not t.is_excluded("LOAN_PAYMENTS_MORTGAGE_PAYMENT")


def test_strict_nesting_resolution():
    t = tax()
    r = t.resolve("FOOD_AND_DRINK_GROCERIES", "FOOD_AND_DRINK")
    assert r.category_path[:3] == [r.tier0, r.tier1, r.tier2]
    assert r.atom == "FOOD_AND_DRINK_GROCERIES"
    assert not r.unmapped


def test_unmapped_atom_is_flagged_not_crashing():
    t = tax()
    r = t.resolve("MADE_UP_NEW_CODE", "GENERAL_MERCHANDISE")
    assert r.unmapped
    assert r.tier1  # still resolves to a sane default


def test_merchant_override_refines_tier():
    t = tax()
    base = t.resolve("TRANSPORTATION_TAXIS_AND_RIDE_SHARES", "TRANSPORTATION")
    eats = t.resolve("TRANSPORTATION_TAXIS_AND_RIDE_SHARES", "TRANSPORTATION",
                     merchant_name="Uber Eats")
    assert base.tier1 == "Transportation"
    assert eats.tier1 == "Dining Out"   # override wins
    assert eats.atom == base.atom        # atom stays stable
