from datetime import date

import pandas as pd

import insights
from enrich import _merchant_dims
from models import CanonicalTransaction
import recurring
from views.transactions import search


def _rows(items):
    """items: (iso date, spend, merchant[, tier1])"""
    df = pd.DataFrame([{"date_resolved": d, "spend": s, "merchant": m, "merchant_id": m,
                        "tier1": (rest[0] if rest else "Cat"), "name": m.upper(),
                        "month": d[:7]} for d, s, m, *rest in items])
    return df


def test_cumulative_by_day_carries_the_total_across_days_without_spend():
    rows = _rows([("2026-09-02", 10.0, "a"), ("2026-09-02", 5.0, "b"),
                  ("2026-09-05", 20.0, "a"), ("2026-08-30", 99.0, "a")])
    cum = insights.cumulative_by_day(rows, date(2026, 9, 1), through_day=6)
    assert cum.tolist() == [0.0, 15.0, 15.0, 15.0, 35.0, 35.0]


def test_average_month_pace_skips_months_missing_from_the_archive():
    rows = _rows([("2026-08-01", 100.0, "a"), ("2026-07-01", 300.0, "a")])
    avg = insights.average_month_pace(rows, date(2026, 9, 15), months=3)
    assert avg.loc[1] == 200.0 and len(avg) == 28      # June absent: mean of two
    assert insights.average_month_pace(rows, date(2025, 1, 15), months=3) is None


def test_period_changes_ranks_by_size_of_change_in_either_direction():
    rows = _rows([("2026-09-03", 50.0, "a", "Food"), ("2026-08-03", 500.0, "a", "Travel"),
                  ("2026-09-04", 80.0, "a", "Fun"), ("2026-08-04", 70.0, "a", "Fun")])
    out = insights.period_changes(rows, "tier1", (date(2026, 9, 1), date(2026, 9, 30)),
                                  (date(2026, 8, 1), date(2026, 8, 31)))
    assert out.index.tolist() == ["Travel", "Food", "Fun"]
    assert out.loc["Travel", "Change"] == -500.0 and out.loc["Food", "Previous"] == 0.0


def test_monthly_merchant_gets_cadence_monthly_cost_and_next_date():
    rows = _rows([(f"2026-0{m}-15", 12.0, "stream") for m in (5, 6, 7, 8)])
    r = recurring.schedule(rows, as_of=date(2026, 8, 20)).iloc[0]
    assert (r["Cadence"], r["Monthly"], r["Next expected"]) == ("monthly", 12.0, "2026-09-14")
    assert not r["Overdue"]
    assert recurring.schedule(rows, as_of=date(2026, 10, 20)).iloc[0]["Overdue"]


def test_yearly_charge_costs_a_twelfth_per_month_not_its_full_amount():
    rows = _rows([("2024-06-01", 120.0, "annual"), ("2025-06-01", 120.0, "annual")])
    r = recurring.schedule(rows, as_of=date(2025, 7, 1)).iloc[0]
    assert r["Cadence"] == "yearly" and r["Monthly"] == 10.0


def test_irregular_merchant_gets_no_forecast_and_same_day_charges_are_one_event():
    rows = _rows([("2026-01-01", 5.0, "x"), ("2026-01-01", 5.0, "x"),
                  ("2026-01-04", 5.0, "x"), ("2026-03-20", 5.0, "x")])
    r = recurring.schedule(rows, as_of=date(2026, 4, 1)).iloc[0]
    assert r["Cadence"] == recurring.IRREGULAR and r["Next expected"] is None
    assert r["Charges"] == 3 and not r["Overdue"]


def test_search_matches_every_term_across_description_and_merchant():
    rows = _rows([("2026-01-01", 1.0, "Blue Bottle"), ("2026-01-02", 1.0, "Blue Apron"),
                  ("2026-01-03", 1.0, "Red Bottle")])
    assert search(rows, "blue BOTTLE")["merchant"].tolist() == ["Blue Bottle"]
    assert len(search(rows, "a+b")) == 0               # terms are literals, not regex


def _txn(**kw):
    fields = {f: None for f in CanonicalTransaction.__dataclass_fields__}
    return CanonicalTransaction(**{**fields, **kw})


def test_rows_without_a_merchant_are_identified_by_description_not_lumped_together():
    a = _merchant_dims(_txn(name="ACME LOAN MTG PMT"))
    b = _merchant_dims(_txn(name="CITY WATER DEPT"))
    again = _merchant_dims(_txn(name="Acme Loan  MTG PMT"))
    assert a["id"] != b["id"] and a["id"] == again["id"]
    assert _merchant_dims(_txn())["id"] == "unknown"


def test_refund_nets_against_its_category_and_only_income_categories_are_income():
    from enrich import enrich
    from taxonomy import load_taxonomy
    txns = [
        _txn(transaction_id="a", account_id="x", amount=100.0, direction="out", posted_date="2026-05-01",
             name="SHOP", pfc_primary="GENERAL_MERCHANDISE",
             pfc_detailed="GENERAL_MERCHANDISE_ONLINE_MARKETPLACES"),
        _txn(transaction_id="b", account_id="x", amount=-30.0, direction="in", posted_date="2026-05-03",
             name="SHOP", pfc_primary="GENERAL_MERCHANDISE",
             pfc_detailed="GENERAL_MERCHANDISE_ONLINE_MARKETPLACES"),
        _txn(transaction_id="c", account_id="x", amount=-2000.0, direction="in", posted_date="2026-05-15",
             name="PAYROLL", pfc_primary="INCOME", pfc_detailed="INCOME_WAGES"),
    ]
    df = enrich(txns, load_taxonomy()).set_index("transaction_id")
    assert df["spend"].sum() == 70.0 and df["income"].sum() == 2000.0
    assert (df.loc["b", "flow"], df.loc["b", "refund"], df.loc["b", "display_amount"]) == (
        "spend", True, -30.0)
    assert df.loc["c", "flow"] == "income"
