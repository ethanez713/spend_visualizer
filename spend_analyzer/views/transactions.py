"""Transactions: find specific rows by text, category, amount or review state."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from cube import Cube, GroupingSpec
from viz import money, sanitize_for_csv
from views._widgets import transaction_detail

_SORTS = {"Newest": ("date_resolved", False), "Oldest": ("date_resolved", True),
          "Largest": ("abs_amount", False), "Smallest": ("abs_amount", True)}
_EXPORT_COLS = ["date_resolved", "name", "merchant", "tier1", "tier2", "atom", "flow",
                "display_amount", "person", "account_name"]


def search(rows: pd.DataFrame, text: str) -> pd.DataFrame:
    """Rows whose description or merchant contains every whitespace-separated term."""
    hay = (rows["name"].fillna("") + " " + rows["merchant"].fillna("")).str.lower()
    mask = pd.Series(True, index=rows.index)
    for term in text.lower().split():
        mask &= hay.str.contains(term, regex=False)
    return rows[mask]


def render(cube: Cube, spec: GroupingSpec) -> None:
    rows = cube.filtered(GroupingSpec(filters=spec.filters, date_from=spec.date_from,
                                      date_to=spec.date_to, include_hidden=True))
    c = st.columns([4, 3, 2, 2])
    text = c[0].text_input("Search", key="txn_search",
                           placeholder="Description or merchant")
    cats = c[1].multiselect("Category", sorted(rows["tier1"].dropna().unique()),
                            key="txn_cats", placeholder="All")
    min_amount = c[2].number_input("Minimum amount", min_value=0.0, value=0.0, step=50.0,
                                   key="txn_min")
    sort = c[3].selectbox("Sort", list(_SORTS), key="txn_sort")
    has_pending = "review_pending" in rows.columns
    pending_only = has_pending and st.toggle("Only rows with a pending suggestion",
                                             key="txn_pending")

    out = search(rows, text) if text else rows
    if cats:
        out = out[out["tier1"].isin(cats)]
    if min_amount:
        out = out[out["abs_amount"] >= min_amount]
    if pending_only:
        out = out[out["review_pending"].fillna(False).astype(bool)]

    if out.empty:
        st.info("No transactions match. Widen the period in the sidebar or clear a filter.")
        return
    col, ascending = _SORTS[sort]
    transaction_detail(out, title="Matching", key="txn_tab", sort=col, ascending=ascending,
                       max_rows=25)
    export = out[[c for c in _EXPORT_COLS if c in out.columns]]
    st.download_button(f"Download {len(out)} rows as CSV · {money(out['display_amount'].sum())}",
                       sanitize_for_csv(export).to_csv(index=False),
                       file_name="transactions.csv", mime="text/csv")
