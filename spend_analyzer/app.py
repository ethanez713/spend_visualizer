"""Spend Analyzer — Streamlit shell: filters + view tabs.

Offline-only: reads the local archive read-only, never calls Plaid or the
network. Run with:  streamlit run app.py
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import streamlit as st

import manual_edits
import state
import theme
from config_io import CONFIG_DIR, PERSONAL_CONFIG_DIR, load_app_config, load_budget
from cube import Cube, GroupingSpec
from data import build_cube
from ingest.load import stat_source
from views import budget as budget_view
from periods import CUSTOM, DEFAULT_PRESET, PRESETS, preset_range
from views import cashflow, drilldown, merchants, overview, review, transactions
from views import corrections_view, rules_view
from views import qc as qc_view

st.set_page_config(page_title="Spend analyzer", layout="wide")

def _config_signature(app) -> tuple:
    """Cache key: archive, intent log and config file stats (mtime/size)."""
    sig = [manual_edits.edits_sig()]
    for p in app.resolved_archive_paths:
        try:
            sig.append(stat_source(p).cache_key)
        except FileNotFoundError:
            sig.append((p, 0.0, 0))
    # Each file must be stat'd in the dir it actually lives in: the personal
    # pair sits under the data root, so statting them under CONFIG_DIR would
    # silently never invalidate the cube when the user edits them.
    for directory, name in ((CONFIG_DIR, "taxonomy.yaml"),
                            (CONFIG_DIR, "budget_model.yaml"),
                            (CONFIG_DIR, "app.yaml"),
                            (PERSONAL_CONFIG_DIR, "accounts.yaml"),
                            (PERSONAL_CONFIG_DIR, "budget.yaml")):
        fp = directory / name
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
    theme.apply()
    st.title("Spend analyzer")
    app = load_app_config()
    budget = load_budget()

    missing = [p for p in app.resolved_archive_paths if not Path(p).exists()]
    if missing:
        st.error("Archive not found:\n" + "\n".join(f"- `{p}`" for p in missing))
        st.caption("Set the path in `config/app.yaml`. The collector owns the archive.")
        st.stop()

    df, qc = _load(_config_signature(app))
    if df.empty:
        st.warning("No transactions after ingest. Check the archive and accounts.yaml.")
        st.stop()

    # apply view-layer hiding: flag rows so every rollup can exclude them
    df = df.copy()
    df["hidden"] = state.hidden_mask(df)
    cube = Cube(df)

    spec, earliest, anchor = _sidebar_filters(cube, df)

    views = {
        "Overview": lambda: overview.render(cube, spec, earliest, anchor),
        "Drilldown": lambda: drilldown.render(cube, spec, earliest),
        "Transactions": lambda: transactions.render(cube, spec),
        "Budget": lambda: budget_view.render(cube, spec, budget, app.trailing_avg_months),
        "Merchants and recurring": lambda: merchants.render(cube, spec, anchor),
        "Cash flow": lambda: cashflow.render(cube, spec),
        "Review": lambda: review.render(df),
        "Overrides": corrections_view.render,
        "Rules": lambda: rules_view.render(df),
        "Checks": lambda: qc_view.render(cube, qc, spec),
    }
    views[_current_view(list(views), {"Review": len(review.pending(df))})]()


VIEW_KEY = "view"


def _current_view(names: list[str], counts: dict[str, int]) -> str:
    """The view switcher. Only the chosen view runs: a chart mounted inside a
    hidden st.tabs panel measures a zero-width container and drops its labels.
    `counts` adds a pending-work count to a view's label.
    The choice is mirrored into the URL so a reload or bookmark returns to it."""
    if VIEW_KEY not in st.session_state:
        asked = st.query_params.get(VIEW_KEY)
        st.session_state[VIEW_KEY] = asked if asked in names else names[0]
    view = st.radio("View", names, horizontal=True, key=VIEW_KEY,
                    label_visibility="collapsed",
                    format_func=lambda n: f"{n} · {counts[n]}" if counts.get(n) else n)
    st.query_params[VIEW_KEY] = view
    return view


_FLOW_LABELS = {"spend": "Spending", "income": "Income", "All": "All"}


def _sidebar_filters(cube: Cube, df) -> tuple[GroupingSpec, date, date]:
    """The filter spec plus the archive's first and last transaction dates."""
    sb = st.sidebar
    theme.eyebrow_in(sb, "Filters")
    dates = df["date_resolved"].dropna()
    earliest, anchor = _d(dates.min()), _d(dates.max())

    preset = sb.selectbox("Period", PRESETS, index=PRESETS.index(DEFAULT_PRESET),
                          key="period_preset")
    date_from, date_to = preset_range(preset, anchor, earliest)
    if preset == CUSTOM:
        picked = sb.date_input("Date range", value=(earliest, anchor),
                               min_value=earliest, max_value=anchor)
        # st.date_input returns a 1-tuple *mid-selection* (after the user clicks the
        # start date, before the end date) — unpacking it into two names crashes the
        # whole app until the second click. Keep the full range until both ends exist.
        if isinstance(picked, (list, tuple)) and len(picked) == 2:
            date_from, date_to = picked
    date_from = max(date_from, earliest)
    sb.caption(f"{date_from:%-d %b %Y} – {date_to:%-d %b %Y}")

    filters: dict = {}
    flow = sb.radio("Flow", list(_FLOW_LABELS), horizontal=True,
                    format_func=_FLOW_LABELS.get)
    if flow != "All":
        filters["flow"] = flow
    for label, col in [("Person", "person"), ("Account type", "account_type"),
                       ("Channel", "channel"), ("Necessity", "necessity")]:
        sel = sb.multiselect(label, cube.distinct(col), default=[], placeholder="All")
        if sel:
            filters[col] = sel

    _hidden_manager(sb)
    if sb.button("Reload data", help="Re-read the archive and taxonomy from disk."):
        _load.clear()
        st.rerun()

    return (GroupingSpec(filters=filters, date_from=str(date_from), date_to=str(date_to)),
            earliest, anchor)


def _hidden_manager(sb) -> None:
    rules = state.hidden_rules()
    theme.eyebrow_in(sb, "Hidden")
    if not rules:
        sb.caption("Nothing hidden. Use Hide in Drilldown or Merchants.")
        return
    for r in list(rules):
        c1, c2 = sb.columns([4, 1])
        c1.caption(r["label"])
        if c2.button("×", key=f"unhide_{r['dim']}_{r['value']}"):
            state.unhide(r["dim"], r["value"])
            st.rerun()
    if sb.button("Show all"):
        state.clear_hidden()
        st.rerun()


def _d(iso: str) -> date:
    return date.fromisoformat(iso[:10])


if __name__ == "__main__":
    main()
