"""Drill (wheel_root) state: breadcrumb/zoom navigation and its crash guards.

A real chart click cannot be simulated (streamlit-plotly-events is a custom
component, outside AppTest's reach) — the click→path mapping is unit-tested in
tests/test_drilldown.py. Here we set ``wheel_root`` directly, which is exactly
the state a click produces, and test everything downstream of it.
"""
import pytest

from cube import CATEGORY_LEVELS
from page_nav import NAV_KEY
from views.spending import BUDGET_KEY, PERIOD_KEY, ROOT_KEY

from tests.ui._harness import go, texts, widget


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
    assert crumbs[0] == "All" and str(path[1]) in crumbs[2], crumbs
    assert any(f"{path[0]} › {path[1]}" in t for t in texts(at)), \
        "transaction detail does not follow the drill root"


def test_given_drilled_root_when_zoom_out_clicked_then_root_pops_one_level(at, cube_df):
    path = _top_full_path(cube_df)[:2]
    at.session_state[ROOT_KEY] = path
    at.run()
    widget(at.button, label="Zoom out").click()
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


def _list_caption(at) -> str:
    return next(t for t in texts(at) if " transactions · " in t or " transaction · " in t)


def test_given_a_focus_and_a_search_when_rendered_then_the_list_is_their_intersection(at, cube_df):
    path = _top_full_path(cube_df)[:2]
    in_focus = cube_df[(cube_df["flow"] == "spend") & (cube_df[CATEGORY_LEVELS[0]] == path[0])
                       & (cube_df[CATEGORY_LEVELS[1]] == path[1])]
    merchant = in_focus["merchant"].mode().iat[0]
    at.session_state[ROOT_KEY] = path
    at.run()

    widget(at.text_input, key="txn_search").set_value(merchant).run()

    assert not at.exception, [str(e.value) for e in at.exception]
    assert f"{path[0]} › {path[1]}" in _list_caption(at)
    assert "chip_txn_search" in [b.key for b in at.button]
    plain_list = [t for t in texts(at) if " — " in t and " transaction" in t][0]
    with_filter = int(plain_list.split(" — ")[1].split(" ")[0].replace(",", ""))
    widget(at.text_input, key="txn_search").set_value("").run()
    without_filter = int(_list_caption(at).split(" — ")[1].split(" ")[0].replace(",", ""))
    assert 0 < with_filter <= without_filter


def test_given_a_search_chip_when_pressed_then_the_search_clears_and_the_focus_stays(at, cube_df):
    path = _top_full_path(cube_df)[:2]
    at.session_state[ROOT_KEY] = path
    at.run()
    widget(at.text_input, key="txn_search").set_value("a").run()

    widget(at.button, key="chip_txn_search").click()
    at.run()

    assert widget(at.text_input, key="txn_search").value == ""
    assert at.session_state[ROOT_KEY] == path


def _nav(**over):
    return {"page": "Spending", "focus": [], "filters": {"search": "", "budget_cat": None},
            "period": None, **over}


def test_given_a_pending_nav_when_spending_opens_then_focus_and_period_apply_and_stay_until_cleared(
        at, cube_df):
    path = _top_full_path(cube_df)[:2]
    at.session_state[NAV_KEY] = _nav(focus=path, period=("2026-08-01", "2026-08-31", "Aug 2026"))
    go(at, "Spending")

    assert NAV_KEY not in at.session_state
    assert at.session_state[ROOT_KEY] == path
    assert any(b.label == "Period: Aug 2026 ×" for b in at.button)

    at.run()
    assert any(b.label == "Period: Aug 2026 ×" for b in at.button)

    widget(at.button, key=f"chip_{PERIOD_KEY}").click()
    at.run()
    assert not any(b.label == "Period: Aug 2026 ×" for b in at.button)
    assert at.session_state[ROOT_KEY] == path


def test_given_a_budget_filter_nav_when_spending_opens_then_only_that_category_is_listed(at, cube_df):
    category = "Dining Out"
    expected = cube_df[cube_df["budget_cat"] == category]
    if expected.empty:
        pytest.skip("no Dining Out rows in the archive")
    at.session_state[NAV_KEY] = _nav(filters={"search": "", "budget_cat": category})
    go(at, "Spending")

    assert any(b.label == f"Budget: {category} ×" for b in at.button)
    assert at.session_state[BUDGET_KEY] == category
    widget(at.button, key=f"chip_{BUDGET_KEY}").click()
    at.run()
    assert at.session_state[BUDGET_KEY] is None


def test_given_a_search_nav_when_spending_opens_then_the_search_box_holds_it(at):
    at.session_state[NAV_KEY] = _nav(filters={"search": "acme", "budget_cat": None})
    go(at, "Spending")
    assert widget(at.text_input, key="txn_search").value == "acme"
