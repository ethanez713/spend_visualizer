"""Drill (wheel_root) state: breadcrumb/zoom navigation and its crash guards.

A real chart click cannot be simulated (streamlit-plotly-events is a custom
component, outside AppTest's reach) — the click→path mapping is unit-tested in
tests/test_drilldown.py. Here we set ``wheel_root`` directly, which is exactly
the state a click produces, and test everything downstream of it.
"""
from cube import CATEGORY_LEVELS
from views.drilldown import ROOT_KEY

from tests.ui._harness import texts, widget


def _top_full_path(cube_df) -> list:
    """Deepest-spend path with all five levels present (tier0 → merchant)."""
    rows = cube_df[(cube_df["flow"] == "spend") & (cube_df["spend"] > 0)]
    for c in CATEGORY_LEVELS:
        rows = rows[rows[c].notna() & (rows[c] != "")]
    r = rows.sort_values("spend", ascending=False).iloc[0]
    return [r[c] for c in CATEGORY_LEVELS]


def test_given_drilled_root_when_rendered_then_breadcrumb_and_detail_follow(at, cube_df):
    path = _top_full_path(cube_df)[:2]
    at.session_state[ROOT_KEY] = path
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    crumbs = [b.label for b in at.button if (b.key or "").startswith("crumb_")]
    assert crumbs[0] == "🏠 All" and str(path[1]) in crumbs[2], crumbs
    assert any(f"{path[0]} › {path[1]}" in t for t in texts(at)), \
        "transaction detail does not follow the drill root"


def test_given_drilled_root_when_zoom_out_clicked_then_root_pops_one_level(at, cube_df):
    path = _top_full_path(cube_df)[:2]
    at.session_state[ROOT_KEY] = path
    at.run()
    widget(at.button, label="⟲ Zoom out").click()
    at.run()
    assert at.session_state[ROOT_KEY] == path[:1]


def test_given_deepest_drill_when_rendered_then_no_degenerate_chart_crash(at, cube_df):
    # Regression: a full-depth root used to build a single-node chart that
    # crashed the component and snapped the drill back to the top.
    at.session_state[ROOT_KEY] = _top_full_path(cube_df)
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert any("Deepest level reached" in t for t in texts(at))


def test_given_stale_root_when_rendered_then_root_resets_to_top(at):
    # e.g. a recategorize run renamed the category a previous session drilled into
    at.session_state[ROOT_KEY] = ["NO_SUCH_TIER0", "NO_SUCH_TIER1"]
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.session_state[ROOT_KEY] == []
