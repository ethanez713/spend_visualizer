"""Budget vs Actual view — the 2026 budget sheet (PLAN.md §14).

Compares each budget category's trailing monthly running-average spend against
its monthly goal, with %-of-goal and $ diff heatmaps and the derived cuts
(Total, Annualized, Less Mortgage, Less Mortgage & Home).

Actuals come from one of two sources, both reduced to the same category × month
pivot before rendering:
  - the budget model (``budget_model.py``): every filter applies, and an override
    made in the app shows at once; or
  - an external pre-categorized ledger CSV, when ``budget.ledger_csv`` is set —
    the converter's output, offered alongside the model while the two are compared.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

import legacy
import state
import theme
from budget_table import (apply_offsets, historic_detail, historic_summary, monthly_grid,
                          split_projects, yearly_averages)
from config_io import COMPLETE_MONTHS, MONTHS_WITH_DATA, Budget
from cube import Cube, GroupingSpec
from periods import CUSTOM, averaging_months, preset_range
from ledger import load_ledger, monthly_pivot as ledger_monthly_pivot
from viz import sanitize_for_csv, style_table, table_height


LEDGER_SOURCE, MODEL_SOURCE = "Converter ledger", "Budget model"
AVERAGING_LABELS = {COMPLETE_MONTHS: "Complete months", MONTHS_WITH_DATA: "Months with data"}


def _model_pivot(cube: Cube, spec: GroupingSpec) -> pd.DataFrame:
    """Budget spend by category and month. The sidebar's flow filter does not apply: what
    counts is decided by the budget model and the household's budget edits."""
    monthly = cube.rollup(
        GroupingSpec(
            group_by=["budget_cat", "month"],
            filters={k: v for k, v in spec.filters.items() if k != "flow"},
            date_from=spec.date_from, date_to=spec.date_to,
            measures=["budget_amount"], order_by=None, include_hidden=True,
        )
    ).dropna(subset=["budget_cat"])
    if monthly.empty:
        return pd.DataFrame()
    return monthly.pivot_table(index="budget_cat", columns="month", values="budget_amount",
                               aggfunc="sum").fillna(0.0)


def compare_averages(model: pd.DataFrame, ledger: pd.DataFrame, months: list[str],
                     window: int) -> pd.DataFrame:
    """Per-category trailing average from each source, for the categories where they
    differ by at least a dollar."""
    def avg(pivot):
        return _running_avg(pivot.reindex(columns=months, fill_value=0.0), window)
    a, b = avg(model), avg(ledger)
    rows = [{"Category": cat, MODEL_SOURCE: float(a.get(cat, 0.0)),
             LEDGER_SOURCE: float(b.get(cat, 0.0)),
             "Difference": float(a.get(cat, 0.0)) - float(b.get(cat, 0.0))}
            for cat in sorted(set(a.index) | set(b.index))]
    table = pd.DataFrame(rows, columns=["Category", MODEL_SOURCE, LEDGER_SOURCE, "Difference"])
    return table[table["Difference"].abs() >= 1].reset_index(drop=True)


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


def _legacy_rows(cube: Cube) -> pd.DataFrame:
    """Imported sheet rows from before the archive's first transaction."""
    if not legacy.LEDGER.exists():
        return pd.DataFrame()
    info = legacy.LEDGER.stat()
    rows = _cached_ledger(str(legacy.LEDGER), (str(legacy.LEDGER), info.st_mtime, info.st_size))
    return legacy.before_coverage(rows, str(cube.df["date_resolved"].dropna().min()))


def _reaching_legacy(spec: GroupingSpec, rows: pd.DataFrame) -> GroupingSpec:
    """The sidebar clamps every period to the archive's first date, so a preset cannot
    reach the imported months; recompute its start from the oldest imported row. A
    custom range is the user's own and is left alone."""
    preset = st.session_state.get("period_preset")
    if rows.empty or preset in (None, CUSTOM):
        return spec
    start, _ = preset_range(preset, date.fromisoformat(spec.date_to),
                            date.fromisoformat(rows["date"].min()))
    return replace(spec, date_from=str(start))


def _add_legacy(pivot: pd.DataFrame, rows: pd.DataFrame, spec: GroupingSpec) -> pd.DataFrame:
    if rows.empty:
        return pivot
    extra = ledger_monthly_pivot(rows, persons=spec.filters.get("person") or None,
                                 date_from=spec.date_from, date_to=spec.date_to)
    if extra.empty:
        return pivot
    return extra if pivot.empty else pivot.add(extra, fill_value=0.0)


def _goals_by_year(budget: Budget, years: list[str]) -> dict[str, dict[str, float]]:
    imported = legacy.load_goals()
    goals = {}
    for year in years:
        computed = {c: budget.goal(c, int(year)) for c in budget.goal_categories()}
        goals[year] = imported.get(int(year)) or {c: g for c, g in computed.items() if g is not None}
    return goals


def _history(cube: Cube, spec: GroupingSpec, budget: Budget, rows: pd.DataFrame) -> None:
    everything = replace(spec, date_from=None, date_to=None)
    full = _add_legacy(_model_pivot(cube, everything), rows, everything)
    full, _ = apply_offsets(full, budget.offsets)
    full, _ = split_projects(full, budget.projects)
    yearly = yearly_averages(full)
    if yearly.shape[1] < 2:
        return
    goals = _goals_by_year(budget, list(yearly.columns))
    with st.expander("History by year"):
        st.caption("Monthly average per year over the months that year has spending, against "
                   "that year's goals; the summary counts only categories that had a goal.")
        summary = historic_summary(yearly, goals).set_index("Year")
        st.dataframe(style_table(summary, money_cols=["Actual", "Goal", "Actual (annualized)",
                                                      "Goal (annualized)"], pct_cols=["% of goal"]),
                     width="stretch")
        detail = historic_detail(yearly, goals)
        st.dataframe(style_table(detail, money_cols=list(detail.columns)), width="stretch",
                     height=table_height(len(detail)))


def render(cube: Cube, spec: GroupingSpec, budget: Budget, window: int) -> None:
    original_spec = spec
    st.subheader("Budget vs actual")
    if not budget.goal_categories():
        st.info("No `config/budget.yaml` found — add monthly goals to enable this view.")
        return

    ledger_csv = budget.resolved_ledger_csv
    ledger_present = bool(ledger_csv and Path(ledger_csv).exists())
    if ledger_csv and not ledger_present:
        st.warning(f"Configured budget ledger not found: `{ledger_csv}` — "
                   "using the budget model. Run the pipeline to (re)generate it.")
    use_ledger = ledger_present and st.radio(
        "Actuals from", [LEDGER_SOURCE, MODEL_SOURCE], horizontal=True,
        key="budget_source") == LEDGER_SOURCE
    averaging = st.radio(
        "Average over", list(AVERAGING_LABELS), horizontal=True,
        index=list(AVERAGING_LABELS).index(budget.averaging),
        format_func=AVERAGING_LABELS.get, key="budget_averaging")
    imported = _legacy_rows(cube)
    if use_ledger:
        pivot = _ledger_pivot(ledger_csv, spec)
        st.caption(f"YTD average and trailing {window}-month running average vs monthly "
                   "goal. Actuals from the external categorized ledger — only the date "
                   "and person filters apply here.")
    else:
        shown = _reaching_legacy(spec, imported)
        pivot = _add_legacy(_model_pivot(cube, shown), imported, shown)
        st.caption(f"YTD average and trailing {window}-month running average vs monthly goal.")
        if not imported.empty:
            st.caption(f"Spending through {imported['date'].max()} comes from the imported sheet "
                       "history, to which only the date and person filters apply.")
        spec = shown

    pivot, missing_offsets = apply_offsets(pivot, budget.offsets)
    for source in missing_offsets:
        st.info(f"No “{source}” rows in this period, so it is not netted against "
                "the categories it offsets.")
    pivot, projects = split_projects(pivot, budget.projects)

    if averaging == COMPLETE_MONTHS:
        months = averaging_months(spec.date_from, spec.date_to, list(pivot.columns))
        st.caption(f"Averages are over the {len(months)} complete months in the period; a "
                   "month the period only clips is left out.")
    else:
        months = sorted(pivot.columns)
        st.caption(f"Averages are over the {len(months)} months that have spending, "
                   "partial months included.")
    averaged = pivot.reindex(columns=months, fill_value=0.0) if not pivot.empty else pivot
    avg = _running_avg(averaged, window)
    ytd = _ytd_avg(averaged)
    year = int(spec.date_to[:4])
    avg_col = f"{window}mo Avg"
    cats = sorted(set(avg.index) | set(budget.goal_categories()))

    rows = []
    for cat in cats:
        a = float(avg.get(cat, 0.0))
        y = float(ytd.get(cat, 0.0))
        g = budget.goal(cat, year)
        rows.append({
            "Category": cat,
            "YTD Avg": y,
            avg_col: a,
            "Goal": g if g is not None else float("nan"),
            "% of Goal": (a / g * 100.0) if g else float("nan"),
            "Budget Diff": (a - g) if g is not None else float("nan"),
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
        st.dataframe(styler, width="stretch", height=table_height(len(indexed)))
    with right:
        st.dataframe(style_table(derived.set_index(""), money_cols=["Actual", "Goal"]),
                     width="stretch")
        if table["_hidden"].any():
            hidden_names = table.loc[table["_hidden"], "Category"].tolist()
            st.caption(f"Hidden from totals: {', '.join(hidden_names)}")

    if not projects.empty:
        st.subheader("Projects")
        st.caption("Tracked against their earmark and left out of the monthly totals above.")
        st.dataframe(style_table(projects.set_index("Project"), money_cols=["Spent", "Earmark", "Left"],
                                 pct_cols=["% used"]), width="stretch")
    with st.expander("Monthly grid"):
        grid = monthly_grid(pivot, months)
        if grid.empty:
            st.caption("No spend in this period.")
        else:
            st.dataframe(style_table(grid, money_cols=list(grid.columns)), width="stretch",
                         height=table_height(len(grid)))
    _history(cube, spec, budget, imported)
    if ledger_present:
        _compare_sources(cube, original_spec, ledger_csv, window)
    _export_buttons(indexed, pivot)
    _trends(pivot, window)


def _compare_sources(cube: Cube, spec: GroupingSpec, ledger_csv: str, window: int) -> None:
    model, ledger = _model_pivot(cube, spec), _ledger_pivot(ledger_csv, spec)
    months = averaging_months(spec.date_from, spec.date_to,
                              sorted(set(model.columns) | set(ledger.columns)))
    with st.expander("Compare the budget model with the converter ledger"):
        diff = compare_averages(model, ledger, months, window)
        if diff.empty:
            st.caption("The two sources agree to the dollar in every category.")
            return
        st.caption(f"Trailing {window}-month average per category where they differ. "
                   "The ledger honors only the date and person filters, so clear the other "
                   "filters before reading a difference as a disagreement.")
        st.dataframe(style_table(diff.set_index("Category"),
                                 money_cols=[MODEL_SOURCE, LEDGER_SOURCE, "Difference"]),
                     width="stretch")


def _export_buttons(summary: pd.DataFrame, pivot: pd.DataFrame) -> None:
    """Paste-ready CSV downloads for the Google-Sheet copy/paste flow.

    Emits RAW numbers (not the on-screen ``$``/``%`` strings) so the target Sheet
    can re-sum them, and formula-escapes every cell (security baseline). Two shapes:
    the summary table as shown, and the full category × month grid.
    """
    c1, c2 = st.columns(2)
    c1.download_button(
        "Summary CSV",
        sanitize_for_csv(summary.reset_index()).to_csv(index=False),
        file_name="budget_vs_actual.csv", mime="text/csv", width="stretch",
    )
    if not pivot.empty:
        grid = pivot.round(2).reset_index().rename(columns={"budget_cat": "Category",
                                                            "category": "Category"})
        c2.download_button(
            "Monthly grid CSV",
            sanitize_for_csv(grid).to_csv(index=False),
            file_name="budget_monthly_grid.csv", mime="text/csv",
            width="stretch",
        )


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
    # Largest categories take the palette; the tail shares the neutral.
    order = list(piv.sum().sort_values(ascending=False).index)
    fig = px.bar(piv, x=piv.index, y=order, color_discrete_map=theme.series_colors(order))
    fig.update_traces(marker_line_color=theme.PAPER_1, marker_line_width=1)
    roll = piv.sum(axis=1).rolling(min(window, len(piv)), min_periods=1).mean()
    fig.add_scatter(x=piv.index, y=roll, mode="lines+markers",
                    name=f"{window}-month average", line=dict(color=theme.NET, width=2))
    fig.update_layout(barmode="stack", height=430, margin=dict(t=10),
                      xaxis_title=None, legend_title_text="",
                      yaxis=dict(title=None, tickprefix="$", tickformat=",.0f"))
    st.plotly_chart(fig, width="stretch")


def _cat(table: pd.DataFrame, name: str, avg_col: str) -> tuple[float, float]:
    row = table[table["Category"] == name]
    if row.empty:
        return 0.0, 0.0
    return float(row[avg_col].iloc[0]), float(row["Goal"].fillna(0).iloc[0])
