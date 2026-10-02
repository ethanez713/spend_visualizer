"""Offsets, projects, the monthly grid, and the goal/config parsing behind them."""
import pandas as pd

from budget_table import apply_offsets, monthly_grid, split_projects
from config_io import Offset, Project, load_budget

MONTHS = ["2026-04", "2026-05"]


def _pivot(**rows):
    return pd.DataFrame(rows, index=MONTHS).T


def test_given_offset_source_when_spread_then_targets_take_their_share_and_source_is_gone():
    pivot = _pivot(**{"Venmo - Income": [-400.0, -200.0], "Dining Out": [100.0, 100.0],
                      "Travel": [50.0, 50.0]})

    out, missing = apply_offsets(pivot, [Offset("Venmo - Income", {"Dining Out": 0.5, "Travel": 0.5})])

    assert out.loc["Dining Out"].tolist() == [-100.0, 0.0]
    assert out.loc["Travel"].tolist() == [-150.0, -50.0]
    assert "Venmo - Income" not in out.index and missing == []


def test_given_target_with_no_spend_when_spread_then_the_row_is_created():
    pivot = _pivot(**{"Venmo - Income": [-100.0, 0.0]})

    out, _ = apply_offsets(pivot, [Offset("Venmo - Income", {"Travel": 1.0})])

    assert out.loc["Travel"].tolist() == [-100.0, 0.0]


def test_given_weights_short_of_one_when_spread_then_the_remainder_stays_under_the_source():
    pivot = _pivot(**{"Venmo - Income": [-100.0, -100.0], "Travel": [0.0, 0.0]})

    out, _ = apply_offsets(pivot, [Offset("Venmo - Income", {"Travel": 0.25})])

    assert out.loc["Venmo - Income"].tolist() == [-75.0, -75.0]
    assert out.loc["Travel"].tolist() == [-25.0, -25.0]


def test_given_offset_source_absent_when_spread_then_reported_missing_and_pivot_unchanged():
    pivot = _pivot(Travel=[10.0, 20.0])

    out, missing = apply_offsets(pivot, [Offset("Venmo - Income", {"Travel": 1.0})])

    assert missing == ["Venmo - Income"] and out.equals(pivot)


def test_given_empty_pivot_when_offset_then_source_reported_missing():
    out, missing = apply_offsets(pd.DataFrame(), [Offset("Venmo - Income", {"Travel": 1.0})])

    assert missing == ["Venmo - Income"] and out.empty


def test_given_project_rows_when_split_then_removed_from_the_pivot_and_tracked_against_earmark():
    pivot = _pivot(**{"Remodel": [3000.0, 1000.0], "House": [200.0, 200.0]})

    rest, projects = split_projects(pivot, [Project("Remodel", 18000.0), Project("Unused", None)])

    assert list(rest.index) == ["House"]
    remodel, unused = projects.to_dict("records")
    assert (remodel["Spent"], remodel["Left"], round(remodel["% used"], 1)) == (4000.0, 14000.0, 22.2)
    assert unused["Spent"] == 0.0 and pd.isna(unused["Earmark"]) and pd.isna(unused["% used"])


def test_given_pivot_when_gridded_then_average_annualized_and_total_row():
    pivot = _pivot(House=[100.0, 300.0], Travel=[0.0, 60.0])

    grid = monthly_grid(pivot, ["2026-04", "2026-05"])

    assert grid.loc["House", ["Avg", "Annualized"]].tolist() == [200.0, 2400.0]
    assert grid.loc["Total", ["2026-04", "2026-05", "Avg"]].tolist() == [100.0, 360.0, 230.0]


def test_given_average_over_a_subset_of_months_when_gridded_then_only_those_months_divide():
    pivot = _pivot(House=[100.0, 300.0])

    assert monthly_grid(pivot, ["2026-05"]).loc["House", "Avg"] == 300.0


def test_given_empty_pivot_when_gridded_then_empty():
    assert monthly_grid(pd.DataFrame(), []).empty


def _budget(tmp_path, text):
    path = tmp_path / "budget.yaml"
    path.write_text(text)
    return load_budget(path)


def test_given_indexed_goal_when_asked_for_a_later_year_then_it_grows_by_the_inflation(tmp_path):
    budget = _budget(tmp_path, """
goal_index: {base_year: 2024, inflation: 0.03}
goals: {Dining Out: {base: 1100}, Mortgage: 4500}
""")

    assert round(budget.goal("Dining Out", 2026), 2) == 1166.99
    assert budget.goal("Dining Out", 2024) == 1100.0
    assert budget.goal("Mortgage", 2030) == 4500.0
    assert budget.goal_categories() == ["Mortgage", "Dining Out"]


def test_given_offsets_projects_and_averaging_when_loaded_then_parsed(tmp_path):
    budget = _budget(tmp_path, """
averaging: months_with_data
offsets:
  - {source: Venmo - Income, spread: {Dining Out: 0.5, Travel: 0.5}}
projects:
  - {name: Remodel, earmark: 18000}
  - {name: Trip}
""")

    assert budget.averaging == "months_with_data"
    assert budget.offsets == [Offset("Venmo - Income", {"Dining Out": 0.5, "Travel": 0.5})]
    assert budget.projects == [Project("Remodel", 18000.0), Project("Trip", None)]


def test_given_malformed_entries_when_loaded_then_skipped_and_defaults_apply(tmp_path):
    budget = _budget(tmp_path, """
averaging: sometimes
goals: {A: oops, B: {base: x}, C: 5}
offsets: [nope, {source: S}, {source: T, spread: {X: high}}]
projects: [nope, {earmark: 5}]
""")

    assert budget.averaging == "complete_months" and budget.goals == {"C": 5.0}
    assert budget.offsets == [] and budget.projects == [] and budget.indexed_goals == {}


def test_given_flat_goals_only_when_loaded_then_behaviour_is_unchanged(tmp_path):
    budget = _budget(tmp_path, "goals: {Mortgage: 4403, Other: 530}\n")

    assert budget.goal("Mortgage") == 4403.0 and budget.goal("Missing") is None
