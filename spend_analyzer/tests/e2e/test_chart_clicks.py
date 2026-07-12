"""Real-browser clicks on the plotly hierarchy chart (streamlit-plotly-events).

The chart iframe is the app's most bug-prone surface and is unreachable from
AppTest. These tests drive the actual click → component event → rerun → drill
loop in Chromium: every sector click must land on the clicked path, zoom-out /
breadcrumb must navigate back (and STAY — the component re-sends the last click
on every rerun, the historical "snaps back down" bug), and all three chart
kinds plus the window selector must render the live data without error.
"""
import re

import pytest
from playwright.sync_api import expect

from tests.e2e._browser import (CHART_IFRAME, assert_no_exception, breadcrumbs,
                                click_sector, metric_value, sector_labels,
                                wait_idle)

pytestmark = pytest.mark.e2e


def test_given_app_loaded_when_warmup_finishes_then_sunburst_has_sized_sectors(app):
    # Regression: the component used to first paint mis-sized (hidden text)
    # until the one-time warm-up rerun; assert the settled chart is usable.
    labels = sector_labels(app)
    assert len(labels) >= 5, f"too few labelled sectors: {labels}"
    box = app.locator(CHART_IFRAME).bounding_box()
    assert box["width"] > 400 and box["height"] > 400, box
    assert_no_exception(app)


def test_given_sunburst_when_sector_clicked_then_app_drills_to_that_path(app):
    top = sector_labels(app)[0]
    click_sector(app, top)
    crumbs = breadcrumbs(app)
    assert crumbs[-1] == top["label"], crumbs
    assert crumbs[0] == "🏠 All" and len(crumbs) >= 2, crumbs
    # the transaction-detail table follows the chart ("<path> — N txns")
    expect(app.get_by_text(
        re.compile(rf"{re.escape(top['label'])} — \d+ txns")).first).to_be_visible()
    assert_no_exception(app)


def test_given_drilled_chart_when_clicked_again_then_drills_deeper(app):
    click_sector(app, sector_labels(app)[0])
    depth_1 = len(breadcrumbs(app))
    inner = sector_labels(app)[0]
    click_sector(app, inner)
    crumbs = breadcrumbs(app)
    assert len(crumbs) > depth_1, (depth_1, crumbs)
    assert crumbs[-1] == inner["label"], crumbs
    assert_no_exception(app)


def test_given_drilled_chart_when_zoomed_out_then_pops_one_level_and_stays(app):
    # THE regression: the component keeps returning the last click across
    # reruns; without the processed-click guard, zoom-out (or any later rerun)
    # would re-fire it and snap the drill back down.
    click_sector(app, sector_labels(app)[0])
    drilled = breadcrumbs(app)
    app.get_by_role("button", name="⟲ Zoom out").click()
    wait_idle(app)
    assert breadcrumbs(app) == drilled[:-1]
    app.get_by_text("3 mo", exact=True).click()   # unrelated rerun
    wait_idle(app)
    assert breadcrumbs(app) == drilled[:-1], "stale click re-fired after a rerun"


def test_given_drilled_chart_when_home_crumb_clicked_then_back_to_top(app):
    click_sector(app, sector_labels(app)[0])
    app.get_by_role("button", name="🏠 All").click()
    wait_idle(app)
    assert breadcrumbs(app) == ["🏠 All"]
    assert len(sector_labels(app)) >= 5, "top-level chart did not come back"


def test_given_treemap_when_tile_clicked_then_drills(app):
    app.get_by_text("Treemap", exact=True).click()
    wait_idle(app)
    top = sector_labels(app)[0]
    click_sector(app, top)
    assert breadcrumbs(app)[-1] == top["label"]
    assert_no_exception(app)


def test_given_window_narrowed_then_chart_rerenders_without_error(app):
    app.get_by_text("3 mo", exact=True).click()
    wait_idle(app)
    assert_no_exception(app)
    app.get_by_text("1 mo", exact=True).click()
    wait_idle(app)
    assert_no_exception(app)
    assert sector_labels(app), "no sectors after narrowing the window"
