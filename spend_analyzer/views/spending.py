"""Spending: one transaction list that the spend table, the hierarchy chart and a search
all narrow.

Interaction model:
  • ``wheel_root`` is the single focus: a chart click, a table row or a breadcrumb sets it.
    Other pages open this one on a row's focus, filter or period (see ``page_nav``).
  • The search, category and amount filters stack on the focus and narrow only the list;
    the table and chart keep showing the whole of the sidebar's period.
  • "Spend by" controls only the table. The chart always renders to the deepest level.
    Click a slice to zoom in (captured via the streamlit-plotly-events component).
  • Hidden categories are dropped from the chart entirely (shown only as a note
    underneath) so they never blow a big empty wedge into the diagram.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from streamlit_plotly_events import plotly_events

import page_nav
import state
from budget_table import known_categories
from config_io import Budget
from cube import Cube, GroupingSpec, CATEGORY_LEVELS, CATEGORY_LEVEL_BY_NAME
import theme
from periods import averaging_months, month_span, month_start, previous_period
from viz import humanize_atom, money, plural, sanitize_for_csv, style_table, table_height
from views._budget_edit import bulk_panel
from views._widgets import correction_form, hide_button, transaction_detail

_LEVEL_LABELS = {
    "necessity": "Necessity", "tier1": "Category", "tier2": "Subcategory",
    "atom": "Atom", "merchant": "Merchant",
}
ROOT_KEY = "wheel_root"
PERIOD_KEY = "_spend_period"
BUDGET_KEY = "_spend_budget"
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


def unclassified_transfers(rows: pd.DataFrame) -> pd.DataFrame:
    """Transfers the budget ignores and nobody has decided about yet: a Venmo paid back,
    a group booking split, money moved between accounts."""
    return rows[(rows["flow"] == "excluded") & rows["atom"].str.startswith("TRANSFER_")
                & (rows["budget_edit_id"] == "")]


def _disp(col: str, v) -> str:
    return humanize_atom(v).capitalize() if col == "atom" else str(v)


def _level_control() -> tuple[str, str]:
    options = list(CATEGORY_LEVEL_BY_NAME.keys())
    level_name = st.selectbox("Spend by", options,
                              index=options.index(st.session_state.get("_level_name", "tier1")),
                              format_func=lambda n: _LEVEL_LABELS[n], key="spend_level")
    st.session_state["_level_name"] = level_name
    return level_name, CATEGORY_LEVEL_BY_NAME[level_name]


def _apply_pending_nav() -> None:
    nav = st.session_state.pop(page_nav.NAV_KEY, None)
    if not nav:
        return
    st.session_state[ROOT_KEY] = nav["focus"]
    st.session_state["txn_search"] = nav["filters"].get("search", "")
    st.session_state["txn_cats"] = []
    st.session_state["txn_min"] = 0.0
    st.session_state[BUDGET_KEY] = nav["filters"].get("budget_cat")
    st.session_state[PERIOD_KEY] = nav["period"]


def _in_temporary_period(spec: GroupingSpec) -> GroupingSpec:
    """A period opened from another page narrows this page only: the sidebar's stays as set."""
    period = st.session_state.get(PERIOD_KEY)
    return replace(spec, date_from=period[0], date_to=period[1]) if period else spec


def render(cube: Cube, spec: GroupingSpec, earliest: date, budget: Budget) -> None:
    _apply_pending_nav()
    spec = _in_temporary_period(spec)
    list_rows, narrowing = _list_controls(cube, spec)
    _kpis(cube, spec)
    period_rows = cube.filtered(GroupingSpec(filters={**spec.filters, "flow": "spend"},
                                             date_from=spec.date_from, date_to=spec.date_to,
                                             include_hidden=True))
    with st.container(key="spend_split"):
        left, right = st.columns(2)
        with left:
            _table_section(cube, spec, earliest)
        with right:
            root = _hierarchy(period_rows)
    _transaction_list(cube, budget, list_rows, root, narrowing)


def _list_controls(cube: Cube, spec: GroupingSpec) -> tuple[pd.DataFrame, dict]:
    """The filters above the page. Returns the rows they leave, before the focus applies,
    and what each filter is set to."""
    c = st.columns([4, 3, 2, 2])
    transfers_only = st.session_state.get("txn_transfers", False)
    budget_cat = st.session_state.get(BUDGET_KEY)
    # A budget category can hold rows outside the sidebar's flow, such as money in.
    filters = {k: v for k, v in spec.filters.items()
               if not ((transfers_only or budget_cat) and k == "flow")}
    rows = cube.filtered(GroupingSpec(filters=filters, date_from=spec.date_from,
                                      date_to=spec.date_to, include_hidden=True))
    if transfers_only:
        rows = unclassified_transfers(rows)
    text = c[0].text_input("Search", key="txn_search", placeholder="Description or merchant")
    cats = c[1].multiselect("Category", sorted(rows["tier1"].dropna().unique()),
                            key="txn_cats", placeholder="All")
    min_amount = c[2].number_input("Minimum amount", min_value=0.0, value=0.0, step=50.0,
                                   key="txn_min")
    sort = c[3].selectbox("Sort", list(_SORTS), key="txn_sort")
    t = st.columns(2)
    has_pending = "review_pending" in rows.columns
    pending_only = has_pending and t[0].toggle("Pending suggestions only", key="txn_pending")
    t[1].toggle("Transfers nobody has classified (ignores the Flow filter)", key="txn_transfers",
                help="Transfers are left out of the budget until you say what they were: spending, "
                     "money back, or not budget spending at all.")

    out = search(rows, text) if text else rows
    if budget_cat:
        out = out[out["budget_cat"] == budget_cat]
    if cats:
        out = out[out["tier1"].isin(cats)]
    if min_amount:
        out = out[out["abs_amount"] >= min_amount]
    if pending_only:
        out = out[out["review_pending"].fillna(False).astype(bool)]
    return out, {"text": text, "cats": cats, "min_amount": min_amount, "sort": sort,
                 "transfers_only": transfers_only, "budget_cat": budget_cat}


def _kpis(cube: Cube, spec: GroupingSpec) -> None:
    months = _averaging_months(cube, spec)
    monthly = _monthly_visible_spend(cube, spec).reindex(months, fill_value=0.0)
    visible = cube.rollup(GroupingSpec(group_by=["tier1"], filters=spec.filters, date_from=spec.date_from,
                                       date_to=spec.date_to, measures=["spend"], order_by=None))
    visible_total = float(visible["spend"].sum()) if not visible.empty else 0.0
    mortgage = _tier1_spend(cube, spec, "Mortgage")
    house = _tier1_spend(cube, spec, "House")
    d = st.columns(4)
    d[0].metric("Total", money(visible_total))
    d[1].metric("Annualized", money(float(monthly.mean()) * 12),
                help=f"Average of the {len(months)} complete months in the period, times 12. "
                     "Every spending category, from the Plaid categories.")
    d[2].metric("Less Mortgage", money(visible_total - mortgage))
    d[3].metric("Less Mortgage & Home", money(visible_total - mortgage - house))


def _table_section(cube: Cube, spec: GroupingSpec, earliest: date) -> None:
    level_name, level_col = _level_control()
    table = cube.rollup(GroupingSpec(group_by=[level_col], filters=spec.filters,
                                     date_from=spec.date_from, date_to=spec.date_to,
                                     measures=["spend", "count"], order_by="spend",
                                     include_hidden=True))
    if table.empty:
        st.info("No spend matches the current filters.")
        return
    raw_vals = table[level_col].tolist()
    hidden_flags = [state.is_hidden(level_col, v) for v in raw_vals]
    st.caption("Tick one row to focus on it; tick several to Hide or Flag them.")
    _spend_table(cube, spec, level_col, level_name, table, raw_vals, hidden_flags, earliest)


def _clear_filter(key: str, empty) -> None:
    st.session_state[key] = empty


def _showing(root: list, narrowing: dict) -> None:
    """The narrowing in force, each part with its own ×."""
    chips = []
    period = st.session_state.get(PERIOD_KEY)
    if period:
        chips.append((f"Period: {period[2]}", PERIOD_KEY, None))
    if narrowing["budget_cat"]:
        chips.append((f"Budget: {narrowing['budget_cat']}", BUDGET_KEY, None))
    if root:
        chips.append((" › ".join(_disp(CATEGORY_LEVELS[i], v) for i, v in enumerate(root)), ROOT_KEY, []))
    if narrowing["text"]:
        chips.append((f"search “{narrowing['text']}”", "txn_search", ""))
    if narrowing["cats"]:
        chips.append((", ".join(narrowing["cats"]), "txn_cats", []))
    if narrowing["min_amount"]:
        chips.append((f"at least {money(narrowing['min_amount'])}", "txn_min", 0.0))
    if not chips:
        return
    with st.container(horizontal=True, gap="small", vertical_alignment="center"):
        st.caption("Showing:")
        for label, key, empty in chips:
            st.button(f"{label} ×", key=f"chip_{key}", on_click=_clear_filter, args=(key, empty))


def _transaction_list(cube: Cube, budget: Budget, rows: pd.DataFrame, root: list, narrowing: dict) -> None:
    out = _drill_filter(rows, root)
    _showing(root, narrowing)
    if out.empty:
        st.info("No transactions match. Widen the period in the sidebar or clear a filter.")
        return
    if root:
        dim, val = CATEGORY_LEVELS[len(root) - 1], root[-1]
        hide_button(dim, val, _disp(dim, val), key=f"hide_drill_{dim}_{val}")
    crumb = " › ".join(_disp(CATEGORY_LEVELS[i], v) for i, v in enumerate(root)) or "All transactions"
    col, ascending = _SORTS[narrowing["sort"]]
    transaction_detail(out, title=crumb, key=f"list_{'_'.join(map(str, root))}", sort=col,
                       ascending=ascending, max_rows=40)
    if (root or narrowing["text"] or narrowing["cats"] or narrowing["min_amount"]
            or narrowing["transfers_only"] or narrowing["budget_cat"]):
        bulk_panel(out, search=narrowing["text"], key="txn_budget",
                   categories=known_categories(budget, cube.df["budget_cat"].dropna().unique()))
    else:
        st.caption("Filter the rows above to set a budget category in bulk.")
    export = out[[c for c in _EXPORT_COLS if c in out.columns]]
    st.download_button(f"Download {plural(len(out), 'row')} as CSV · {money(out['display_amount'].sum())}",
                       sanitize_for_csv(export).to_csv(index=False),
                       file_name="transactions.csv", mime="text/csv")


def _spend_table(cube, spec, level_col, level_name, table, raw_vals, hidden_flags,
                 earliest) -> None:
    """Spend table (actual, monthly averages, change on the previous period, txns)
    with tickbox row selection → bulk Hide / Flag buttons."""
    months = _averaging_months(cube, spec)
    n_months = len(months)
    avg_all = _trailing_avg(cube, spec, level_col, months)
    avg3 = _trailing_avg(cube, spec, level_col, months[-3:])
    label = _LEVEL_LABELS[level_name]
    names = [_disp(level_col, v) for v in raw_vals]
    avg_col = f"Avg/mo, {month_span(months)}" if months else "Avg/mo"
    cols = {label: names, "Actual": table["spend"].values}
    money_cols, heat_cols, diverging = ["Actual"], ["Actual"], {}
    if n_months > 1:
        cols[avg_col] = [float(avg_all.get(n, 0.0)) for n in names]
        money_cols.append(avg_col); heat_cols.append(avg_col)
    if n_months > 3:
        cols["Avg/mo (last 3m)"] = [float(avg3.get(n, 0.0)) for n in names]
        money_cols.append("Avg/mo (last 3m)"); heat_cols.append("Avg/mo (last 3m)")
    previous = _previous_spend(cube, spec, level_col, earliest)
    if previous is not None:
        cols["vs previous period"] = [float(a) - float(previous.get(v, 0.0))
                                      for a, v in zip(table["spend"].values, raw_vals)]
        money_cols.append("vs previous period")
        diverging["vs previous period"] = 0.0
    cols["Txns"] = table["count"].astype(int).values
    disp = pd.DataFrame(cols).set_index(label)
    grey = pd.Series(hidden_flags, index=disp.index)
    event = st.dataframe(
        style_table(disp, money_cols=money_cols, int_cols=["Txns"], heat_cols=heat_cols,
                    diverging=diverging, grey_rows=grey),
        width="stretch", on_select="rerun", selection_mode="multi-row",
        key=f"spend_sel_{level_col}", height=table_height(len(disp)),
    )

    sel = []
    try:
        sel = list(event.selection["rows"])
    except (AttributeError, KeyError, TypeError):
        sel = []
    picked = [raw_vals[i] for i in sel if i < len(raw_vals)]

    if picked:
        all_hidden = all(state.is_hidden(level_col, v) for v in picked)
        c1, c2, _ = st.columns([2, 2, 6])
        if c1.button(f"{'Show' if all_hidden else 'Hide'} {len(picked)}",
                     key=f"hidebtn_{level_col}", width="stretch"):
            for v in picked:
                state.unhide(level_col, v) if all_hidden else state.hide(level_col, v, _disp(level_col, v))
            st.rerun()
        if c2.button(f"Flag {len(picked)}", key=f"flagbtn_{level_col}",
                     width="stretch"):
            st.session_state[f"flagged_{level_col}"] = set(picked)
            st.rerun()
    _focus_on_a_single_pick(cube, spec, level_col, picked)

    for v in list(st.session_state.get(f"flagged_{level_col}", set())):
        _flag_category(cube, spec, level_col, v)


def _focus_on_a_single_pick(cube, spec, level_col, picked: list) -> None:
    """The selection outlives the rerun, so only a newly picked row moves the focus: clearing
    the focus chip must not put it straight back."""
    last_key = f"_spend_pick_{level_col}"
    pick = picked[0] if len(picked) == 1 else None
    if pick == st.session_state.get(last_key):
        return
    st.session_state[last_key] = pick
    if pick is None:
        return
    path = _path_to(cube, spec, level_col, pick)
    if path:
        st.session_state[ROOT_KEY] = path
    else:
        # Filed under more than one parent, so no single node holds it.
        st.session_state[page_nav.NAV_KEY] = {
            "focus": [], "filters": {"search": _disp(level_col, pick)},
            "period": st.session_state.get(PERIOD_KEY)}
    st.rerun()


def _path_to(cube, spec, level_col, value) -> list | None:
    """The hierarchy path down to one table row, or None when the value sits under more
    than one parent (a merchant filed under two categories) and no single node holds it."""
    rows = cube.filtered(GroupingSpec(filters={**spec.filters, "flow": "spend", level_col: value},
                                      date_from=spec.date_from, date_to=spec.date_to,
                                      include_hidden=True))
    levels = CATEGORY_LEVELS[:CATEGORY_LEVELS.index(level_col) + 1]
    paths = rows[levels].drop_duplicates()
    return paths.iloc[0].tolist() if len(paths) == 1 else None


def _previous_spend(cube, spec, level_col, earliest) -> pd.Series | None:
    """Spend per value in the period before the selected one; None when the
    archive does not cover that period."""
    prev_from, prev_to = previous_period(date.fromisoformat(spec.date_from),
                                         date.fromisoformat(spec.date_to))
    if prev_from < month_start(earliest):
        return None
    prev = cube.rollup(GroupingSpec(group_by=[level_col], filters=spec.filters,
                                    date_from=str(prev_from), date_to=str(prev_to),
                                    measures=["spend"], order_by=None, include_hidden=True))
    return prev.set_index(level_col)["spend"]


def _flag_category(cube, spec, level_col, value) -> None:
    rep = cube.filtered(GroupingSpec(
        filters={**spec.filters, "flow": "spend", level_col: value},
        date_from=spec.date_from, date_to=spec.date_to, include_hidden=True))
    original = {"tier1": "", "tier2": "", "pfc_detailed": "", "merchant_name": ""}
    if not rep.empty:
        r = rep.iloc[0]
        original = {"tier1": r.get("tier1"), "tier2": r.get("tier2"),
                    "pfc_detailed": r.get("pfc_detailed"), "merchant_name": r.get("merchant")}
    correction_form(scope=level_col, original=original,
                    target={"label": _disp(level_col, value), level_col: str(value),
                            "merchant": original.get("merchant_name")},
                    key=f"flagcat_{level_col}_{value}")


def _nothing_visible_under(rows: pd.DataFrame, root: list) -> bool:
    focused = _drill_filter(rows, root)
    return focused.empty or bool(focused["hidden"].all())


def _hierarchy(rows: pd.DataFrame) -> list:
    root: list = st.session_state.setdefault(ROOT_KEY, [])
    if root and _nothing_visible_under(rows, root):
        root = st.session_state[ROOT_KEY] = []

    kind = st.radio("Chart", ["Wheel", "Treemap"], horizontal=True,
                    key="hier_kind", label_visibility="collapsed")
    _breadcrumb(root)

    sub = _drill_filter(rows, root)
    hidden = sub["hidden"] if "hidden" in sub.columns else pd.Series(False, index=sub.index)
    vis = sub[~hidden]
    # the chart always renders to maximum granularity (merchant) below the root
    levels = CATEGORY_LEVELS[len(root):]

    if vis.empty or not levels:
        # at/under the deepest level there is nothing left to nest — show the
        # transactions instead of building a degenerate single-node chart (which
        # otherwise crashes the component and snaps back to the top).
        st.caption("Deepest level reached — the matching transactions are listed below."
                   if not levels else "Nothing to chart at this node.")
    else:
        fig, abs_nodes = _build_hierarchy_fig(vis, levels, root, treemap=(kind == "Treemap"))
        # STABLE key (no len(root)) so a drill updates the figure *in place* rather
        # than remounting the iframe — that remount was the blank "flash". The
        # warmed flag only changes once, for the initial-paint sizing fix.
        warm = int(bool(st.session_state.get("_hier_warmed")))
        key = f"pe_{kind}_{warm}"
        clicks = plotly_events(fig, click_event=True, override_height=560,
                               override_width="100%", key=key)
        st.caption("Click a slice to zoom in · breadcrumb or Zoom out to go back.")
        # The component keeps returning the last click across reruns; only act on a
        # value we haven't processed yet (otherwise the post-drill rerun re-fires it).
        proc_key = f"pe_proc_{kind}"
        if clicks and clicks != st.session_state.get(proc_key):
            st.session_state[proc_key] = clicks
            target = _clicked_path(clicks, abs_nodes)
            if target and target != root and len(target) <= len(CATEGORY_LEVELS):
                st.session_state[ROOT_KEY] = target
                st.rerun()

    if hidden.any():
        names = ", ".join(sorted(sub.loc[hidden, "tier1"].unique()))
        st.caption(f"Hidden (not charted): {names} ({money(sub.loc[hidden,'spend'].sum())})")

    # One-time warm-up: the component mis-sizes on the very first page paint
    # (renders before layout settles, hiding text). A single rerun fixes it.
    if not st.session_state.get("_hier_warmed"):
        st.session_state["_hier_warmed"] = True
        st.rerun()
    return root


def _clicked_path(clicks, abs_nodes: list) -> list | None:
    """Map a plotly_events click on a sunburst/treemap slice to its full path.

    The component returns the clicked sector's index; sectors are emitted in the
    same order we built ids/values, so the index selects into abs_nodes.
    """
    if not clicks:
        return None
    pt = clicks[0]
    for k in ("pointNumber", "pointIndex", "point_number", "point_index"):
        i = pt.get(k) if isinstance(pt, dict) else None
        if isinstance(i, int) and 0 <= i < len(abs_nodes):
            return abs_nodes[i]
    return None


def _breadcrumb(root: list) -> None:
    crumbs = ["All"] + [_disp(CATEGORY_LEVELS[i], v) for i, v in enumerate(root)]
    with st.container(horizontal=True, gap="small", vertical_alignment="center"):
        for i, name in enumerate(crumbs):
            here = i == len(crumbs) - 1
            if st.button(name, key=f"crumb_{i}", type="tertiary" if here else "secondary",
                         disabled=here):
                st.session_state[ROOT_KEY] = root[:i]
                st.rerun()
        if root and st.button("Zoom out", key="zoom_out", icon=":material/arrow_upward:"):
            st.session_state[ROOT_KEY] = root[:-1]
            st.rerun()


def _drill_filter(rows: pd.DataFrame, root: list) -> pd.DataFrame:
    out = rows
    for i, v in enumerate(root):
        out = out[out[CATEGORY_LEVELS[i]] == v]
    return out


def _build_hierarchy_fig(vis, levels, root, treemap: bool):
    """Build a sunburst/treemap for the subtree under `root`.

    Returns (fig, abs_nodes) where abs_nodes[i] is the absolute category path of
    the i-th sector — used to resolve a click back to a drill target.
    """
    totals: dict[tuple, float] = {}
    parent: dict[tuple, tuple] = {}
    # A sunburst cannot draw a negative wedge and needs each parent to equal its
    # children, so refunds net at the leaf (merchant) and a leaf that nets to zero
    # or below drops out before the parents are summed.
    leaves = vis.groupby([c for c in levels], dropna=False)["spend"].sum().reset_index()
    for _, r in leaves[leaves["spend"] > 0].iterrows():
        rel = []
        for c in levels:
            v = r[c]
            if v is None or (isinstance(v, float) and pd.isna(v)) or v == "":
                break
            rel.append(v)  # RAW value: drill state must match the raw column (see _drill_filter)
        for i in range(len(rel)):
            key = tuple(rel[: i + 1])
            totals[key] = totals.get(key, 0.0) + float(r["spend"])
            parent[key] = tuple(rel[:i])

    ids, labels, parents, values, colors, abs_nodes = [], [], [], [], [], []
    # Colour follows the top-level node, largest first, so the big wedges get the
    # distinct hues and the long tail shares the neutral.
    # The necessity ring has two values, which would paint the whole wheel in two
    # colours; hue follows the category ring beneath it and necessity stays neutral.
    hue_depth = 2 if levels[0] == "tier0" and len(levels) > 1 else 1
    top = sorted((k for k in totals if len(k) == hue_depth), key=totals.get, reverse=True)
    cmap = theme.series_colors(top)
    for key, val in totals.items():
        ids.append("/".join(map(str, key)))
        # ids/parents/abs_nodes carry RAW values (so a click resolves to a filterable
        # path); only the visible label is humanized, at its own level.
        labels.append(_disp(levels[len(key) - 1], key[-1]))
        parents.append("/".join(map(str, parent[key])) if parent[key] else "")
        values.append(val)
        colors.append(cmap[key[:hue_depth]] if len(key) >= hue_depth else theme.INK_700)
        abs_nodes.append(list(root) + list(key))

    trace_cls = go.Treemap if treemap else go.Sunburst
    kwargs = dict(
        ids=ids, labels=labels, parents=parents, values=values, branchvalues="total",
        marker=dict(colors=colors, line=dict(color=theme.PAPER_0, width=2)),
        # No uniformtext: let each sector auto-scale its label to fill the box,
        # capped at 22px, so the (large) leaf sectors get big, readable text.
        insidetextfont=dict(size=22, color=theme.PAPER_0),
        textfont=dict(size=15, color=theme.PAPER_0),
        maxdepth=3,
        hovertemplate="<b>%{label}</b><br>%{value:$,.0f}<extra></extra>",
    )
    if not treemap:
        kwargs["insidetextorientation"] = "radial"
    fig = go.Figure(trace_cls(**kwargs))
    fig.update_layout(
        margin=dict(t=6, l=6, r=6, b=6), height=560,
    )
    return fig, abs_nodes


def _trailing_avg(cube, spec, level_col, months: list[str]) -> pd.Series:
    """Mean monthly spend per value over `months`; a month without spend counts as 0."""
    monthly = cube.rollup(GroupingSpec(group_by=[level_col, "month"], filters=spec.filters,
                                       date_from=spec.date_from, date_to=spec.date_to,
                                       measures=["spend"], order_by=None, include_hidden=True))
    if monthly.empty:
        return pd.Series(dtype=float)
    pivot = monthly.pivot_table(index=level_col, columns="month", values="spend",
                                aggfunc="sum").fillna(0.0)
    out = pivot.reindex(columns=months, fill_value=0.0).mean(axis=1)
    out.index = [_disp(level_col, i) for i in out.index]
    return out


def _monthly_visible_spend(cube, spec) -> pd.Series:
    m = cube.rollup(GroupingSpec(group_by=["month"], filters=spec.filters,
                                 date_from=spec.date_from, date_to=spec.date_to,
                                 measures=["spend"], order_by=None))
    return m.set_index("month")["spend"] if not m.empty else pd.Series(dtype=float)


def _averaging_months(cube, spec) -> list[str]:
    present = cube.rollup(GroupingSpec(group_by=["month"], filters=spec.filters,
                                       date_from=spec.date_from, date_to=spec.date_to,
                                       measures=["spend"], order_by=None, include_hidden=True))
    return averaging_months(spec.date_from, spec.date_to,
                            present["month"].tolist() if not present.empty else [])


def _tier1_spend(cube, spec, tier1) -> float:
    # Visible rows only (no include_hidden): these feed the "Less Mortgage/Home"
    # metrics, which subtract from the visible Total — a hidden Mortgage is already
    # out of that total, so subtracting its hidden spend would double-remove it.
    f = {**spec.filters, "tier1": tier1, "flow": "spend"}
    return cube.total(GroupingSpec(filters=f, date_from=spec.date_from,
                                   date_to=spec.date_to))["spend"]
