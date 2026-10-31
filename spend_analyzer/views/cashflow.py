"""Cash flow: income against spending over time, and where the income went."""
from __future__ import annotations

from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import page_nav
import theme
from cube import Cube, GroupingSpec
from periods import add_months, month_end
from viz import money, style_table, table_height

_TIMEFRAMES = {"Month": ["month"], "Quarter": ["year", "quarter"], "Year": ["year"]}
_SANKEY_CATEGORIES = 8
_PARTIAL_OPACITY = 0.45


def render(cube: Cube, spec: GroupingSpec, earliest: date, anchor: date) -> None:
    # Income and spending are the two halves of this view, so Flow is ignored.
    filters = {k: v for k, v in spec.filters.items() if k != "flow"}
    base = dict(filters=filters, date_from=spec.date_from, date_to=spec.date_to)
    c1, c2 = st.columns([3, 2])
    view = c1.radio("View", ["Over time", "Where it went"], horizontal=True, key="cash_view")
    timeframe = "Month"
    if view == "Over time":
        timeframe = c2.radio("Group by", list(_TIMEFRAMES), horizontal=True,
                             key="cash_timeframe")

    periods = cube.rollup(GroupingSpec(group_by=_TIMEFRAMES[timeframe],
                                       measures=["spend", "income"], order_by=None, **base))
    if periods.empty:
        st.info("Nothing matches the current filters.")
        return
    periods = _label_periods(periods, timeframe)

    income, spend = float(periods["income"].sum()), float(periods["spend"].sum())
    net = income - spend
    c = st.columns(4)
    c[0].metric("Total income", money(income))
    c[1].metric("Total spend", money(spend))
    c[2].metric("Net savings", money(net))
    c[3].metric("Saved", f"{net / income * 100:.0f}% of income" if income > 0 else "—")

    if view == "Over time":
        _over_time(periods, timeframe, earliest, anchor)
    else:
        _sankey(cube, base, income, spend)


def _label_periods(periods: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    out = periods.copy()
    if timeframe == "Quarter":
        out["period"] = out["year"].astype(int).astype(str) + " " + out["quarter"]
    elif timeframe == "Year":
        out["period"] = out["year"].astype(int).astype(str)
    else:
        out["period"] = pd.to_datetime(out["month"]).dt.strftime("%b %Y")
        out = out.assign(_order=out["month"])
    out = out.sort_values("_order" if "_order" in out else "period").reset_index(drop=True)
    out["net"] = out["income"] - out["spend"]
    out["rate"] = (out["net"] / out["income"] * 100).where(out["income"] > 0)
    out["cumulative"] = out["net"].cumsum()
    return out


def _period_bounds(row, timeframe: str) -> tuple[date, date]:
    if timeframe == "Month":
        first = date.fromisoformat(f"{row['month']}-01")
        months = 1
    elif timeframe == "Quarter":
        first = date(int(row["year"]), 3 * int(str(row["quarter"]).removeprefix("Q")) - 2, 1)
        months = 3
    else:
        first = date(int(row["year"]), 1, 1)
        months = 12
    return first, month_end(add_months(first, months - 1))


def partial_periods(periods: pd.DataFrame, timeframe: str, earliest: date, anchor: date) -> pd.Series:
    """True where the archive covers a period only in part. Strict, unlike the Budget tab's
    averaging tolerance: the missing days are the ones a paycheck lands on."""
    bounds = [_period_bounds(row, timeframe) for _, row in periods.iterrows()]
    return pd.Series([earliest > first or anchor < last for first, last in bounds],
                     index=periods.index, dtype=bool)


def _coverage_note(first: date, last: date, earliest: date, anchor: date) -> str:
    start = f"from {earliest.day} {earliest:%b}" if earliest > first else ""
    end = f"to {anchor.day} {anchor:%b}" if anchor < last else ""
    return " ".join(part for part in (start, end) if part)


def _dotted_net(net: pd.Series, partial: pd.Series) -> pd.Series:
    """Net at each partial point and the points beside it, the segments drawn dotted."""
    return net.where(partial | partial.shift(1, fill_value=False) | partial.shift(-1, fill_value=False))


def _over_time(p: pd.DataFrame, timeframe: str, earliest: date, anchor: date) -> None:
    partial = partial_periods(p, timeframe, earliest, anchor)
    hover_note = partial.map({True: " (partial)", False: ""})
    bar_style = dict(opacity=partial.map({True: _PARTIAL_OPACITY, False: 1.0}).tolist(),
                     pattern=dict(shape=partial.map({True: "/", False: ""}).tolist()))
    fig = go.Figure()
    fig.add_bar(x=p["period"], y=p["income"], name="Income",
                marker=dict(color=theme.INCOME, **bar_style), customdata=hover_note,
                hovertemplate="%{y:$,.0f}%{customdata}<extra>Income</extra>")
    fig.add_bar(x=p["period"], y=-p["spend"], name="Spending",
                marker=dict(color=theme.SPEND, **bar_style),
                customdata=list(zip(p["spend"], hover_note)),
                hovertemplate="%{customdata[0]:$,.0f}%{customdata[1]}<extra>Spending</extra>")
    fig.add_scatter(x=p["period"], y=p["net"].where(~partial), name="Net", mode="lines+markers",
                    line=dict(color=theme.NET, width=2), marker=dict(size=8),
                    hovertemplate="%{y:$,.0f}<extra>Net</extra>")
    if partial.any():
        fig.add_scatter(x=p["period"], y=_dotted_net(p["net"], partial), mode="lines", showlegend=False,
                        line=dict(color=theme.NET, width=2, dash="dot"), hoverinfo="skip")
        fig.add_scatter(x=p["period"], y=p["net"].where(partial), mode="markers", showlegend=False,
                        name="Net", marker=dict(color=theme.NET, size=8, symbol="circle-open"),
                        hovertemplate="%{y:$,.0f} (partial)<extra>Net</extra>")
    fig.update_layout(barmode="relative", height=380, hovermode="x unified",
                      xaxis=dict(type="category"),
                      yaxis=dict(tickprefix="$", tickformat=",.0f"))
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
    if partial.any():
        st.caption("Lighter bars cover part of a month; their income and savings rate are not "
                   "comparable with settled months.")

    labels = [f"{period} ({_coverage_note(*_period_bounds(row, timeframe), earliest, anchor)})" if is_partial
              else period for period, is_partial, (_, row) in zip(p["period"], partial, p.iterrows())]
    table = p.assign(period=labels)[["period", "income", "spend", "net", "rate", "cumulative"]].rename(columns={
        "period": timeframe, "income": "Income", "spend": "Spending", "net": "Net",
        "rate": "Saved", "cumulative": "Cumulative net"}).set_index(timeframe)
    event = st.dataframe(
        style_table(table, money_cols=["Income", "Spending", "Net", "Cumulative net"],
                    pct_cols=["Saved"], diverging={"Net": 0.0}, higher_is_better=["Net"],
                    grey_rows=pd.Series(partial.values, index=table.index)),
        width="stretch", height=table_height(len(table)),
        on_select="rerun", selection_mode="single-row", key="cash_pick")
    st.caption("Select a row for its transactions.")
    picked = page_nav.picked_row(event)
    if picked is not None:
        first, last = _period_bounds(p.iloc[picked], timeframe)
        page_nav.open_spending(period=(max(first, earliest).isoformat(), min(last, anchor).isoformat()),
                               period_label=str(p["period"].iloc[picked]))


def _sankey(cube: Cube, base: dict, income: float, spend: float) -> None:
    """Income sources into one pool, out to the largest spend categories and what
    was kept. A shortfall is drawn as a source, so both sides balance."""
    # Income is a single top-level category, so its sources are the subcategories
    # (salary, wages, refunds, ...).
    sources = cube.rollup(GroupingSpec(group_by=["tier2"], measures=["income"],
                                       order_by="income", **base))
    sources = sources[sources["income"] > 0]
    uses = cube.rollup(GroupingSpec(group_by=["tier1"], measures=["spend"],
                                    order_by="spend", **base))
    uses = uses[uses["spend"] > 0]
    if sources.empty or uses.empty:
        st.info("This view needs both income and spending in the period.")
        return
    top, rest = uses.head(_SANKEY_CATEGORIES), uses.iloc[_SANKEY_CATEGORIES:]

    labels, colors, src, dst, val = ["Money in"], [theme.INK_700], [], [], []

    def node(name: str, color: str) -> int:
        labels.append(name)
        colors.append(color)
        return len(labels) - 1

    for _, r in sources.iterrows():
        src.append(node(str(r["tier2"]), theme.INCOME)); dst.append(0); val.append(r["income"])
    if spend > income:
        src.append(node("From savings", theme.OTHER)); dst.append(0); val.append(spend - income)
    palette = theme.series_colors(top["tier1"].tolist())
    for _, r in top.iterrows():
        src.append(0); dst.append(node(str(r["tier1"]), palette[r["tier1"]])); val.append(r["spend"])
    if not rest.empty:
        src.append(0); dst.append(node(f"{len(rest)} smaller categories", theme.OTHER))
        val.append(float(rest["spend"].sum()))
    if income > spend:
        src.append(0); dst.append(node("Saved", theme.INCOME)); val.append(income - spend)

    total = max(income, spend)
    fig = go.Figure(go.Sankey(
        arrangement="snap",
        node=dict(label=[f"{n} · {money(v)}" for n, v in zip(labels, _node_values(labels, src, dst, val, total))],
                  color=colors, pad=18, thickness=14, line=dict(color=theme.PAPER_0, width=1)),
        link=dict(source=src, target=dst, value=val, color=theme.LINK_WASH,
                  hovertemplate="%{source.label} → %{target.label}<extra></extra>"),
    ))
    fig.update_layout(height=520, font=dict(size=13, color=theme.INK_900))
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})


def _node_values(labels, src, dst, val, total) -> list[float]:
    out = [0.0] * len(labels)
    out[0] = total
    for s, d, v in zip(src, dst, val):
        out[d if s == 0 else s] = float(v)
    return out
