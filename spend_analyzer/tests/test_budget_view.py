"""The Budget view's data layer: the model pivot and the two-source comparison."""
import pandas as pd

from cube import Cube, GroupingSpec
from budget_table import apply_offsets
from config_io import Budget, Offset, load_budget
from views.budget import (LEDGER_SOURCE, MODEL_SOURCE, _model_pivot, add_pivots, compare_averages,
                          offset_source_pivot, without_sources)


def _cube(rows):
    df = pd.DataFrame(rows, columns=["date_resolved", "flow", "budget_cat", "person", "spend"])
    df["month"] = df["date_resolved"].str[:7]
    df["budget_amount"] = df["spend"].where(df["budget_cat"].notna(), 0.0)
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


def test_given_refund_and_uncategorised_rows_when_pivoted_then_refund_nets_and_they_are_absent():
    cube = _cube([("2026-04-02", "spend", "House", "A", 100.0),
                  ("2026-04-09", "spend", "House", "A", -40.0),
                  ("2026-04-10", "spend", None, "A", 999.0),
                  ("2026-04-11", "income", None, "A", 5000.0)])

    pivot = _model_pivot(cube, _spec())

    assert list(pivot.index) == ["House"] and pivot.loc["House", "2026-04"] == 60.0


def test_given_person_and_date_filters_when_pivoted_then_both_apply():
    cube = _cube([("2026-04-02", "spend", "Travel", "Alice", 10.0),
                  ("2026-04-03", "spend", "Travel", "Bob", 20.0),
                  ("2026-05-03", "spend", "Travel", "Alice", 40.0)])

    pivot = _model_pivot(cube, _spec(filters={"person": ["Alice"]}, date_to="2026-04-30"))

    assert pivot.loc["Travel"].to_dict() == {"2026-04": 10.0}


def test_given_nothing_categorised_when_pivoted_then_empty_frame():
    assert _model_pivot(_cube([("2026-04-02", "income", None, "A", 5.0)]), _spec()).empty


def test_given_an_edit_counted_a_non_spend_row_when_pivoted_then_it_is_included_whatever_the_flow():
    cube = _cube([("2026-04-02", "excluded", "Travel", "A", 40.0)])

    assert _model_pivot(cube, _spec(filters={"flow": "spend"})).loc["Travel", "2026-04"] == 40.0


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
    rows = _imported(("Alice", "2026-02-10", "Rent", 7.0), ("Bob", "2026-02-11", "Rent", 3.0),
                     ("Alice", "2025-12-01", "Rent", 9.0))

    out = _add_legacy(pd.DataFrame(), rows, _spec(filters={"person": ["Alice"]}, date_from="2026-01-01"))

    assert out.loc["Rent"].to_dict() == {"2026-02": 7.0}


def test_given_no_imported_rows_when_added_then_pivot_unchanged():
    from views.budget import _add_legacy
    pivot = pd.DataFrame({"2026-04": [10.0]}, index=["House"])

    assert _add_legacy(pivot, pd.DataFrame(), _spec()) is pivot


def test_given_transfer_rows_when_filtered_then_only_unclassified_excluded_transfers_remain():
    from views.spending import unclassified_transfers
    rows = pd.DataFrame({
        "flow": ["excluded", "excluded", "excluded", "spend", "excluded"],
        "atom": ["TRANSFER_OUT_FROM_APPS", "TRANSFER_IN_FROM_APPS", "TRANSFER_OUT_FROM_APPS",
                 "TRANSFER_OUT_FROM_APPS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"],
        "budget_edit_id": ["", "", "e1", "", ""]})

    assert unclassified_transfers(rows).index.tolist() == [0, 1]


OFFSET = Offset("Pay App", {"Dining": 0.5, "Travel": 0.5})


def _offset_cube():
    return _cube([("2026-01-05", "spend", "Dining", "Alice", 100.0),
                  ("2026-01-06", "spend", "Travel", "Alice", 300.0),
                  ("2026-01-07", "income", "Pay App", "Alice", -40.0),
                  ("2026-02-07", "income", "Pay App", "Bob", -60.0)])


def test_given_ledger_without_source_rows_when_borrowed_and_offset_then_it_equals_the_model():
    cube = _offset_cube()
    model = _model_pivot(cube, _spec())
    ledger = model.drop(index="Pay App")

    borrowed = add_pivots(ledger, offset_source_pivot(cube, _spec(), ["Pay App"]))

    pd.testing.assert_frame_equal(apply_offsets(borrowed, [OFFSET])[0].sort_index(),
                                  apply_offsets(model, [OFFSET])[0].sort_index())


def test_given_person_and_other_filters_when_source_borrowed_then_only_person_narrows_it():
    spec = _spec(filters={"person": ["Bob"], "flow": ["spend"], "tier1": ["Dining"]})

    pivot = offset_source_pivot(_offset_cube(), spec, ["Pay App"])

    assert pivot.to_dict("index") == {"Pay App": {"2026-02": -60.0}}


def test_given_empty_ledger_when_source_borrowed_then_the_source_rows_remain():
    borrowed = add_pivots(pd.DataFrame(), offset_source_pivot(_offset_cube(), _spec(), ["Pay App"]))

    assert list(borrowed.index) == ["Pay App"]


def test_given_offsets_off_when_filtered_then_the_source_row_leaves_and_absent_is_harmless():
    pivot = _model_pivot(_offset_cube(), _spec())

    assert "Pay App" not in without_sources(pivot, [OFFSET]).index
    assert list(without_sources(pivot.drop(index="Pay App"), [OFFSET]).index) == ["Dining", "Travel"]


def test_given_yaml_without_or_with_offsets_enabled_when_loaded_then_default_is_true(tmp_path):
    path = tmp_path / "budget.yaml"
    path.write_text("goals: {Dining: 1}\n")
    assert load_budget(path).offsets_enabled is True and Budget().offsets_enabled is True
    path.write_text("goals: {Dining: 1}\noffsets_enabled: false\n")
    assert load_budget(path).offsets_enabled is False
