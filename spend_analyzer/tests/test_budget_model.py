"""The budget category model: tier-1 folded into budget categories, pinned merchants,
the layered config loader, and the frame annotation the Budget view reads."""
from pathlib import Path

import pandas as pd
import yaml

from budget_model import BudgetModel, annotate, load_budget_model
from config_io import CONFIG_DIR

GENERIC = CONFIG_DIR / "budget_model.yaml"

BUDGET_CATEGORIES = {
    "Dining Out", "Entertainment", "Gifts/Charity", "Groceries", "Healthcare", "House",
    "Merchandise", "Mortgage", "Other", "Rent", "Transportation", "Travel", "Utilities"}


def _model(**over):
    base = dict(fold={"Services": "Other", "Personal Care": "Other"},
                atoms={"PERSONAL_CARE_GYMS_AND_FITNESS_CENTERS": "Healthcare"},
                drop=frozenset({"Taxes"}), pinned=())
    return BudgetModel(**{**base, **over})


def test_given_unfolded_tier1_when_assigned_then_it_is_its_own_budget_category():
    assert _model().assign("Dining Out", "FOOD_AND_DRINK_RESTAURANT", "Cafe") == "Dining Out"


def test_given_folded_tier1_when_assigned_then_the_folded_category():
    assert _model().assign("Services", "GENERAL_SERVICES_OTHER_GENERAL_SERVICES", "Co") == "Other"


def test_given_atom_exception_when_assigned_then_it_beats_the_fold():
    got = _model().assign("Personal Care", "PERSONAL_CARE_GYMS_AND_FITNESS_CENTERS", "Gym")
    assert got == "Healthcare"


def test_given_dropped_tier1_when_assigned_then_none_even_if_a_phrase_matches():
    model = _model(pinned=(("Other", ("irs",)),))
    assert model.assign("Taxes", "GOVERNMENT_AND_NON_PROFIT_TAX_PAYMENT", "IRS") is None


def test_given_pinned_phrase_when_assigned_then_it_beats_atom_and_fold():
    model = _model(pinned=(("House", ("wf *",)),))
    assert model.assign("Merchandise", "GENERAL_MERCHANDISE_OTHER", "WF ******************") == "House"


def test_given_overlapping_pinned_phrases_when_assigned_then_the_first_rule_wins():
    model = _model(pinned=(("Dining Out", ("uber eats",)), ("Transportation", ("uber",))))
    assert model.assign("Transportation", "a", "UBER EATS") == "Dining Out"
    assert model.assign("Dining Out", "a", "Uber Trip") == "Transportation"


def test_given_phrase_inside_a_longer_word_when_assigned_then_not_pinned():
    model = _model(pinned=(("Transportation", ("uber",)), ("House", ("wf *",))))
    assert model.assign("Dining Out", "a", "Hubert's Diner") == "Dining Out"
    assert model.assign("Groceries", "a", "WFM Whole Foods") == "Groceries"


def test_given_blank_description_when_assigned_then_falls_through_to_the_fold():
    model = _model(pinned=(("House", ("wf *",)),))
    assert model.assign("Services", "a", None) == "Other"


def test_given_shipped_generic_config_when_every_taxonomy_atom_assigned_then_a_budget_category_or_dropped():
    taxonomy = yaml.safe_load((CONFIG_DIR / "taxonomy.yaml").read_text(encoding="utf-8"))
    model = load_budget_model(GENERIC, Path("/nonexistent/budget.yaml"))

    assigned = {model.assign(t["tier1"], atom, "") for atom, t in taxonomy["atoms"].items()}

    assert assigned - {None} <= BUDGET_CATEGORIES


def _write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def test_given_personal_file_when_loaded_then_it_layers_over_the_generic_mapping(tmp_path):
    generic = _write(tmp_path / "g.yaml", "fold: {Services: Other}\natoms: {A: House}\ndrop: [Taxes]\n")
    personal = _write(tmp_path / "p.yaml", """
fold: {Services: Merchandise}
drop: [Fees]
pinned:
  - {category: Dining Out, match: [DoorDash, "Uber Eats"]}
  - {category: Transportation, match: [uber]}
goals: {Dining Out: 100}
""")

    model = load_budget_model(generic, personal)

    assert model.fold == {"Services": "Merchandise"}
    assert model.atoms == {"A": "House"}
    assert model.drop == {"Taxes", "Fees"}
    assert model.pinned == (("Dining Out", ("doordash", "uber eats")), ("Transportation", ("uber",)))


def test_given_malformed_pinned_entries_when_loaded_then_skipped_not_fatal(tmp_path):
    personal = _write(tmp_path / "p.yaml", """
pinned:
  - not a mapping
  - {category: "", match: [x]}
  - {category: House}
  - {category: House, match: ["", "  "]}
  - {category: House, match: [ok]}
""")

    assert load_budget_model(tmp_path / "none.yaml", personal).pinned == (("House", ("ok",)),)


def test_given_unreadable_yaml_when_loaded_then_generic_defaults_only(tmp_path):
    generic = _write(tmp_path / "g.yaml", "fold: {Services: Other}\n")
    personal = _write(tmp_path / "p.yaml", "fold: [unclosed\n")

    model = load_budget_model(generic, personal)

    assert model.fold == {"Services": "Other"} and model.pinned == ()


def test_given_no_config_files_when_loaded_then_an_empty_model(tmp_path):
    assert load_budget_model(tmp_path / "a.yaml", tmp_path / "b.yaml") == BudgetModel()


def _frame(rows):
    return pd.DataFrame(rows, columns=["flow", "tier1", "atom", "description"])


def test_given_spend_income_and_excluded_rows_when_annotated_then_only_spend_is_categorised():
    df = _frame([("spend", "Services", "S", "Plumber"),
                 ("income", "Income", "I", "Payroll"),
                 ("excluded", "Transfer Out", "T", "Savings"),
                 ("spend", "Taxes", "X", "IRS")])

    got = annotate(df, _model())

    assert got["budget_cat"].tolist() == ["Other", None, None, None]
    assert "budget_cat" not in df.columns


def test_given_no_spend_rows_when_annotated_then_column_exists_and_is_empty():
    got = annotate(_frame([("income", "Income", "I", "Payroll")]), _model())

    assert got["budget_cat"].isna().all()


def test_given_empty_frame_when_annotated_then_returned_unchanged():
    empty = pd.DataFrame()

    assert annotate(empty, _model()) is empty
