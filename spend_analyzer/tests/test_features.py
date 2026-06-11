"""Tests for the v2 feature set: necessity slicing, budget, corrections, viz."""
import pandas as pd

import corrections as corr
from config_io import load_budget
from taxonomy import load_taxonomy
from viz import blank_if_missing, humanize_atom, sanitize_for_csv, style_table


def test_necessity_slicing_non_1to1():
    """Child categories need not share their primary's necessity (PLAN.md §6)."""
    t = load_taxonomy()
    groceries = t.resolve("FOOD_AND_DRINK_GROCERIES", "FOOD_AND_DRINK")
    restaurant = t.resolve("FOOD_AND_DRINK_RESTAURANT", "FOOD_AND_DRINK")
    assert groceries.tier0 == "Necessary" and groceries.tier1 == "Groceries"
    assert restaurant.tier0 == "Discretionary" and restaurant.tier1 == "Dining Out"
    # mortgage kept out of exclusions and given its own necessary tier1
    mort = t.resolve("LOAN_PAYMENTS_MORTGAGE_PAYMENT", "LOAN_PAYMENTS")
    assert mort.tier1 == "Mortgage" and not mort.excluded


def test_strict_nesting_still_holds():
    """Each tier1 must map to exactly one tier0 (so rollups never double-count)."""
    t = load_taxonomy()
    seen: dict[str, str] = {}
    for atom, spec in t.atoms.items():
        t1, t0 = spec["tier1"], spec["tier0"]
        assert seen.setdefault(t1, t0) == t0, f"{t1} spans two necessities"


def test_budget_loads_screenshot_goals():
    b = load_budget()
    assert b.goal("Mortgage") == 4403
    assert b.goal("Groceries") == 743
    # the sheet's displayed $12,425 total is rounded; the per-category goals sum to 12,424
    assert sum(b.goals.values()) == 12424


def test_corrections_layer_suggestion():
    # source-owned field => upstream; grouping-only => local
    assert corr.suggest_layer(["pfc_detailed"]) == "upstream"
    assert corr.suggest_layer(["tier1", "tier2"]) == "local"
    assert corr.suggest_layer(["merchant_name", "tier1"]) == "upstream"


def test_viz_helpers():
    assert humanize_atom("FOOD_AND_DRINK_GROCERIES") == "FOOD AND DRINK GROCERIES"
    assert blank_if_missing(None) == "" and blank_if_missing("None") == ""
    assert blank_if_missing("https://x/y.png") == "https://x/y.png"


def test_csv_formula_injection_guarded():
    df = pd.DataFrame({"name": ["=cmd()", "+1", "safe", "-2"]})
    out = sanitize_for_csv(df)
    assert list(out["name"]) == ["'=cmd()", "'+1", "safe", "'-2"]


def test_style_table_counts_not_money():
    df = pd.DataFrame({"Actual": [100.0], "Txns": [5]}).set_index(
        pd.Index(["A"], name="cat"))
    html = style_table(df, money_cols=["Actual"], int_cols=["Txns"],
                       green_cols=["Actual"]).to_html()
    assert "$100" in html        # money formatted
    assert ">5<" in html         # count rendered plain, no $
    assert "$5" not in html
