"""Real-browser element selections AppTest cannot simulate: glide-grid canvas
row ticks (st.dataframe on_select → bulk Hide/Flag), the data_editor 🚩
checkbox, and the merchant-detail dropdown."""
import re

import pytest
from playwright.sync_api import expect

from tests.e2e._browser import (assert_no_exception, click_grid_cell,
                                grid_canvas, metric_value, money_to_f,
                                sector_labels, wait_idle)

pytestmark = pytest.mark.e2e

CHECKBOX_X = 16     # row-selection column center in the spend table
FLAG_COL_X = 30     # 🚩 column center in the transaction-detail editor


def test_given_two_rows_ticked_then_bulk_buttons_show_count(app):
    grid = grid_canvas(app, 0)
    click_grid_cell(app, grid, row=0, col_center_x=CHECKBOX_X)
    click_grid_cell(app, grid, row=1, col_center_x=CHECKBOX_X)
    expect(app.get_by_role("button", name="👁 Hide 2")).to_be_visible()
    expect(app.get_by_role("button", name="🚩 Flag 2")).to_be_visible()


def test_given_row_hidden_then_total_drops_and_show_all_restores(app):
    base = money_to_f(metric_value(app, "Total"))
    click_grid_cell(app, grid_canvas(app, 0), row=0, col_center_x=CHECKBOX_X)
    app.get_by_role("button", name="👁 Hide 1").click()
    wait_idle(app)
    assert money_to_f(metric_value(app, "Total")) < base
    app.get_by_role("button", name="Show all").click()
    wait_idle(app)
    assert money_to_f(metric_value(app, "Total")) == base


def test_given_top_category_hidden_then_chart_drops_it_and_notes_it(app, top_tier1):
    click_grid_cell(app, grid_canvas(app, 0), row=0, col_center_x=CHECKBOX_X)
    app.get_by_role("button", name="👁 Hide 1").click()
    wait_idle(app)
    assert all(d["label"] != top_tier1 for d in sector_labels(app)), \
        f"hidden category {top_tier1!r} still charted"
    expect(app.get_by_text(re.compile(
        rf"🙈 Hidden \(not charted\):.*{re.escape(top_tier1)}")).first).to_be_visible()


def test_given_row_flagged_then_correction_form_opens(app):
    click_grid_cell(app, grid_canvas(app, 0), row=0, col_center_x=CHECKBOX_X)
    app.get_by_role("button", name="🚩 Flag 1").click()
    wait_idle(app)
    expect(app.get_by_text("Flag a categorization problem").first).to_be_visible()
    assert_no_exception(app)


def test_given_transaction_flag_ticked_then_fix_expander_opens(app):
    # grid 1 = the drilldown transaction-detail st.data_editor (🚩 first column)
    click_grid_cell(app, grid_canvas(app, 1), row=0, col_center_x=FLAG_COL_X)
    expect(app.get_by_text("Fix categorization —").first).to_be_visible()
    assert_no_exception(app)


def test_given_merchant_picked_in_dropdown_then_detail_follows(app):
    app.get_by_role("tab", name="🏪 Merchants & recurring").click()
    app.locator('[data-testid="stSelectbox"]:visible').first.click()
    second = app.locator('li[role="option"]').nth(1)
    picked = second.inner_text().strip()
    second.click()
    wait_idle(app)
    expect(app.get_by_text(
        re.compile(rf"{re.escape(picked)} — \d+ txns")).first).to_be_visible()
    assert_no_exception(app)
