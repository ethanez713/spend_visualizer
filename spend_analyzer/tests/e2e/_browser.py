"""Playwright helpers: Streamlit idle-waits, plotly-iframe access, canvas clicks."""
from __future__ import annotations

from playwright.sync_api import FrameLocator, Page, expect

CHART_IFRAME = 'iframe[title="streamlit_plotly_events.plotly_events"]'

# Breadcrumb-trail buttons (the drill path). A sector click's observable effect is
# this trail changing — used both to read the trail and to wait for a drill to land.
_CRUMB_BUTTONS = 'div[class*="st-key-crumb_"] button'

# Glide-data-grid geometry (Streamlit 1.40 defaults). The probe test validated
# these against the live DOM; revisit if a Streamlit upgrade changes the grid.
GRID_HEADER_H = 35
GRID_ROW_H = 35


def open_app(page: Page, url: str) -> None:
    page.goto(url)
    expect(page.get_by_text("📊 Spend Analyzer")).to_be_visible(timeout=45000)
    wait_idle(page)


def wait_idle(page: Page, runs: int = 4) -> None:
    """Wait until Streamlit stops (re)running — reruns can chain (warm-up)."""
    for _ in range(runs):
        try:
            page.wait_for_selector('[data-testid="stStatusWidget"]',
                                   state="attached", timeout=700)
        except Exception:
            return  # no (further) run started
        page.wait_for_selector('[data-testid="stStatusWidget"]',
                               state="detached", timeout=60000)


def chart_frame(page: Page) -> FrameLocator:
    return page.frame_locator(CHART_IFRAME)


def sector_labels(page: Page) -> list[dict]:
    """Visible slice-label texts in the hierarchy chart with page-coordinate
    bounding boxes, largest first (a big label = a comfortably clickable slice)."""
    page.locator(CHART_IFRAME).scroll_into_view_if_needed()
    texts = chart_frame(page).locator("g.slicetext text")
    out = []
    for i in range(texts.count()):
        el = texts.nth(i)
        box = el.bounding_box()
        label = (el.text_content() or "").strip()
        if box and box["width"] > 0 and label:
            out.append({"label": label, "box": box,
                        "area": box["width"] * box["height"]})
    return sorted(out, key=lambda d: -d["area"])


def click_sector(page: Page, sector: dict) -> None:
    """Click the middle of a slice label (labels pass pointer events through to the
    slice) and wait for the drill to actually land.

    The click round-trips iframe → websocket → server rerun (~1.5s on a busy
    machine) — far longer than wait_idle's rerun-start budget, so a bare
    wait_idle() can return *before* the rerun even begins and the caller then reads
    stale, pre-click DOM (the historical chart-click flakiness). Wait on the drill's
    observable effect — the breadcrumb trail changing — then let the rerun settle.
    """
    before = page.eval_on_selector_all(
        _CRUMB_BUTTONS, "els => els.map(e => e.innerText.trim())")
    box = sector["box"]
    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.wait_for_function(
        "([sel, prev]) => {"
        "  const cur = Array.from(document.querySelectorAll(sel))"
        "    .map(e => e.innerText.trim());"
        "  return JSON.stringify(cur) !== JSON.stringify(prev);"
        "}",
        arg=[_CRUMB_BUTTONS, before], timeout=15000)
    wait_idle(page)


def breadcrumbs(page: Page) -> list[str]:
    crumbs = page.locator(_CRUMB_BUTTONS)
    return [crumbs.nth(i).inner_text().strip() for i in range(crumbs.count())]


def grid_canvas(page: Page, index: int = 0):
    """The index-th VISIBLE data grid (st.dataframe / st.data_editor share it)."""
    return page.locator('[data-testid="stDataFrame"]:visible').nth(index)


def click_grid_cell(page: Page, grid, row: int, col_center_x: float) -> None:
    """Click inside a grid cell by geometry (the grid is a canvas — no DOM cells)."""
    grid.scroll_into_view_if_needed()
    box = grid.bounding_box()
    x = box["x"] + col_center_x
    y = box["y"] + GRID_HEADER_H + GRID_ROW_H * row + GRID_ROW_H / 2
    page.mouse.click(x, y)
    wait_idle(page)


def assert_no_exception(page: Page) -> None:
    exc = page.locator('[data-testid="stException"]')
    assert exc.count() == 0, f"app rendered an exception: {exc.first.inner_text()[:500]}"


def metric_value(page: Page, label: str) -> str:
    m = page.locator('[data-testid="stMetric"]',
                     has=page.get_by_text(label, exact=True)).first
    return m.locator('[data-testid="stMetricValue"]').inner_text().strip()


def money_to_f(s: str) -> float:
    return float(s.replace("$", "").replace(",", ""))
