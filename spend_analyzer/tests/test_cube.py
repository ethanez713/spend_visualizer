"""Integration tests: build the real cube and assert the invariants (PLAN.md §12)."""
import pandas as pd
import pytest

from cube import CATEGORY_LEVELS, Cube, GroupingSpec, topk_frontier
from data import build_cube


@pytest.fixture(scope="module")
def built():
    try:
        return build_cube()
    except FileNotFoundError as e:
        # The real archive is produced by the upstream pipeline (finance_pipeline /
        # categorize.py). On a fresh checkout it doesn't exist yet — skip, don't fail.
        pytest.skip(f"real archive not available yet — run the pipeline first ({e})")


def test_pipeline_loads(built):
    assert not built.df.empty
    assert built.qc["n_transactions"] > 0


def test_no_double_count_along_any_grouping(built):
    cube = built.cube
    grand = cube.total(GroupingSpec(filters={"flow": "spend"}))["spend"]
    for col in CATEGORY_LEVELS + ["person", "channel"]:
        g = cube.rollup(GroupingSpec(group_by=[col], filters={"flow": "spend"},
                                     measures=["spend"], order_by=None))
        assert g["spend"].sum() == pytest.approx(grand, abs=0.01), col


def test_cc_payments_excluded_mortgage_kept(built):
    df = built.df
    cc = df[df["pfc_detailed"] == "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"]
    if len(cc):
        assert (cc["flow"] == "excluded").all()
    mort = df[df["pfc_detailed"] == "LOAN_PAYMENTS_MORTGAGE_PAYMENT"]
    if len(mort):
        assert (mort["flow"] == "spend").all()


def test_spend_and_income_are_disjoint(built):
    df = built.df
    # a row contributes to exactly one of spend/income (excluded -> neither)
    both = df[(df["spend"] > 0) & (df["income"] > 0)]
    assert both.empty


def test_topk_frontier_grows_to_k():
    df = pd.DataFrame({
        "category_path": [["A", "x"], ["A", "y"], ["B", "z"], ["B", "z"], ["C", "w"]],
        "spend": [100.0, 50.0, 30.0, 20.0, 5.0],
    })
    front = topk_frontier(df, k=3)
    assert len(front) >= 3
    # largest node (A=150) should have been expanded first
    assert ("A", "x") in front or ("A",) in front


def test_avg_ticket_divides_by_spend_rows_only():
    """`count` covers income + excluded rows too, so it deflates the average.

    A rollup that isn't already filtered to flow='spend' (the Merchants and QC
    surfaces group without it) reported an avg ticket well below the real one.
    """
    df = pd.DataFrame({
        "merchant": ["Acme"] * 4,
        "flow": ["spend", "spend", "income", "excluded"],
        "spend": [30.0, 10.0, 0.0, 0.0],
        "income": [0.0, 0.0, 500.0, 0.0],
        "abs_amount": [30.0, 10.0, 500.0, 99.0],
        "count": [1, 1, 1, 1],
    })
    out = Cube(df).rollup(GroupingSpec(group_by=["merchant"], measures=["spend"],
                                       order_by=None))
    assert out.loc[0, "spend"] == 40.0
    assert out.loc[0, "count"] == 4          # every row in the group
    assert out.loc[0, "avg_ticket"] == 20.0  # but only the 2 spend rows average
    assert "_spend_rows" not in out.columns
