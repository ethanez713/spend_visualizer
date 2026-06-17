"""Budget vs Actual view — the 2026 budget sheet (PLAN.md §14).

Compares each tier-1 category's trailing monthly running-average spend against
its monthly goal, with %-of-goal and $ diff heatmaps and the derived cuts
(Total, Annualized, Less Mortgage, Less Mortgage & Home).

Actuals come from one of two sources, both reduced to the same category × month
pivot before rendering:
  - the built-in tier1 cube (default), or
  - an external pre-categorized ledger CSV, when ``budget.ledger_csv`` is set —
    so the categories line up with the established (converter-defined) budget.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

import state
from config_io import Budget
from cube import Cube, GroupingSpec
from ledger import load_ledger, monthly_pivot as ledger_monthly_pivot
from viz import style_table


def _monthly_pivot(cube: Cube, spec: GroupingSpec) -> pd.DataFrame:
    monthly = cube.rollup(
        GroupingSpec(
            group_by=["tier1", "month"],
            filters={**spec.filters, "flow": "spend"},
            date_from=spec.date_from, date_to=spec.date_to,
            measures=["spend"], order_by=None, include_hidden=True,
        )
    )
    if monthly.empty:
        return pd.DataFrame()
    return monthly.pivot_table(index="tier1", columns="month", values="spend",
                               aggfunc="sum").fillna(0.0)


@st.cache_data(show_spinner=False)
def _cached_ledger(path: str, cache_key: tuple):
    # `cache_key` (path + mtime/size) is a normal (hashed) param ON PURPOSE: it
    # must bust the cache when the ledger is regenerated. Underscore-naming it
    # would make cache_data skip it and pin the result to the first load forever
    # (the same trap app.py's `_config_signature` calls out).
    return load_ledger(path)


def _ledger_pivot(path: str, spec: GroupingSpec) -> pd.DataFrame:
    """Category × month pivot from the external ledger, honoring the filters it
    can (date range + person); see ledger.py for why the rest don't apply."""
    st_info = Path(path).stat()
    ledger = _cached_ledger(path, (path, st_info.st_mtime, st_info.st_size))
    return ledger_monthly_pivot(
        ledger,
        persons=spec.filters.get("person") or None,
        date_from=spec.date_from, date_to=spec.date_to,
    )


def _running_avg(pivot: pd.DataFrame, window: int) -> pd.Series:
    if pivot.empty:
        return pd.Series(dtype=float)
    last = sorted(pivot.columns)[-window:]
    return pivot[last].mean(axis=1)


def _ytd_avg(pivot: pd.DataFrame) -> pd.Series:
    """Average monthly spend across the latest calendar year present in the data."""
    if pivot.empty:
        return pd.Series(dtype=float)
    latest_year = max(m[:4] for m in pivot.columns)
    ytd_months = [m for m in pivot.columns if m.startswith(latest_year)]
    return pivot[ytd_months].mean(axis=1)


def render(cube: Cube, spec: GroupingSpec, budget: Budget, window: int) -> None:
    st.subheader("Budget vs Actual")
    if not budget.goals:
        st.info("No `config/budget.yaml` found — add monthly goals to enable this view.")
        return

    ledger_csv = budget.resolved_ledger_csv
    if ledger_csv and Path(ledger_csv).exists():
        pivot = _ledger_pivot(ledger_csv, spec)
        st.caption(f"YTD average and trailing {window}-month running average vs monthly "
                   "goal. Actuals from the external categorized ledger — only the date "
                   "and person filters apply here.")
    else:
        if ledger_csv:
            st.warning(f"Configured budget ledger not found: `{ledger_csv}` — "
                       "falling back to the built-in categorization. Run the pipeline "
                       "to (re)generate it.")
        pivot = _monthly_pivot(cube, spec)
        st.caption(f"YTD average and trailing {window}-month running average vs monthly goal.")

    avg = _running_avg(pivot, window)
    ytd = _ytd_avg(pivot)
    avg_col = f"{window}mo Avg"
    cats = sorted(set(avg.index) | set(budget.goals.keys()))

    rows = []
    for cat in cats:
        a = float(avg.get(cat, 0.0))
        y = float(ytd.get(cat, 0.0))
        g = budget.goal(cat)
        rows.append({
            "Category": cat,
            "YTD Avg": y,
            avg_col: a,
            "Goal": g,
            "% of Goal": (a / g * 100.0) if g else None,
            "Budget Diff": (a - g) if g is not None else None,
            "_hidden": state.is_hidden("tier1", cat),
        })
    table = pd.DataFrame(rows)
    table = table[(table[avg_col] > 0) | (table["YTD Avg"] > 0)
                  | table["Goal"].notna()].reset_index(drop=True)

    indexed = table.drop(columns="_hidden").set_index("Category")
    grey_rows = pd.Series(table["_hidden"].values, index=indexed.index)
    styler = style_table(
        indexed,
        money_cols=["YTD Avg", avg_col, "Goal", "Budget Diff"],
        pct_cols=["% of Goal"],
        diverging={"% of Goal": 100.0, "Budget Diff": 0.0},
        grey_rows=grey_rows,
    )

    # --- derived cuts (exclude hidden categories from totals) ---
    vis = table[~table["_hidden"]]
    tot_a = float(vis[avg_col].sum())
    tot_g = float(vis["Goal"].dropna().sum())
    mort_a, mort_g = _cat(vis, "Mortgage", avg_col)
    house_a, house_g = _cat(vis, "House", avg_col)
    derived = pd.DataFrame(
        [
            ("Total (monthly)", tot_a, tot_g),
            ("Annualized", tot_a * 12, tot_g * 12),
            ("Less Mortgage", (tot_a - mort_a) * 12, (tot_g - mort_g) * 12),
            ("Less Mortgage & Home", (tot_a - mort_a - house_a) * 12,
             (tot_g - mort_g - house_g) * 12),
        ],
        columns=["", "Actual", "Goal"],
    )

    # Side by side on wide screens; the CSS media query stacks them when narrow.
    # Each table fills its column and is drag-resizable (global CSS).
    left, right = st.columns([3, 2])
    with left:
        st.dataframe(styler, use_container_width=True)
    with right:
        st.dataframe(style_table(derived.set_index(""), money_cols=["Actual", "Goal"]),
                     use_container_width=True)
        if table["_hidden"].any():
            hidden_names = table.loc[table["_hidden"], "Category"].tolist()
            st.caption(f"🙈 Hidden from totals: {', '.join(hidden_names)}")

    _trends(pivot, window)


def _trends(pivot: pd.DataFrame, window: int) -> None:
    """Monthly spend stacked by category, with a running-average overlay.

    Driven by the same category × month pivot as the table (so both views agree
    on the source), minus hidden categories."""
    st.subheader("Trends")
    if pivot.empty:
        st.caption("No spend to chart.")
        return
    visible = [c for c in pivot.index if not state.is_hidden("tier1", c)]
    piv = pivot.loc[visible].T.sort_index()         # rows = month, cols = category
    if piv.empty:
        st.caption("No spend to chart.")
        return
    fig = px.bar(piv, x=piv.index, y=list(piv.columns))
    roll = piv.sum(axis=1).rolling(min(window, len(piv)), min_periods=1).mean()
    fig.add_scatter(x=piv.index, y=roll, mode="lines+markers",
                    name=f"{window}-mo avg", line=dict(color="#e6e6e6", width=2))
    fig.update_layout(barmode="stack", height=430, margin=dict(t=10),
                      legend_title="Category", yaxis_title="Spend",
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      font=dict(color="#e6e6e6"))
    st.plotly_chart(fig, use_container_width=True)


def _cat(table: pd.DataFrame, name: str, avg_col: str) -> tuple[float, float]:
    row = table[table["Category"] == name]
    if row.empty:
        return 0.0, 0.0
    return float(row[avg_col].iloc[0]), float(row["Goal"].fillna(0).iloc[0])
