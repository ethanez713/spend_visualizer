"""Helpers for the AppTest UI suite: boot the real app, find widgets, read text.

The suite drives the actual `app.py` headless via `streamlit.testing.v1.AppTest`
(no browser, no server) against the real read-only archive. Write paths are
redirected by the autouse fixtures in conftest.py — see there before adding any
test that presses a save/revoke/delete button.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[2]
APP = str(ROOT / "app.py")


VIEWS = ["Overview", "Drilldown", "Transactions", "Budget", "Merchants",
         "Cash flow", "Review", "Suggestions", "Overrides", "Rules", "Checks"]


def go(at: AppTest, view: str) -> AppTest:
    """Switch to a view (only the active one renders) and fail on any exception."""
    widget(at.radio, key="view").set_value(view)
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def boot(view: str = "Drilldown") -> AppTest:
    """Run app.py against the real archive and open `view`; fail on any uncaught
    exception."""
    from config_io import load_app_config

    archives = load_app_config().resolved_archive_paths
    if not all(Path(p).exists() for p in archives):
        pytest.skip("real transaction archive not present")
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return go(at, view)


def widget(seq, *, key: str | None = None, key_prefix: str | None = None,
           label: str | None = None):
    """First widget in an AppTest accessor matching key / key prefix / label."""
    for w in seq:
        if key is not None and w.key == key:
            return w
        if key_prefix is not None and (w.key or "").startswith(key_prefix):
            return w
        if label is not None and getattr(w, "label", None) == label:
            return w
    have = [(w.key, getattr(w, "label", None)) for w in seq]
    raise AssertionError(
        f"no widget with key={key!r} key_prefix={key_prefix!r} label={label!r}; "
        f"present: {have}")


def metric(at: AppTest, label: str) -> str:
    """Value of the first metric with this label in the active view."""
    return widget(at.metric, label=label).value


def spend_table(at: AppTest):
    """The Drilldown 'Spend by …' table."""
    for d in at.dataframe:
        if "Actual" in d.value.columns:
            return d.value
    raise AssertionError("spend table not rendered")


def open_first_merchant(at: AppTest) -> None:
    """Open a merchant's detail (and its fix form) through the merchant search."""
    go(at, "Merchants")
    box = widget(at.selectbox, key="merchant_search")
    box.set_value(box.options[0])
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]


def metrics(at: AppTest, label: str) -> list[str]:
    return [m.value for m in at.metric if m.label == label]


def money_to_f(s: str) -> float:
    return float(str(s).replace("$", "").replace(",", ""))


def texts(at: AppTest) -> list[str]:
    """All visible text fragments (markdown, captions, alerts, headers)."""
    out: list[str] = []
    for accessor in ("markdown", "caption", "info", "success", "warning",
                     "error", "title", "header", "subheader"):
        for el in getattr(at, accessor, []):
            out.append(str(el.value))
    return out
