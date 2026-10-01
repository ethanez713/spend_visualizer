"""Overview: how the selected period went, and how this month is pacing."""
from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import insights
import theme
from cube import Cube, GroupingSpec
from periods import add_months, month_start, previous_period
from viz import money, money_md, style_table, table_height
from views._widgets import transaction_detail

_PACE_OPTIONS = ["Last month", "Average month", "Same month last year"]
_AVERAGE_MONTHS = 3


def render(cube: Cube, spec: GroupingSpec, earliest: date, anchor: date) -> None:
    # Both flows on purpose: income and savings rate are meaningless under a
    # spend-only filter, so this tab ignores the Flow control.
    filters = {k: v for k, v in spec.filters.items() if k != "flow"}
    rows = cube.filtered(GroupingSpec(filters=filters))
    if rows.empty:
        st.info("Nothing matches the current filters.")
        return
    current = (date.fromisoformat(spec.date_from), date.fromisoformat(spec.date_to))
    previous = previous_period(*current)
    has_previous = previous[0] >= month_start(earliest)

    _headline(rows, current, previous if has_previous else None)
    st.divider()
    left, right = st.columns([3, 2])
    with left:
        _pace(rows, anchor)
    with right:
        _changes(rows, current, previous if has_previous else None, earliest, anchor)
    st.divider()
    _largest(rows, current)
    _pending(rows)


def _totals(rows: pd.DataFrame, span: tuple[date, date]) -> tuple[float, float]:
    d = rows["date_resolved"].str[:10]
    sel = rows[(d >= span[0].isoformat()) & (d <= span[1].isoformat())]
    return float(sel["spend"].sum()), float(sel["income"].sum())


def _headline(rows, current, previous) -> None:
    spend, income = _totals(rows, current)
    net = income - spend
    c = st.columns(4)
    if previous:
        p_spend, p_income = _totals(rows, previous)
        label = f"vs {previous[0]:%-d %b} – {previous[1]:%-d %b}"
        c[0].metric("Spending", money(spend), f"{_signed(spend - p_spend)} {label}",
                    delta_color="inverse")
        c[1].metric("Income", money(income), f"{_signed(income - p_income)} {label}")
    else:
        c[0].metric("Spending", money(spend))
        c[1].metric("Income", money(income))
    c[2].metric("Net", money(net))
    c[3].metric("Savings rate",
                f"{net / income * 100:.0f}%".replace("-", "−") if income > 0 else "—",
                help="Net as a share of income for the period.")


def _signed(x: float) -> str:
    # st.metric reads the delta's direction from a leading ASCII "-" or "+".
    return f"{'-' if x < 0 else '+'}{money(abs(x))}"


def _pace(rows: pd.DataFrame, anchor: date) -> None:
    st.subheader(f"Spending pace, {anchor:%B}")
    present = set(rows["month"].dropna())
    last_year = add_months(month_start(anchor), -12)
    options = [o for o in _PACE_OPTIONS if o != "Same month last year"
               or f"{last_year.year}-{last_year.month:02d}" in present]
    choice = st.radio("Compare with", options, horizontal=True, key="pace_compare")
    this = insights.cumulative_by_day(rows, anchor, through_day=anchor.day)
    ref, ref_name = None, choice.lower()
    if choice == "Average month":
        ref = insights.average_month_pace(rows, anchor, _AVERAGE_MONTHS)
        ref_name = f"average of prior {_AVERAGE_MONTHS} months"
    else:
        m = add_months(month_start(anchor), -1 if choice == "Last month" else -12)
        if f"{m.year}-{m.month:02d}" in present:
            ref = insights.cumulative_by_day(rows, m)
            ref_name = f"{m:%B %Y}"

    fig = go.Figure()
    if ref is not None:
        fig.add_scatter(x=ref.index, y=ref.values, name=ref_name, mode="lines",
                        line=dict(color=theme.COMPARISON, width=2, dash="dot"),
                        hovertemplate="day %{x}: %{y:$,.0f}<extra>" + ref_name + "</extra>")
    fig.add_scatter(x=this.index, y=this.values, name=f"{anchor:%B %Y}", mode="lines",
                    line=dict(color=theme.SPEND, width=2),
                    hovertemplate="day %{x}: %{y:$,.0f}<extra>this month</extra>")
    fig.update_layout(height=300, hovermode="x unified",
                      xaxis=dict(title="Day of month", dtick=5, range=[1, 31]),
                      yaxis=dict(tickprefix="$", tickformat=",.0f", rangemode="tozero"))
    st.plotly_chart(fig, use_container_width=True, config={"displayModeBar": False})

    so_far = float(this.iloc[-1])
    if ref is None:
        st.caption(f"{money_md(so_far)} through day {anchor.day}. "
                   f"The archive has no data for {ref_name}.")
        return
    same_point = float(ref.get(min(anchor.day, int(ref.index.max())), ref.iloc[-1]))
    diff = so_far - same_point
    st.caption(f"{money_md(so_far)} through day {anchor.day}, {money_md(abs(diff))} "
               f"{'more' if diff > 0 else 'less'} than {ref_name} at the same point.")


def _changes(rows, current, previous, earliest, anchor) -> None:
    st.subheader("Biggest changes")
    if previous is None:
        # The selected period has nothing before it in the archive; the month to
        # date against the same days of last month is the comparison that always exists.
        current = (month_start(anchor), anchor)
        previous = previous_period(*current)
        if previous[0] < month_start(earliest):
            st.caption("The archive holds a single month, so there is nothing to compare.")
            return
    spend = rows[rows["flow"] == "spend"]
    table = insights.period_changes(spend, "tier1", current, previous).head(10)
    table.index.name = "Category"
    st.dataframe(
        style_table(table, money_cols=["Current", "Previous", "Change"],
                    diverging={"Change": 0.0}),
        use_container_width=True, height=table_height(len(table)))
    st.caption(f"By category: {current[0]:%-d %b} – {current[1]:%-d %b} against "
               f"{previous[0]:%-d %b} – {previous[1]:%-d %b %Y}.")


def _largest(rows, current) -> None:
    d = rows["date_resolved"].str[:10]
    sel = rows[(rows["flow"] == "spend") & (d >= current[0].isoformat())
               & (d <= current[1].isoformat())]
    top = sel.nlargest(10, "abs_amount")
    st.subheader("Largest transactions")
    transaction_detail(top, title="Top 10 by amount", key="overview_top")


def _pending(rows) -> None:
    if "review_pending" not in rows.columns:
        return
    n = int(rows["review_pending"].fillna(False).astype(bool).sum())
    if n:
        st.caption(f"{n} category suggestions await review. Filter to them in the "
                   "Transactions tab, or adjudicate with `categorize.py --review`.")
