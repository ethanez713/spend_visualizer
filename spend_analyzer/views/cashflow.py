"""Cash flow: income against spending over time, and where the income went."""
from __future__ import annotations

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import theme
from cube import Cube, GroupingSpec
from viz import money, style_table, table_height

_TIMEFRAMES = {"Month": ["month"], "Quarter": ["year", "quarter"], "Year": ["year"]}
_SANKEY_CATEGORIES = 8


def render(cube: Cube, spec: GroupingSpec) -> None:
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
        _over_time(periods, timeframe)
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


def _over_time(p: pd.DataFrame, timeframe: str) -> None:
    fig = go.Figure()
    fig.add_bar(x=p["period"], y=p["income"], name="Income", marker_color=theme.INCOME,
                hovertemplate="%{y:$,.0f}<extra>Income</extra>")
    fig.add_bar(x=p["period"], y=-p["spend"], name="Spending", marker_color=theme.SPEND,
                customdata=p["spend"], hovertemplate="%{customdata:$,.0f}<extra>Spending</extra>")
    fig.add_scatter(x=p["period"], y=p["net"], name="Net", mode="lines+markers",
                    line=dict(color=theme.NET, width=2), marker=dict(size=8),
                    hovertemplate="%{y:$,.0f}<extra>Net</extra>")
    fig.update_layout(barmode="relative", height=380, hovermode="x unified",
                      xaxis=dict(type="category"),
                      yaxis=dict(tickprefix="$", tickformat=",.0f"))
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})

    table = p[["period", "income", "spend", "net", "rate", "cumulative"]].rename(columns={
        "period": timeframe, "income": "Income", "spend": "Spending", "net": "Net",
        "rate": "Saved", "cumulative": "Cumulative net"}).set_index(timeframe)
    st.dataframe(
        style_table(table, money_cols=["Income", "Spending", "Net", "Cumulative net"],
                    pct_cols=["Saved"], diverging={"Net": 0.0}, higher_is_better=["Net"]),
        width="stretch", height=table_height(len(table)))


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
