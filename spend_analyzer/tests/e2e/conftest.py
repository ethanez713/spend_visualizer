"""Browser e2e fixtures: one isolated app server per session, fresh page per test.

Isolation is environmental (see _server.py): the subprocess app reads the real
archive read-only but every write path points into pytest tmp dirs. The same
live-data hash guard as the AppTest suite runs around every test.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

# Chromium needs a few system libs that aren't installed machine-wide (no sudo);
# they were extracted next to the Playwright browsers. `sudo playwright
# install-deps chromium` is the cleaner permanent fix.
_EXTRA_LIBS = Path.home() / ".cache/ms-playwright/extra-libs/extracted/usr/lib/x86_64-linux-gnu"
if _EXTRA_LIBS.is_dir():
    os.environ["LD_LIBRARY_PATH"] = (
        f"{_EXTRA_LIBS}:{os.environ.get('LD_LIBRARY_PATH', '')}")

from tests._liveguard import digest_all
from tests.e2e import _browser, _server


@pytest.fixture(scope="session")
def app_server(tmp_path_factory):
    base = tmp_path_factory.mktemp("e2e_app")
    proc, url = _server.launch(base)
    yield url
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()


@pytest.fixture(autouse=True)
def live_data_untouched():
    before = digest_all()
    yield
    assert digest_all() == before, "an e2e test mutated live data files!"


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    # Tall, wide viewport: keeps the 560px chart and the spend table fully
    # on-screen so geometry-based canvas clicks are stable.
    return {**browser_context_args, "viewport": {"width": 1600, "height": 1400}}


@pytest.fixture
def app(page, app_server):
    """A fresh browser session with the app loaded and idle."""
    _browser.open_app(page, app_server)
    return page


@pytest.fixture(scope="session")
def top_tier1() -> str:
    """Ground truth: the tier1 with the most spend (= top spend-table row)."""
    from data import build_cube
    df = build_cube().df
    return df[df["flow"] == "spend"].groupby("tier1")["spend"].sum().idxmax()
