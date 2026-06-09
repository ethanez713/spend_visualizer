"""Merchants & recurring view (PLAN.md §10B) — with hide + correction tools."""
from __future__ import annotations

import pandas as pd
import streamlit as st

import state
from cube import Cube, GroupingSpec
from viz import blank_if_missing, money, style_table
from views._widgets import correction_form, hide_button, transaction_detail


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


def render(cube: Cube, spec: GroupingSpec) -> None:
    rows = cube.filtered(GroupingSpec(filters={**spec.filters, "flow": "spend"},
                                      date_from=spec.date_from, date_to=spec.date_to,
                                      include_hidden=True))
    if rows.empty:
        st.info("No spend matches the current filters.")
        return

    st.subheader("Top merchants")
    table = _merchant_table(rows).head(40)
    table["_hidden"] = table["_mid"].map(lambda m: state.is_hidden("merchant_id", m))
    show = table[["Logo", "Merchant", "Category", "Total", "Visits", "Avg", "First", "Last"]]
    indexed = show.set_index("Merchant")
    grey = pd.Series(table["_hidden"].values, index=indexed.index)
    st.dataframe(
        style_table(indexed, money_cols=["Total", "Avg"], int_cols=["Visits"],
                    green_cols=["Total"], grey_rows=grey),
        use_container_width=True,
        column_config={"Logo": st.column_config.ImageColumn("", width="small")},
    )

    # --- per-merchant detail: sparkline + transactions + hide + correct ---
    pick = st.selectbox("Merchant detail", table["Merchant"].tolist())
    if pick:
        sub = rows[rows["merchant"] == pick]
        mid = sub["merchant_id"].iloc[0]
        spark = sub.groupby("month")["spend"].sum().sort_index()
        st.line_chart(spark)
        transaction_detail(sub, title=pick, key=f"merch_{mid}")
        c1, c2 = st.columns([2, 3])
        with c1:
            hide_button("merchant_id", mid, pick, key=f"hide_merch_{mid}")
        with c2:
            first = sub.iloc[0]
            original = {"tier1": first.get("tier1"), "tier2": first.get("tier2"),
                        "pfc_detailed": first.get("pfc_detailed"),
                        "merchant_name": first.get("merchant")}
            correction_form(scope="merchant", original=original,
                            target={"label": pick, "merchant": pick, "merchant_id": str(mid)},
                            key=f"merch_{mid}")

    # --- recurring / subscriptions ---
    st.subheader("Recurring & subscriptions")
    rec = rows[(rows["recurrence"] == "recurring") & (~rows["hidden"] if "hidden" in rows else True)]
    if rec.empty:
        st.caption("No recurring merchants detected for these filters.")
        return
    rg = rec.groupby("merchant_id")
    rtab = rg.agg(
        Merchant=("merchant", "first"),
        Category=("tier1", "first"),
        Charges=("count", "sum"),
        Total=("spend", "sum"),
        Last=("date_resolved", "max"),
    ).reset_index(drop=True)
    rtab["Avg charge"] = (rtab["Total"] / rtab["Charges"]).round(2)
    rtab["Est. monthly"] = rtab["Merchant"].map(lambda m: _monthly_burden(rec, m))
    rtab = rtab.sort_values("Est. monthly", ascending=False)
    st.dataframe(
        style_table(rtab.set_index("Merchant"),
                    money_cols=["Total", "Avg charge", "Est. monthly"],
                    int_cols=["Charges"], green_cols=["Est. monthly"]),
        use_container_width=True,
    )
    st.metric("Total monthly subscription burden", money(rtab["Est. monthly"].sum()))


def _monthly_burden(rec: pd.DataFrame, merchant: str) -> float:
    sub = rec[rec["merchant"] == merchant]
    by_month = sub.groupby("month")["spend"].sum()
    return round(float(by_month.mean()), 2) if len(by_month) else 0.0
