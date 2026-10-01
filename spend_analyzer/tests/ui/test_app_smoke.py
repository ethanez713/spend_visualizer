"""Boot + cross-view consistency: every view renders, and the views agree.

Only the active view runs, so each one is opened in turn over the real archive —
the class of bug previously only caught by opening the app by hand.
"""
import pytest
from streamlit.testing.v1 import AppTest

from tests.ui._harness import APP, VIEWS, go, metric, metrics, money_to_f



@pytest.mark.parametrize("view", VIEWS)
def test_given_real_archive_when_view_opened_then_it_renders_without_exception(at, view):
    go(at, view)
    assert at.radio(key="view").value == view


def test_given_default_filters_when_booted_then_totals_tie_out_across_views(at):
    # Drilldown's visible Total must equal "Total spend" everywhere it appears
    # (Cash flow) — the UI-level face of the no-double-count invariant.
    total = money_to_f(metric(at, "Total"))
    go(at, "Cash flow")
    spends = [money_to_f(v) for v in metrics(at, "Total spend")]
    assert spends and all(abs(s - total) <= 1 for s in spends), (total, spends)
    income = money_to_f(metric(at, "Total income"))
    net = money_to_f(metric(at, "Net savings"))
    assert abs((income - total) - net) <= 1, (income, total, net)


def test_given_missing_archive_when_app_boots_then_friendly_error_not_crash(monkeypatch, tmp_path):
    import config_io
    cfg = config_io.AppConfig(archive_paths=[str(tmp_path / "missing.jsonl")])
    monkeypatch.setattr(config_io, "load_app_config", lambda *a, **k: cfg)
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert "Archive not found" in at.error[0].value
