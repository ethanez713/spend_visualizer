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


def _sig_names(sig) -> set:
    return {entry[0] for entry in sig if isinstance(entry, tuple)}


def test_personal_config_files_are_stat_in_the_data_root(monkeypatch, tmp_path):
    """accounts/budget.yaml live under the data root, not the project config dir.

    Statting them under CONFIG_DIR found nothing, so editing the real files never
    busted the cube cache. The two dirs collapse to one under
    SPEND_ANALYZER_CONFIG_DIR, so this test must point them somewhere different.
    """
    project, personal = tmp_path / "project", tmp_path / "personal"
    project.mkdir()
    personal.mkdir()
    (project / "app.yaml").write_text("archive_paths: []\n")
    (personal / "accounts.yaml").write_text("acc_1: {person: A}\n")
    (personal / "budget.yaml").write_text("goals: {}\n")
    monkeypatch.setattr(app, "CONFIG_DIR", project)
    monkeypatch.setattr(app, "PERSONAL_CONFIG_DIR", personal)
    cfg = SimpleNamespace(resolved_archive_paths=[])

    before = app._config_signature(cfg)
    assert _sig_names(before) == {"app.yaml", "accounts.yaml", "budget.yaml",
                                  "manual_edits.jsonl"}

    (personal / "accounts.yaml").write_text("acc_1: {person: A, include: false}\n")
    assert app._config_signature(cfg) != before, \
        "editing the personal accounts.yaml must bust the cube cache"
