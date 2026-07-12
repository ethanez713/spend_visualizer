"""Spend Analyzer — Streamlit shell: filters + view tabs.

Offline-only: reads the local archive read-only, never calls Plaid or the
network. Run with:  streamlit run app.py
"""
from __future__ import annotations

from pathlib import Path

import streamlit as st

import state
from config_io import CONFIG_DIR, load_app_config, load_budget
from cube import Cube, GroupingSpec
from data import build_cube
from ingest.load import stat_source
from views import budget as budget_view
from views import cashflow, drilldown, merchants
from views import corrections_view
from views import qc as qc_view

st.set_page_config(page_title="Spend Analyzer", page_icon="📊", layout="wide")

# Layout polish: tables drag-resizable + columns that stack on narrow windows.
_CSS = """
<style>
/* Drag the bottom-right corner of any table to widen / narrow it on demand. */
div[data-testid="stDataFrame"] {
    resize: horizontal;
    overflow: auto;
    min-width: 280px;
}
/* Responsive stacking: side-by-side columns wrap to full width when narrow. */
@media (max-width: 820px) {
    div[data-testid="stHorizontalBlock"] { flex-wrap: wrap; }
    div[data-testid="stHorizontalBlock"] > div[data-testid="stColumn"] {
        min-width: 100% !important;
        flex: 1 1 100% !important;
    }
}
</style>
"""


def _inject_css() -> None:
    st.markdown(_CSS, unsafe_allow_html=True)


def _config_signature(app) -> tuple:
    """Cache key: archive + config file stats (mtime/size)."""
    sig = []
    for p in app.resolved_archive_paths:
        try:
            sig.append(stat_source(p).cache_key)
        except FileNotFoundError:
            sig.append((p, 0.0, 0))
    for name in ("taxonomy.yaml", "accounts.yaml", "app.yaml", "budget.yaml"):
        fp = CONFIG_DIR / name
        if fp.exists():
            s = fp.stat()
            sig.append((name, s.st_mtime, s.st_size))
    return tuple(sig)


@st.cache_data(show_spinner="Loading & enriching transactions…")
def _load(signature: tuple):
    # `signature` must NOT be underscore-prefixed: st.cache_data skips hashing
    # `_`-named params, which would pin the cache to the first load forever.
    res = build_cube()
    return res.df, res.qc


def main() -> None:
    st.title("📊 Spend Analyzer")
    _inject_css()
    app = load_app_config()
    budget = load_budget()

    missing = [p for p in app.resolved_archive_paths if not Path(p).exists()]
    if missing:
        st.error("Archive not found:\n" + "\n".join(f"- `{p}`" for p in missing))
        st.caption("Set the path in `config/app.yaml`. The collector owns the archive.")
        st.stop()

    if st.sidebar.button("🔄 Reload taxonomy / data"):
        _load.clear()

    df, qc = _load(_config_signature(app))
    if df.empty:
        st.warning("No transactions after ingest. Check the archive and accounts.yaml.")
        st.stop()

    # apply view-layer hiding: flag rows so every rollup can exclude them
    df = df.copy()
    df["hidden"] = state.hidden_mask(df)
    cube = Cube(df)

    spec = _sidebar_filters(cube, df)

    # Drilldown first so it is the default tab on load.
    t_drill, t_budget, t_merch, t_cash, t_corr, t_qc = st.tabs(
        ["🧭 Drilldown", "💰 Budget", "🏪 Merchants & recurring",
         "💵 Cash flow", "✏️ Corrections", "✅ QC"]
    )
    with t_drill:
        drilldown.render(cube, spec, app.trailing_avg_months)
    with t_budget:
        budget_view.render(cube, spec, budget, app.trailing_avg_months)
    with t_merch:
        merchants.render(cube, spec)
    with t_cash:
        cashflow.render(cube, spec)
    with t_corr:
        corrections_view.render()
    with t_qc:
        qc_view.render(cube, qc, spec)


def _sidebar_filters(cube: Cube, df) -> GroupingSpec:
    sb = st.sidebar
    sb.header("Filters")
    dmin, dmax = df["date_resolved"].dropna().min(), df["date_resolved"].dropna().max()
    if dmin and dmax:
        picked = sb.date_input(
            "Date range", value=(_d(dmin), _d(dmax)),
            min_value=_d(dmin), max_value=_d(dmax),
        )
        # st.date_input returns a 1-tuple *mid-selection* (after the user clicks the
        # start date, before the end date) — unpacking it into two names crashes the
        # whole app until the second click. Keep the full range until both ends exist.
        if isinstance(picked, (list, tuple)) and len(picked) == 2:
            date_from, date_to = picked
        else:
            date_from, date_to = _d(dmin), _d(dmax)
    else:
        date_from = date_to = None

    filters: dict = {}
    flow = sb.radio("Flow", ["spend", "income", "All"], horizontal=True)
    if flow != "All":
        filters["flow"] = flow
    for label, col in [("Person", "person"), ("Account type", "account_type"),
                       ("Channel", "channel"), ("Necessity", "necessity")]:
        sel = sb.multiselect(label, cube.distinct(col), default=[])
        if sel:
            filters[col] = sel

    _hidden_manager(sb)

    return GroupingSpec(
        filters=filters,
        date_from=str(date_from) if date_from else None,
        date_to=str(date_to) if date_to else None,
    )


def _hidden_manager(sb) -> None:
    rules = state.hidden_rules()
    sb.header("👁 Hidden")
    if not rules:
        sb.caption("Nothing hidden. Use the 👁 controls in Drilldown / Merchants.")
        return
    for r in list(rules):
        c1, c2 = sb.columns([4, 1])
        c1.write(f"🙈 {r['label']}")
        if c2.button("✕", key=f"unhide_{r['dim']}_{r['value']}"):
            state.unhide(r["dim"], r["value"])
            st.rerun()
    if sb.button("Show all"):
        state.clear_hidden()
        st.rerun()


def _d(iso: str):
    from datetime import date
    return date.fromisoformat(iso[:10]) if iso else None


if __name__ == "__main__":
    main()
