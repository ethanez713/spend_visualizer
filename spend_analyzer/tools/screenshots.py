#!/usr/bin/env python3
"""Regenerate the README screenshots from SYNTHETIC demo data — never real data.

Builds a fresh temp data root with tools.gen_demo_data (fixed seed), starts the
analyzer with SPEND_VISUALIZER_DATA pointed only at that root, and writes one
PNG per view. Refuses to run if it cannot create its own temp root.

Usage:
    python -m tools.screenshots [--out DIR]

Default --out is <repo>/docs/img (overview.png, budget.png, merchants.png).
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlencode

from tests.e2e import _browser, _server
from tools.gen_demo_data import generate

REPO = Path(__file__).resolve().parents[2]
DEFAULT_OUT = REPO / "docs" / "img"
WIDTH = 1440
# Same seed and span as tools.gen_demo_data's defaults, passed explicitly so a
# rerun cannot drift if those defaults change.
SEED = 7
MONTHS = 9
_MERCHANT = "Green Valley Market"

# Chromium's extra libs live next to the Playwright browsers (see tests/e2e/conftest.py).
_EXTRA_LIBS = Path.home() / ".cache/ms-playwright/extra-libs/extracted/usr/lib/x86_64-linux-gnu"

# wait_idle's rerun-start budget returns before a chart paints — the same race
# click_sector avoids by waiting on the breadcrumb trail. Wait until this view's
# own markers and a drawn Plotly series are in the DOM.
_READY_JS = """
(spec) => {
  const app = document.querySelector('.stApp');
  if (!app || app.getAttribute('data-test-script-state') === 'running') return false;
  const fail = (why) => { window.__shot = why; return false; };
  if (document.querySelector('[data-testid="stSpinner"]')) return fail('spinner');
  if (document.querySelector('[data-testid="stException"]')) return fail('exception');

  const group = document.querySelector('.st-key-view');
  let selected = '';
  const input = group && group.querySelector('input:checked');
  if (input) {
    const label = input.closest('label');
    selected = label ? label.innerText : '';
  } else if (group) {
    const aria = group.querySelector('[role="radio"][aria-checked="true"]');
    selected = aria ? aria.innerText : '';
  }
  selected = selected.replace(/\\s+/g, ' ').trim();
  if (selected !== spec.view) return fail('view:' + selected);

  // innerText applies CSS text-transform, so metric labels arrive uppercase.
  const text = (document.body.innerText || '').toLowerCase();
  for (const needle of spec.text) {
    if (!text.includes(String(needle).toLowerCase())) return fail('text:' + needle);
  }

  const charts = Array.from(document.querySelectorAll('[data-testid="stPlotlyChart"]'));
  if (charts.length < spec.charts) return fail('charts:' + charts.length);
  for (const chart of charts) {
    const box = chart.getBoundingClientRect();
    const svg = chart.querySelector('svg');
    const series = chart.querySelector(
      '.scatterlayer path, .barlayer path, .pielayer path, .js-line');
    if (!svg || box.width < 240 || box.height < 80 || !series) {
      return fail('chart-paint:' + Math.round(box.width) + 'x' + Math.round(box.height));
    }
  }

  const frames = Array.from(document.querySelectorAll(
      '[data-testid="stDataFrame"], [data-testid="stDataEditor"]'));
  const painted = frames.filter(frame => {
    const box = frame.getBoundingClientRect();
    const canvas = frame.querySelector('canvas');
    return box.width > 80 && box.height > 40 && canvas
      && canvas.width > 0 && canvas.height > 0;
  });
  if (painted.length < spec.frames) {
    return fail('frames:' + painted.length + '/' + frames.length);
  }
  window.__shot = 'ok';
  return true;
}
"""

_HEIGHT_JS = """
() => {
  const main = document.querySelector('[data-testid="stMain"]');
  const side = document.querySelector('[data-testid="stSidebar"]');
  const mainH = main ? main.scrollHeight : 0;
  const sideH = side ? side.scrollHeight : 0;
  return Math.ceil(Math.max(mainH, sideH, 900));
}
"""

_VIEWS = [
    {
        "view": "Overview",
        "file": "overview.png",
        "text": ["Spending pace", "Largest purchases", "Savings rate"],
        "charts": 1,
        "frames": 2,
    },
    {
        "view": "Budget",
        "file": "budget.png",
        "text": ["Budget vs actual", "Trends"],
        "charts": 1,
        "frames": 2,
    },
    {
        "view": "Merchants",
        "file": "merchants.png",
        "text": ["Top merchants", "Recurring and subscriptions"],
        "charts": 0,
        "frames": 1,
        "merchant": _MERCHANT,
    },
]


def _prepare_libs() -> None:
    if _EXTRA_LIBS.is_dir():
        os.environ["LD_LIBRARY_PATH"] = (
            f"{_EXTRA_LIBS}:{os.environ.get('LD_LIBRARY_PATH', '')}")
    # Playwright's node driver warns if both are set; neither is needed here.
    os.environ.pop("FORCE_COLOR", None)
    os.environ.pop("NO_COLOR", None)


def _make_temp_root() -> Path:
    """A directory we just created under the system temp dir, or refuse."""
    try:
        root = Path(tempfile.mkdtemp(prefix="spend_screenshots_"))
    except OSError as exc:
        raise SystemExit(f"refusing to run: cannot create a temp data root ({exc})") from exc
    root = root.resolve()
    tmp = Path(tempfile.gettempdir()).resolve()
    banned = (Path.home() / "finance_data").resolve()
    if root != tmp and tmp not in root.parents:
        raise SystemExit("refusing to run: temp data root is outside the system temp dir")
    if root == banned or banned in root.parents or root in banned.parents:
        raise SystemExit("refusing to run: temp data root collides with the user data root")
    if not root.is_dir():
        raise SystemExit("refusing to run: temp data root was not created")
    return root


def _assert_demo(root: Path) -> None:
    store = root / "plaid_category_transformer" / "data" / "transactions_categorized.jsonl"
    accounts = root / "spend_analyzer" / "config" / "accounts.yaml"
    budget = root / "spend_analyzer" / "config" / "budget.yaml"
    if store.is_symlink() or not store.is_file() or store.stat().st_size == 0:
        raise SystemExit("refusing to run: synthetic store was not written")
    if root not in store.resolve().parents:
        raise SystemExit("refusing to run: synthetic store escaped the temp root")
    if not accounts.is_file() or not budget.is_file():
        raise SystemExit("refusing to run: synthetic config was not written")


def _launch(log_dir: Path, demo: Path) -> tuple[subprocess.Popen, str]:
    # Don't resolve(): the venv python is a symlink to the system interpreter,
    # and streamlit lives beside the symlink, not beside /usr/bin/python3.
    streamlit = Path(sys.executable).parent / "streamlit"
    if not streamlit.is_file():
        raise SystemExit(f"refusing to run: streamlit is not next to {sys.executable}")
    demo = demo.resolve()
    env = os.environ.copy()
    # Drop every pointer that could send the app at the user's data. The only
    # data root the child is allowed to see is the temp demo root.
    for key in ("SPEND_VISUALIZER_DATA", "SPEND_VISUALIZER_CONVERTER",
                "SPEND_ANALYZER_CONFIG_DIR", "SPEND_ANALYZER_DATA_DIR"):
        env.pop(key, None)
    env["SPEND_VISUALIZER_DATA"] = str(demo)
    env["STREAMLIT_BROWSER_GATHER_USAGE_STATS"] = "false"
    if env.get("SPEND_VISUALIZER_DATA") != str(demo):
        raise SystemExit("refusing to run: failed to pin SPEND_VISUALIZER_DATA to the temp root")

    port = _server.free_port()
    log = log_dir / "server.log"
    proc = subprocess.Popen(
        [str(streamlit), "run", "app.py",
         f"--server.port={port}", "--server.headless=true",
         "--server.address=127.0.0.1", "--server.fileWatcherType=none",
         "--browser.gatherUsageStats=false"],
        cwd=_server.ROOT, env=env, stdout=log.open("w"), stderr=subprocess.STDOUT,
        start_new_session=True)
    url = f"http://127.0.0.1:{port}"
    try:
        _server._wait_healthy(url, proc, log, timeout=90.0)
    except Exception:
        _stop(proc)
        raise
    return proc, url


def _stop(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        proc.wait(timeout=5)


def _discard(path: Path | None) -> None:
    if path is None:
        return
    tmp = Path(tempfile.gettempdir()).resolve()
    resolved = path.resolve()
    if resolved == tmp or tmp not in resolved.parents:
        return
    shutil.rmtree(resolved, ignore_errors=True)


def _wait_rendered(page, spec: dict) -> None:
    try:
        page.wait_for_function(_READY_JS, arg=spec, timeout=45000)
    except Exception:
        _dump(page)
        raise
    _browser.assert_no_exception(page)


def _dump(page) -> None:
    try:
        info = page.evaluate("""() => ({
          why: window.__shot || '',
          fonts: document.fonts ? document.fonts.status : '',
          state: document.querySelector('.stApp')?.getAttribute('data-test-script-state') || '',
          checked: (document.querySelector('.st-key-view input:checked')
                    ?.closest('label')?.innerText || '').trim(),
          charts: document.querySelectorAll('[data-testid="stPlotlyChart"]').length,
          frames: document.querySelectorAll('[data-testid="stDataFrame"]').length,
          exc: (document.querySelector('[data-testid="stException"]')?.innerText || '').slice(0, 600),
          text: (document.body.innerText || '').slice(0, 900),
        })""")
    except Exception as exc:
        print(f"page dump failed: {exc}", file=sys.stderr)
        return
    print(info, file=sys.stderr)


def _fit(page) -> None:
    """Match the viewport to the rendered page so the shot is not clipped."""
    height = int(page.evaluate(_HEIGHT_JS))
    height = max(900, min(height, 3600))
    if page.viewport_size and page.viewport_size.get("height") == height:
        return
    page.set_viewport_size({"width": WIDTH, "height": height})


def _pick_merchant(page, name: str) -> None:
    page.locator('[data-testid="stMain"]').get_by_role("combobox").first.click()
    page.get_by_role("option", name=name, exact=True).click()


def _shoot(page, base_url: str, spec: dict, dest: Path) -> None:
    _browser.open_app(page, f"{base_url}/?{urlencode({'view': spec['view']})}")
    _wait_rendered(page, spec)
    if spec.get("merchant"):
        _pick_merchant(page, spec["merchant"])
        detail = dict(spec)
        detail["text"] = [f"{spec['merchant']} —", "tick Flag", "Recurring and subscriptions"]
        detail["charts"] = 1
        detail["frames"] = 2
        _wait_rendered(page, detail)
        spec = detail
    _fit(page)
    _wait_rendered(page, spec)
    # Drop the focus ring left on the merchant search, and don't shoot while a
    # webfont is still swapping in.
    page.evaluate("""() => {
      if (document.activeElement) document.activeElement.blur();
      const fonts = document.fonts ? document.fonts.ready : Promise.resolve();
      const cap = new Promise(resolve => setTimeout(resolve, 4000));
      return Promise.race([fonts, cap]);
    }""")
    page.screenshot(path=str(dest), animations="disabled", caret="hide")


def _png_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if len(data) < 32 or data[:8] != b"\x89PNG\r\n\x1a\n" or b"IEND" not in data[-24:]:
        raise SystemExit(f"{path} is not a complete PNG")
    width, height = struct.unpack(">II", data[16:24])
    if width < 100 or height < 100:
        raise SystemExit(f"{path} is {width}x{height}, which is too small to be a view")
    return width, height


def _capture(url: str, out: Path) -> list[tuple[Path, int, int]]:
    from playwright.sync_api import sync_playwright

    saved: list[tuple[Path, int, int]] = []
    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(headless=True)
        except Exception as exc:
            raise SystemExit(f"could not launch Chromium ({exc})") from exc
        context = browser.new_context(
            viewport={"width": WIDTH, "height": 1200},
            color_scheme="light",
            device_scale_factor=1,
            locale="en-US",
        )
        page = context.new_page()
        page.set_default_timeout(20000)
        try:
            for spec in _VIEWS:
                dest = out / spec["file"]
                _shoot(page, url, spec, dest)
                saved.append((dest, *_png_size(dest)))
        finally:
            context.close()
            browser.close()
    return saved


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=None,
                    help="directory for the PNGs (default: <repo>/docs/img)")
    args = ap.parse_args()
    out = args.out if args.out is not None else DEFAULT_OUT
    out.mkdir(parents=True, exist_ok=True)

    _prepare_libs()
    demo = log_dir = None
    proc = None
    try:
        demo = _make_temp_root()
        generate(demo, MONTHS, SEED)
        _assert_demo(demo)
        log_dir = _make_temp_root()
        proc, url = _launch(log_dir, demo)
        saved = _capture(url, out)
    except Exception:
        if log_dir is not None:
            log = log_dir / "server.log"
            if log.is_file():
                print(log.read_text(encoding="utf-8", errors="replace")[-2000:], file=sys.stderr)
        raise
    finally:
        _stop(proc)
        _discard(log_dir)
        _discard(demo)

    for path, width, height in saved:
        print(f"{path} {width}x{height}")


if __name__ == "__main__":
    main()
