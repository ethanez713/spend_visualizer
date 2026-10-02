"""Tests for the v2 feature set: necessity slicing, budget, corrections, viz."""
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

import corrections as corr
from config_io import load_budget
from taxonomy import load_taxonomy
from viz import blank_if_missing, humanize_atom, md_text, sanitize_for_csv, style_table


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


def test_budget_loads_goals_and_fails_soft(tmp_path):
    # budget.yaml is personal data living under the external data root — the loader
    # must parse a real-shaped file and return an empty Budget when none exists yet.
    p = tmp_path / "budget.yaml"
    p.write_text("period: monthly\ngoals:\n  Mortgage: 4403\n  Groceries: 743\n")
    b = load_budget(p)
    assert b.goal("Mortgage") == 4403.0
    assert b.goal("Groceries") == 743.0
    assert b.goal("Nonexistent") is None
    assert load_budget(tmp_path / "missing.yaml").goals == {}


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
                       heat_cols=["Actual"]).to_html()
    assert "$100" in html        # money formatted
    assert ">5<" in html         # count rendered plain, no $
    assert "$5" not in html


def test_data_root_resolution(tmp_path, monkeypatch):
    """Env var beats the data_root file; the file's first non-comment line wins."""
    import config_io

    monkeypatch.setenv("SPEND_VISUALIZER_DATA", str(tmp_path / "from_env"))
    assert config_io.data_root() == tmp_path / "from_env"

    monkeypatch.delenv("SPEND_VISUALIZER_DATA")
    root_file = tmp_path / "data_root"
    root_file.write_text("# comment\n\n~/somewhere\n~/ignored-second-line\n")
    monkeypatch.setattr(config_io, "MONOREPO_ROOT", tmp_path)
    assert str(config_io.data_root()).endswith("/somewhere")
    assert not str(config_io.data_root()).startswith("~")  # tilde expanded

    root_file.unlink()
    assert str(config_io.data_root()).endswith("/finance_data")  # default


def test_accounts_yaml_is_read_from_the_personal_config_dir():
    """build_cube must not look for accounts.yaml in the project config dir.

    It only ever lives under the data root, so a CONFIG_DIR default made
    load_accounts return {} in production — silently disabling every per-account
    facet AND the `include: false` opt-out asserted below.
    """
    import inspect

    from config_io import PERSONAL_CONFIG_DIR
    from data import build_cube

    default = inspect.signature(build_cube).parameters["accounts_path"].default
    assert Path(default).parent == PERSONAL_CONFIG_DIR


def test_accounts_include_false_drops_the_account_from_the_cube():
    from enrich import enrich
    from ingest.normalize import normalize_one

    def raw(tid, account):
        return {"transaction_id": tid, "account_id": account, "amount": 10.0,
                "date": "2026-03-04", "name": "COFFEE",
                "personal_finance_category": {"primary": "FOOD_AND_DRINK",
                                              "detailed": "FOOD_AND_DRINK_COFFEE"}}

    txns = [normalize_one(raw("t1", "keep")), normalize_one(raw("t2", "drop"))]
    accounts = {"keep": {"person": "A"}, "drop": {"person": "B", "include": False}}
    df = enrich(txns, load_taxonomy(), accounts)
    assert list(df["transaction_id"]) == ["t1"]
    assert list(df["person"]) == ["A"], "accounts.yaml person is the un-stamped fallback"


def test_iso_week_stays_out_of_the_hierarchical_time_path():
    """A named bucket in the path must not be split across parents.

    2026-12-31 and 2027-01-01 are the same ISO week (2026-W53) but different
    calendar years, so nesting the week under the year filed one real week under
    two — and a week straddling a month boundary does the same every month. The
    week label is self-contained, so it is grouped on directly instead. (`dow`
    stays: path nodes are keyed by their full prefix, so a repeating leaf label
    is a distinct node per month, not a split bucket.)
    """
    from enrich import _time_dims

    dec, jan = _time_dims("2026-12-31"), _time_dims("2027-01-01")
    assert dec["week"] == jan["week"] == "2026-W53"
    assert dec["year"] != jan["year"]

    d = date(2026, 1, 1)
    while d < date(2028, 1, 1):
        dims = _time_dims(d.isoformat())
        assert dims["week"] not in dims["path"], d
        assert dims["path"] == [str(d.year), dims["quarter"], dims["month"], dims["dow"]]
        d += timedelta(days=1)


def test_md_text_neutralises_markup_in_data():
    assert md_text("**1234 $5_a") == r"\*\*1234 \$5\_a"
    assert md_text("Plain Name") == "Plain Name"
