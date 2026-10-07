"""Merchants & recurring view (PLAN.md §10B) — with hide + correction tools."""
from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import recurring
import state
import theme
from cube import Cube, GroupingSpec
from viz import blank_if_missing, md_text, money, money_md, plural, style_table, table_height
from views._widgets import fix_categorization, hide_button, transaction_detail

_TOP_MERCHANTS = 40


def _merchant_table(rows: pd.DataFrame) -> pd.DataFrame:
    g = rows.groupby("merchant_id")
    out = g.agg(
        Merchant=("merchant", "first"),
        Logo=("logo_url", "first"),
        Category=("tier1", "first"),
        Total=("spend", "sum"),
        Visits=("count", "sum"),
        First=("date_resolved", "min"),
        Last=("date_resolved", "max"),
        _mid=("merchant_id", "first"),
    ).reset_index(drop=True)
    out["Avg"] = (out["Total"] / out["Visits"]).round(2)
    out["Logo"] = out["Logo"].map(blank_if_missing)   # no 'None' text for missing icons
    out = out[out["Total"] > 0].sort_values("Total", ascending=False)
    return out


def render(cube: Cube, spec: GroupingSpec, anchor: date) -> None:
    rows = cube.filtered(GroupingSpec(filters={**spec.filters, "flow": "spend"},
                                      date_from=spec.date_from, date_to=spec.date_to,
                                      include_hidden=True))
    if rows.empty:
        st.info("No spend matches the current filters.")
        return

    st.subheader("Top merchants")
    table = _merchant_table(rows).head(_TOP_MERCHANTS).reset_index(drop=True)
    table["_hidden"] = table["_mid"].map(lambda m: state.is_hidden("merchant_id", m))
    show = table[["Logo", "Merchant", "Category", "Total", "Visits", "Avg", "First", "Last"]]
    indexed = show.set_index("Merchant")
    grey = pd.Series(table["_hidden"].values, index=indexed.index)
    st.caption(f"The {len(table)} largest by spend. Select a row to open that merchant, "
               "or search every merchant below.")
    event = st.dataframe(
        style_table(indexed, money_cols=["Total", "Avg"], int_cols=["Visits"],
                    heat_cols=["Total"], grey_rows=grey),
        width="stretch", height=table_height(len(indexed), 12),
        on_select="rerun", selection_mode="single-row", key="merchant_pick",
        column_config={"Logo": st.column_config.ImageColumn("", width="small")},
    )
    picked = event.selection["rows"] if event and "rows" in event.selection else []
    everyone = sorted(rows.loc[~rows["refund"], "merchant"].dropna().unique())
    searched = st.selectbox("Merchant", everyone, index=None, key="merchant_search",
                            placeholder=f"Search {len(everyone)} merchants",
                            label_visibility="collapsed")
    # A fresh row click wins over an older search and the reverse, so whichever
    # control was used last decides the merchant shown.
    row_pick = table["Merchant"].iloc[picked[0]] if picked else None
    last = st.session_state.get("_merchant_last", (None, None))
    if (row_pick, searched) != last:
        st.session_state["_merchant_shown"] = (
            searched if searched != last[1] and searched else row_pick or searched)
        st.session_state["_merchant_last"] = (row_pick, searched)
    shown = st.session_state.get("_merchant_shown")
    if shown in everyone:
        _merchant_detail(rows, shown)

    _recurring(rows, anchor)


def _merchant_detail(rows: pd.DataFrame, pick: str) -> None:
    sub = rows[rows["merchant"] == pick]
    mid = sub["merchant_id"].iloc[0]
    st.subheader(md_text(pick))
    # Every month of the period, so a gap reads as a month without a visit.
    months = pd.period_range(rows["month"].min(), rows["month"].max(), freq="M").strftime("%Y-%m")
    monthly = sub.groupby("month")["spend"].sum().reindex(months, fill_value=0.0)
    visits = int((~sub["refund"]).sum())
    st.caption(f"{money_md(float(sub['spend'].sum()))} over {plural(visits, 'visit')} · "
               f"{sub['date_resolved'].min()[:10]} to {sub['date_resolved'].max()[:10]}")
    fig = go.Figure(go.Bar(x=pd.to_datetime(monthly.index).strftime("%b %Y"), y=monthly.values,
                           marker_color=theme.SPEND,
                           hovertemplate="%{y:$,.0f}<extra>%{x}</extra>"))
    fig.update_layout(height=180, xaxis=dict(type="category"),
                      yaxis=dict(tickprefix="$", tickformat=",.0f"))
    st.plotly_chart(fig, width="stretch", config={"displayModeBar": False})
    transaction_detail(sub, title=pick, key=f"merch_{mid}", sort="date_resolved")
    c1, c2 = st.columns([2, 3])
    with c1:
        hide_button("merchant_id", mid, pick, key=f"hide_merch_{mid}")
    with c2:
        # Default scope 'merchant': recategorizing from here usually means
        # "every transaction of this merchant", current and future.
        fix_categorization(row=sub.iloc[0], label=pick, key=f"merch_{mid}",
                           default_scope="merchant")


def _recurring(rows: pd.DataFrame, anchor: date) -> None:
    st.subheader("Recurring and subscriptions")
    rec = rows[(rows["recurrence"] == "recurring") & ~rows["hidden"]]
    if rec.empty:
        st.caption("No recurring merchants detected for these filters.")
        return
    sched = recurring.schedule(rec, anchor)
    due_soon = sched[sched["Next expected"].notna() & ~sched["Overdue"]
                     & (sched["Next expected"] <= (anchor + timedelta(days=30)).isoformat())]
    c = st.columns(3)
    c[0].metric("Monthly recurring cost", money(sched["Monthly"].sum()))
    c[1].metric("Expected in the next 30 days", money(due_soon["Typical charge"].sum()),
                help="Typical charge of every merchant whose next expected date falls "
                     "within 30 days of the latest transaction in the archive.")
    c[2].metric("Overdue", f"{int(sched['Overdue'].sum())} of {len(sched)} merchants",
                help="The next expected charge date has passed without a charge, which "
                     "usually means the subscription ended.")
    show = sched.drop(columns=["merchant_id", "Overdue"]).set_index("Merchant")
    show["Next expected"] = [f"{n} (overdue)" if o else n
                             for n, o in zip(sched["Next expected"], sched["Overdue"])]
    st.dataframe(
        style_table(show, money_cols=["Typical charge", "Monthly"], int_cols=["Charges"],
                    heat_cols=["Monthly"]),
        width="stretch", height=table_height(len(show)))
