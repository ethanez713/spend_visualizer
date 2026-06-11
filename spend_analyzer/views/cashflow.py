"""Cash flow view: monthly income vs spend vs net, cumulative net (PLAN.md §10C)."""
from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from cube import Cube, GroupingSpec
from viz import money


def render(cube: Cube, spec: GroupingSpec) -> None:
    monthly = cube.rollup(
        GroupingSpec(
            group_by=["month"],
            filters={k: v for k, v in spec.filters.items() if k != "flow"},
            date_from=spec.date_from, date_to=spec.date_to,
            measures=["spend", "income", "net"], order_by=None,
        )
    )
    if monthly.empty:
        st.info("No data matches the current filters.")
        return
    monthly = monthly.sort_values("month")
    monthly["savings"] = monthly["income"] - monthly["spend"]
    monthly["cumulative_net"] = monthly["savings"].cumsum()

    c1, c2, c3 = st.columns(3)
    c1.metric("Total income", money(monthly["income"].sum()))
    c2.metric("Total spend", money(monthly["spend"].sum()))
    c3.metric("Net savings", money(monthly["savings"].sum()))

    fig = go.Figure()
    fig.add_bar(x=monthly["month"], y=monthly["income"], name="Income",
                marker_color="#2e7d32")
    fig.add_bar(x=monthly["month"], y=-monthly["spend"], name="Spend",
                marker_color="#c62828")
    fig.add_scatter(x=monthly["month"], y=monthly["savings"], name="Net",
                    mode="lines+markers", line=dict(color="#1565c0", width=3))
    fig.update_layout(barmode="relative", height=420, margin=dict(t=10),
                      yaxis_title="$ (spend shown negative)")
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Cumulative net")
    figc = go.Figure()
    figc.add_scatter(x=monthly["month"], y=monthly["cumulative_net"],
                     fill="tozeroy", mode="lines+markers", line=dict(color="#1565c0"))
    figc.update_layout(height=320, margin=dict(t=10), yaxis_title="Cumulative net $")
    st.plotly_chart(figc, use_container_width=True)

    st.dataframe(
        monthly[["month", "income", "spend", "savings", "cumulative_net"]],
        use_container_width=True, hide_index=True,
        column_config={
            c: st.column_config.NumberColumn(format="$%.0f")
            for c in ("income", "spend", "savings", "cumulative_net")
        },
    )
