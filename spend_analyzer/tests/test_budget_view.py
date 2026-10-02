"""The Budget view's data layer: the model pivot and the two-source comparison."""
import pandas as pd

from cube import Cube, GroupingSpec
from views.budget import LEDGER_SOURCE, MODEL_SOURCE, _model_pivot, compare_averages


def _cube(rows):
    df = pd.DataFrame(rows, columns=["date_resolved", "flow", "budget_cat", "person", "spend"])
    df["month"] = df["date_resolved"].str[:7]
    df["count"] = 1
    df["hidden"] = False
    return Cube(df)


def _spec(**over):
    return GroupingSpec(**over)


def test_given_spend_rows_when_pivoted_then_category_by_month_sums():
    cube = _cube([("2026-04-02", "spend", "Dining Out", "A", 30.0),
                  ("2026-04-20", "spend", "Dining Out", "A", 20.0),
                  ("2026-05-02", "spend", "Dining Out", "A", 10.0),
                  ("2026-04-03", "spend", "Groceries", "A", 5.0)])

    pivot = _model_pivot(cube, _spec())

    assert pivot.loc["Dining Out"].to_dict() == {"2026-04": 50.0, "2026-05": 10.0}
    assert pivot.loc["Groceries", "2026-04"] == 5.0


def test_given_refund_and_dropped_rows_when_pivoted_then_refund_nets_and_dropped_is_absent():
    cube = _cube([("2026-04-02", "spend", "House", "A", 100.0),
                  ("2026-04-09", "spend", "House", "A", -40.0),
                  ("2026-04-10", "spend", None, "A", 999.0),
                  ("2026-04-11", "income", "Other", "A", 5000.0)])

    pivot = _model_pivot(cube, _spec())

    assert list(pivot.index) == ["House"] and pivot.loc["House", "2026-04"] == 60.0


def test_given_person_and_date_filters_when_pivoted_then_both_apply():
    cube = _cube([("2026-04-02", "spend", "Travel", "Jordan", 10.0),
                  ("2026-04-03", "spend", "Travel", "Ethan", 20.0),
                  ("2026-05-03", "spend", "Travel", "Jordan", 40.0)])

    pivot = _model_pivot(cube, _spec(filters={"person": ["Jordan"]}, date_to="2026-04-30"))

    assert pivot.loc["Travel"].to_dict() == {"2026-04": 10.0}


def test_given_no_spend_when_pivoted_then_empty_frame():
    assert _model_pivot(_cube([("2026-04-02", "income", "Other", "A", 5.0)]), _spec()).empty


def _pivot(**by_category):
    return pd.DataFrame(by_category, index=["2026-04", "2026-05"]).T


def test_given_sources_that_differ_when_compared_then_signed_difference_per_category():
    model = _pivot(Other=[100.0, 100.0], House=[50.0, 50.0])
    ledger = _pivot(Other=[400.0, 400.0], House=[50.0, 50.0])

    table = compare_averages(model, ledger, ["2026-04", "2026-05"], window=2)

    assert table.to_dict("records") == [
        {"Category": "Other", MODEL_SOURCE: 100.0, LEDGER_SOURCE: 400.0, "Difference": -300.0}]


def test_given_category_in_one_source_only_when_compared_then_other_side_is_zero():
    table = compare_averages(_pivot(Travel=[30.0, 30.0]), _pivot(Other=[0.5, 0.5]),
                             ["2026-04", "2026-05"], window=2)

    assert table.set_index("Category").loc["Travel", LEDGER_SOURCE] == 0.0
    assert "Other" not in set(table["Category"])


def test_given_sources_that_agree_when_compared_then_no_rows():
    same = _pivot(House=[50.0, 50.0])

    assert compare_averages(same, same.copy(), ["2026-04", "2026-05"], window=2).empty


def test_given_empty_pivots_when_compared_then_empty_not_error():
    assert compare_averages(pd.DataFrame(), pd.DataFrame(), [], window=3).empty


def _imported(*rows):
    df = pd.DataFrame(rows, columns=["person", "date", "category", "spend"])
    df["month"] = df["date"].str[:7]
    return df


def test_given_imported_rows_when_added_then_their_months_join_the_pivot_and_overlap_sums():
    from views.budget import _add_legacy
    pivot = pd.DataFrame({"2026-04": [10.0]}, index=["House"])
    rows = _imported(("A", "2026-02-10", "House", 5.0), ("A", "2026-04-01", "House", 1.0),
                     ("A", "2026-02-11", "Rent", 7.0))

    out = _add_legacy(pivot, rows, _spec())

    assert out.loc["House"].to_dict() == {"2026-02": 5.0, "2026-04": 11.0}
    assert out.loc["Rent", "2026-02"] == 7.0


def test_given_person_and_date_filters_when_imported_rows_added_then_both_apply():
    from views.budget import _add_legacy
    rows = _imported(("Jordan", "2026-02-10", "Rent", 7.0), ("Ethan", "2026-02-11", "Rent", 3.0),
                     ("Jordan", "2025-12-01", "Rent", 9.0))

    out = _add_legacy(pd.DataFrame(), rows, _spec(filters={"person": ["Jordan"]}, date_from="2026-01-01"))

    assert out.loc["Rent"].to_dict() == {"2026-02": 7.0}


def test_given_no_imported_rows_when_added_then_pivot_unchanged():
    from views.budget import _add_legacy
    pivot = pd.DataFrame({"2026-04": [10.0]}, index=["House"])

    assert _add_legacy(pivot, pd.DataFrame(), _spec()) is pivot
