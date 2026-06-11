"""Sidebar filters + granularity actually reshape the views (and never crash)."""
from datetime import date, timedelta

from tests.ui._harness import metric, money_to_f, texts, widget


def test_given_merchant_granularity_when_selected_then_table_groups_by_merchant(at):
    widget(at.radio, label="Table granularity").set_value("merchant")
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert "**Spend by Merchant**" in texts(at)
    assert at.dataframe[0].value.index.name == "Merchant"


def test_given_person_filter_when_applied_then_total_narrows(at, cube_df):
    base = money_to_f(metric(at, "Total"))
    spend = cube_df[cube_df["flow"] == "spend"]
    top_person = spend.groupby("person")["spend"].sum().idxmax()
    widget(at.multiselect, label="Person").set_value([top_person])
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    filtered = money_to_f(metric(at, "Total"))
    assert 0 < filtered <= base + 1, (filtered, base)


def test_given_narrowed_date_range_when_applied_then_total_shrinks_or_holds(at, cube_df):
    base = money_to_f(metric(at, "Total"))
    dates = cube_df["date_resolved"].dropna().astype(str)
    dmin, dmax = date.fromisoformat(dates.min()[:10]), date.fromisoformat(dates.max()[:10])
    start = max(dmin, dmax - timedelta(days=30))
    widget(at.date_input, label="Date range").set_value((start, dmax))
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    narrowed = money_to_f(metric(at, "Total"))
    assert 0 <= narrowed <= base + 1, (narrowed, base)


def test_given_income_flow_when_selected_then_all_views_render_without_exception(at):
    widget(at.radio, label="Flow").set_value("income")
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
