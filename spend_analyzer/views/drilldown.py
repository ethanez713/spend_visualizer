"""Drilldown core: granularity (table only), spend table, an interactive
hierarchy chart (wheel/treemap/sankey) and a transaction-detail table that
*follows the chart*.

Interaction model:
  • The top **Granularity** radio controls ONLY the "Spend by X" table.
  • The hierarchy chart has its own **Chart detail** control and starts at the
    deepest level by default. Click a slice to zoom in (captured via the
    streamlit-plotly-events component); the breadcrumb and ⟲ Zoom-out navigate
    back. The chart focus and the transaction-detail table share one
    `wheel_root` so they stay in lock-step.
  • Hidden categories are dropped from the charts entirely (shown only as a note
    underneath) so they never blow a big empty wedge into the diagram.
"""
from __future__ import annotations

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
from streamlit_plotly_events import plotly_events

import state
from cube import Cube, GroupingSpec, CATEGORY_LEVELS, CATEGORY_LEVEL_BY_NAME
from viz import humanize_atom, money, style_table
from views._widgets import correction_form, hide_button, transaction_detail

_LEVEL_LABELS = {
    "necessity": "Necessity", "tier1": "Category", "tier2": "Subcategory",
    "atom": "Atom", "merchant": "Merchant",
}
_PALETTE = px.colors.qualitative.Set3 + px.colors.qualitative.Pastel
ROOT_KEY = "wheel_root"
_SANKEY_TOPN = 10


def _disp(col: str, v) -> str:
    return humanize_atom(v) if col == "atom" else str(v)


# ----------------------------------------------------------------- granularity
def granularity_control(cube: Cube, spec: GroupingSpec) -> tuple[str, str]:
    options = list(CATEGORY_LEVEL_BY_NAME.keys())
    level_name = st.radio(
        "Table granularity", options,
        index=options.index(st.session_state.get("_level_name", "tier1")),
        horizontal=True, format_func=lambda n: _LEVEL_LABELS[n],
    )
    st.session_state["_level_name"] = level_name
    return level_name, CATEGORY_LEVEL_BY_NAME[level_name]


# ----------------------------------------------------------------------- render
def render(cube: Cube, spec: GroupingSpec, trailing_months: int) -> None:
    level_name, level_col = granularity_control(cube, spec)

    table = cube.rollup(GroupingSpec(group_by=[level_col], filters=spec.filters,
                                     date_from=spec.date_from, date_to=spec.date_to,
                                     measures=["spend", "count"], order_by="spend",
                                     include_hidden=True))
    if table.empty:
        st.info("No spend matches the current filters.")
        return
    raw_vals = table[level_col].tolist()
    hidden_flags = [state.is_hidden(level_col, v) for v in raw_vals]

    st.markdown(f"**Spend by {_LEVEL_LABELS[level_name]}**")
    st.caption("Tick rows to select, then Hide or Flag them in bulk. "
               "(Manage hidden categories from the sidebar.)")
    _spend_table(cube, spec, level_col, level_name, table, raw_vals, hidden_flags)

    visible_total = float(table.loc[[not h for h in hidden_flags], "spend"].sum())
    n_months = max(1, _num_months(cube, spec))
    mortgage = _tier1_spend(cube, spec, "Mortgage")
    house = _tier1_spend(cube, spec, "House")
    d = st.columns(4)
    d[0].metric("Total", money(visible_total))
    d[1].metric("Annualized", money(visible_total / n_months * 12))
    d[2].metric("Less Mortgage", money(visible_total - mortgage))
    d[3].metric("Less Mortgage & Home", money(visible_total - mortgage - house))

    rows = cube.filtered(GroupingSpec(filters={**spec.filters, "flow": "spend"},
                                      date_from=spec.date_from, date_to=spec.date_to,
                                      include_hidden=True))
    rows = rows[rows["spend"] > 0]
    root = _hierarchy(rows, cube, spec)
    _detail_section(rows, root)


def _spend_table(cube, spec, level_col, level_name, table, raw_vals, hidden_flags) -> None:
    """Styled spend table (Actual + 12m/3m running avgs + txns, green heatmap)
    with tickbox row selection → bulk 👁 Hide / 🚩 Flag buttons."""
    avg12 = _trailing_avg(cube, spec, level_col, 12)
    avg3 = _trailing_avg(cube, spec, level_col, 3)
    label = _LEVEL_LABELS[level_name]
    names = [_disp(level_col, v) for v in raw_vals]
    disp = pd.DataFrame({
        label: names,
        "Actual": table["spend"].values,
        "Avg/mo (12m)": [float(avg12.get(n, 0.0)) for n in names],
        "Avg/mo (3m)": [float(avg3.get(n, 0.0)) for n in names],
        "Txns": table["count"].astype(int).values,
    }).set_index(label)
    grey = pd.Series(hidden_flags, index=disp.index)
    event = st.dataframe(
        style_table(disp, money_cols=["Actual", "Avg/mo (12m)", "Avg/mo (3m)"],
                    int_cols=["Txns"], green_cols=["Actual", "Avg/mo (12m)", "Avg/mo (3m)"],
                    grey_rows=grey),
        use_container_width=True, on_select="rerun", selection_mode="multi-row",
        key=f"spend_sel_{level_col}",
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
        if c1.button(f"{'🙈 Show' if all_hidden else '👁 Hide'} {len(picked)}",
                     key=f"hidebtn_{level_col}", use_container_width=True):
            for v in picked:
                state.unhide(level_col, v) if all_hidden else state.hide(level_col, v, _disp(level_col, v))
            st.rerun()
        if c2.button(f"🚩 Flag {len(picked)}", key=f"flagbtn_{level_col}",
                     use_container_width=True):
            st.session_state[f"flagged_{level_col}"] = set(picked)
            st.rerun()

    for v in list(st.session_state.get(f"flagged_{level_col}", set())):
        _flag_category(cube, spec, level_col, v)


def _max_date(cube, spec):
    from datetime import date
    if spec.date_to:
        return date.fromisoformat(spec.date_to[:10])
    dates = cube.distinct("date_resolved")
    return date.fromisoformat(dates[-1][:10]) if dates else None


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


_WINDOW_DAYS = {"12 mo": 365, "3 mo": 90, "1 mo": 30}


# --------------------------------------------------------------------- hierarchy
def _hierarchy(rows: pd.DataFrame, cube=None, spec=None) -> list:
    st.subheader("Hierarchy")
    root: list = st.session_state.setdefault(ROOT_KEY, [])
    if root and _drill_filter(rows, root).empty:
        root = st.session_state[ROOT_KEY] = []

    c1, c2, c3 = st.columns([3, 3, 1])
    kind = c1.radio("Chart", ["Wheel", "Treemap", "Sankey"], horizontal=True,
                    key="hier_kind", label_visibility="collapsed")
    win = c2.radio("Window", list(_WINDOW_DAYS), horizontal=True, key="hier_window",
                   label_visibility="collapsed")
    if c3.button("⟲ Zoom out", disabled=not root):
        st.session_state[ROOT_KEY] = root[:-1]
        st.rerun()

    _breadcrumb(root)

    sub = _drill_filter(rows, root)
    # the Window selector restricts the chart (only) to the trailing 12 / 3 / 1 months
    anchor = _max_date(cube, spec) if cube is not None else None
    if anchor is not None:
        from datetime import timedelta
        start = (anchor - timedelta(days=_WINDOW_DAYS[win])).isoformat()
        sub = sub[sub["date_resolved"] >= start]
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
    elif kind == "Sankey":
        st.plotly_chart(_build_sankey(vis, levels), use_container_width=True, key="sankey")
    else:
        fig, abs_nodes = _build_hierarchy_fig(vis, levels, root, treemap=(kind == "Treemap"))
        # STABLE key (no len(root)) so a drill updates the figure *in place* rather
        # than remounting the iframe — that remount was the blank "flash". The
        # warmed flag only changes once, for the initial-paint sizing fix.
        warm = int(bool(st.session_state.get("_hier_warmed")))
        key = f"pe_{kind}_{warm}"
        clicks = plotly_events(fig, click_event=True, override_height=560,
                               override_width="100%", key=key)
        st.caption("Click a slice to zoom in · breadcrumb or ⟲ Zoom out to go back.")
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
        st.caption(f"🙈 Hidden (not charted): {names} ({money(sub.loc[hidden,'spend'].sum())})")

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
    crumbs = ["🏠 All"] + [_disp(CATEGORY_LEVELS[i], v) for i, v in enumerate(root)]
    cols = st.columns(len(crumbs) + 4)
    for i, name in enumerate(crumbs):
        if cols[i].button(name, key=f"crumb_{i}"):
            st.session_state[ROOT_KEY] = root[:i]
            st.rerun()


def _drill_filter(rows: pd.DataFrame, root: list) -> pd.DataFrame:
    out = rows
    for i, v in enumerate(root):
        out = out[out[CATEGORY_LEVELS[i]] == v]
    return out


# --------------------------------------------------------------------- charts
def _build_hierarchy_fig(vis, levels, root, treemap: bool):
    """Build a sunburst/treemap for the subtree under `root`.

    Returns (fig, abs_nodes) where abs_nodes[i] is the absolute category path of
    the i-th sector — used to resolve a click back to a drill target.
    """
    totals: dict[tuple, float] = {}
    parent: dict[tuple, tuple] = {}
    root_of: dict[tuple, str] = {}
    for _, r in vis.iterrows():
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
            root_of[key] = rel[0]

    ids, labels, parents, values, colors, abs_nodes = [], [], [], [], [], []
    cmap: dict[str, str] = {}
    for key, val in totals.items():
        ids.append("/".join(map(str, key)))
        # ids/parents/abs_nodes carry RAW values (so a click resolves to a filterable
        # path); only the visible label is humanized, at its own level.
        labels.append(_disp(levels[len(key) - 1], key[-1]))
        parents.append("/".join(map(str, parent[key])) if parent[key] else "")
        values.append(val)
        colors.append(cmap.setdefault(root_of[key], _PALETTE[len(cmap) % len(_PALETTE)]))
        abs_nodes.append(list(root) + list(key))

    trace_cls = go.Treemap if treemap else go.Sunburst
    kwargs = dict(
        ids=ids, labels=labels, parents=parents, values=values, branchvalues="total",
        marker=dict(colors=colors, line=dict(color="#0e1117", width=1)),
        # No uniformtext: let each sector auto-scale its label to fill the box,
        # capped at 22px, so the (large) leaf sectors get big, readable text.
        insidetextfont=dict(size=22, color="#10161f"),
        textfont=dict(size=15, color="#10161f"),
        maxdepth=3,
        hovertemplate="<b>%{label}</b><br>%{value:$,.0f}<extra></extra>",
    )
    if not treemap:
        kwargs["insidetextorientation"] = "radial"
    fig = go.Figure(trace_cls(**kwargs))
    fig.update_layout(
        margin=dict(t=6, l=6, r=6, b=6), height=560,
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font=dict(color="#e6e6e6"),
    )
    return fig, abs_nodes


def _build_sankey(vis, levels) -> go.Figure:
    """Readable Sankey: at most 3 stages, top-N nodes per stage, the long tail
    folded into an 'Other' node, links coloured by source."""
    stages = levels[:3]
    if len(stages) < 2:
        fig = go.Figure()
        fig.add_annotation(text="Sankey needs ≥2 levels — increase Chart detail "
                                "or zoom out.", showarrow=False)
        fig.update_layout(height=300, margin=dict(t=10))
        return fig

    # keep the top-N values per stage; everything else -> "Other"
    keep: dict[str, set] = {}
    for c in stages:
        tot = vis.groupby(c)["spend"].sum().sort_values(ascending=False)
        keep[c] = set(tot.head(_SANKEY_TOPN).index)

    def node_name(stage: str, raw) -> str:
        disp = _disp(stage, raw)
        return disp if raw in keep[stage] else f"Other {_LEVEL_LABELS[stage]}"

    labels: list[str] = []
    idx: dict[tuple, int] = {}
    node_color: list[str] = []
    cmap: dict[str, str] = {}

    def nid(level: int, name: str) -> int:
        key = (level, name)
        if key not in idx:
            idx[key] = len(labels)
            labels.append(name)
            node_color.append(cmap.setdefault(name, _PALETTE[len(cmap) % len(_PALETTE)]))
        return idx[key]

    links: dict[tuple[int, int], float] = {}
    for _, r in vis.iterrows():
        names = []
        for c in stages:
            v = r[c]
            if v is None or (isinstance(v, float) and pd.isna(v)) or v == "":
                break
            names.append((c, v))
        for i in range(len(names) - 1):
            s = nid(i, node_name(*names[i]))
            d = nid(i + 1, node_name(*names[i + 1]))
            links[(s, d)] = links.get((s, d), 0.0) + float(r["spend"])

    src = [s for s, _ in links]
    link_colors = [_rgba(node_color[s], 0.35) for s in src]
    fig = go.Figure(go.Sankey(
        arrangement="snap",
        node=dict(label=labels, color=node_color, pad=22, thickness=20,
                  line=dict(color="#0e1117", width=1)),
        link=dict(source=src, target=[d for _, d in links], value=list(links.values()),
                  color=link_colors, hovertemplate="%{value:$,.0f}<extra></extra>"),
    ))
    fig.update_layout(height=560, margin=dict(t=20, l=10, r=10, b=10),
                      paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
                      font=dict(size=13, color="#e6e6e6"))
    return fig


# ------------------------------------------------------------ transaction detail
def _detail_section(rows: pd.DataFrame, root: list) -> None:
    st.subheader("Transaction detail")
    sub = _drill_filter(rows, root)
    if not root:
        st.caption("Click into the hierarchy above to focus these transactions on a node.")
    crumb = " › ".join(_disp(CATEGORY_LEVELS[i], v) for i, v in enumerate(root)) or "All spend"
    if root and not sub.empty:
        dim, val = CATEGORY_LEVELS[len(root) - 1], root[-1]
        hide_button(dim, val, _disp(dim, val), key=f"hide_drill_{dim}_{val}")
    transaction_detail(sub, title=crumb, key=f"drill_{len(root)}_{'_'.join(map(str, root))}")


# ------------------------------------------------------------------- helpers


def _rgba(hex_or_rgb: str, alpha: float) -> str:
    s = hex_or_rgb.strip()
    if s.startswith("#"):
        r, g, b = int(s[1:3], 16), int(s[3:5], 16), int(s[5:7], 16)
        return f"rgba({r},{g},{b},{alpha})"
    if s.startswith("rgb("):
        return s.replace("rgb(", "rgba(").replace(")", f",{alpha})")
    return s


def _trailing_avg(cube, spec, level_col, window) -> pd.Series:
    monthly = cube.rollup(GroupingSpec(group_by=[level_col, "month"], filters=spec.filters,
                                       date_from=spec.date_from, date_to=spec.date_to,
                                       measures=["spend"], order_by=None, include_hidden=True))
    if monthly.empty:
        return pd.Series(dtype=float)
    pivot = monthly.pivot_table(index=level_col, columns="month", values="spend",
                                aggfunc="sum").fillna(0.0)
    last = sorted(pivot.columns)[-window:]
    out = pivot[last].mean(axis=1)
    out.index = [humanize_atom(i) if level_col == "atom" else i for i in out.index]
    return out


def _num_months(cube, spec) -> int:
    m = cube.rollup(GroupingSpec(group_by=["month"], filters=spec.filters,
                                 date_from=spec.date_from, date_to=spec.date_to,
                                 measures=["spend"], order_by=None, include_hidden=True))
    return int(m["month"].nunique()) if not m.empty else 1


def _tier1_spend(cube, spec, tier1) -> float:
    # Visible rows only (no include_hidden): these feed the "Less Mortgage/Home"
    # metrics, which subtract from the visible Total — a hidden Mortgage is already
    # out of that total, so subtracting its hidden spend would double-remove it.
    f = {**spec.filters, "tier1": tier1, "flow": "spend"}
    return cube.total(GroupingSpec(filters=f, date_from=spec.date_from,
                                   date_to=spec.date_to))["spend"]
