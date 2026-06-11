"""Regression: the data cache must bust when the archive/config signature changes.

`app._load` is keyed on `_config_signature(app)` (archive + config mtime/size).
Streamlit's `st.cache_data` silently EXCLUDES parameters whose names start with an
underscore from the cache key — naming the parameter `_signature` pinned the cache to
the first load for the whole server session, so a pipeline run while the UI was open
never showed the new data. These tests call the real cached function with a stubbed
`build_cube` and assert on how often it actually executes.
"""
from types import SimpleNamespace

import pandas as pd

import app


def _stub_build_cube(calls):
    def _build():
        calls.append(1)
        return SimpleNamespace(df=pd.DataFrame({"x": [1]}), qc={})
    return _build


def test_same_signature_hits_cache(monkeypatch):
    calls: list = []
    monkeypatch.setattr(app, "build_cube", _stub_build_cube(calls))
    app._load.clear()
    app._load(("archive", 1.0, 10))
    app._load(("archive", 1.0, 10))
    assert len(calls) == 1, "identical signature must reuse the cached load"


def test_changed_signature_reloads(monkeypatch):
    calls: list = []
    monkeypatch.setattr(app, "build_cube", _stub_build_cube(calls))
    app._load.clear()
    app._load(("archive", 1.0, 10))
    app._load(("archive", 2.0, 11))  # archive mtime/size changed (new fetch)
    assert len(calls) == 2, "a changed signature must bust the cache and reload"
