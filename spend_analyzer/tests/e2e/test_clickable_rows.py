"""A row in a summary table opens the transactions behind it. Selecting a row needs a real
browser: AppTest cannot drive st.dataframe's selection."""
import re

import pytest
from playwright.sync_api import Page, expect

from tests.e2e._browser import assert_no_exception, click_grid_cell, go, wait_idle

pytestmark = pytest.mark.e2e

CHECKBOX_X = 16      # row-selection column center in a single-row table
OPEN_COL_X = 110     # the Open tick's column center in the transaction editor


def _pick_first_row(page: Page, key: str) -> None:
    click_grid_cell(page, page.locator(f'.st-key-{key} [data-testid="stDataFrame"]'), row=0,
                    col_center_x=CHECKBOX_X)


def _chip(page: Page, text: str):
    return page.get_by_role("button", name=re.compile(rf"^{text}.* ×$"))


def _assert_landed_on_spending(page: Page) -> None:
    assert_no_exception(page)
    expect(page).to_have_url(re.compile(r"/spending"))


def test_given_budget_summary_row_when_picked_then_spending_filters_that_category(app):
    go(app, "Budget")
    _pick_first_row(app, "budget_pick")
    _assert_landed_on_spending(app)
    expect(_chip(app, "Budget: ")).to_be_visible()


def test_given_monthly_grid_row_when_picked_then_spending_has_category_and_period(app):
    go(app, "Budget")
    app.get_by_text("Monthly grid", exact=True).first.click()
    wait_idle(app)
    _pick_first_row(app, "grid_pick")
    _assert_landed_on_spending(app)
    expect(_chip(app, "Budget: ")).to_be_visible()
    expect(_chip(app, "Period: ")).to_be_visible()


def test_given_biggest_change_row_when_picked_then_spending_focuses_with_the_window(app):
    go(app, "Overview")
    _pick_first_row(app, "changes_pick")
    _assert_landed_on_spending(app)
    expect(_chip(app, "Period: ")).to_be_visible()


def test_given_cash_flow_row_when_picked_then_spending_shows_that_period(app):
    go(app, "Cash flow")
    _pick_first_row(app, "cash_pick")
    _assert_landed_on_spending(app)
    expect(_chip(app, "Period: ")).to_be_visible()


def test_given_largest_purchase_open_ticked_then_spending_searches_the_merchant(app):
    go(app, "Overview")
    click_grid_cell(app, app.locator('.st-key-txned_overview_top [data-testid="stDataFrame"]'), row=0,
                    col_center_x=OPEN_COL_X)
    _assert_landed_on_spending(app)
    expect(_chip(app, "search ")).to_be_visible()


def test_given_recurring_row_when_picked_then_that_merchant_opens_on_the_same_page(app):
    go(app, "Merchants")
    _pick_first_row(app, "recurring_pick")
    assert_no_exception(app)
    expect(app).to_have_url(re.compile(r"/merchants"))
    expect(app.get_by_role("heading", name="Recurring and subscriptions")).to_be_visible()
    expect(app.get_by_text("Merge with another merchant…")).to_be_visible()


def test_given_spend_row_when_picked_then_the_focus_moves_and_its_chip_clears_it_for_good(app):
    click_grid_cell(app, app.locator('.st-key-spend_sel_tier1 [data-testid="stDataFrame"]'), row=0,
                    col_center_x=CHECKBOX_X)
    assert_no_exception(app)
    chip = app.locator('.st-key-chip_wheel_root button')
    expect(chip).to_be_visible()
    chip.click()
    wait_idle(app)
    expect(chip).to_have_count(0)
