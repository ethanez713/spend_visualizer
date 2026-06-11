"""Boot + cross-view consistency: the whole script renders, and the views agree.

Streamlit reruns the entire script on every interaction, so "the app boots with
no exception" already exercises every tab's render path over the real archive —
the class of bug previously only caught by opening the app by hand.
"""
from streamlit.testing.v1 import AppTest

from tests.ui._harness import APP, metric, metrics, money_to_f

_TAB_LABELS = {"🧭 Drilldown", "💰 Budget", "🏪 Merchants & recurring",
               "💵 Cash flow", "✏️ Corrections", "✅ QC"}


def test_given_real_archive_when_app_boots_then_all_tabs_render_without_exception(at):
    assert _TAB_LABELS <= {t.label for t in at.tabs}


def test_given_default_filters_when_booted_then_totals_tie_out_across_views(at):
    # Drilldown's visible Total must equal "Total spend" everywhere it appears
    # (Cash flow, QC) — the UI-level face of the no-double-count invariant.
    total = money_to_f(metric(at, "Total"))
    spends = [money_to_f(v) for v in metrics(at, "Total spend")]
    assert spends, "no 'Total spend' metric rendered"
    assert all(abs(s - total) <= 1 for s in spends), (total, spends)
    income = money_to_f(metric(at, "Total income"))
    net = money_to_f(metric(at, "Net"))
    assert abs((income - total) - net) <= 1, (income, total, net)


def test_given_missing_archive_when_app_boots_then_friendly_error_not_crash(monkeypatch, tmp_path):
    import config_io
    cfg = config_io.AppConfig(archive_paths=[str(tmp_path / "missing.jsonl")])
    monkeypatch.setattr(config_io, "load_app_config", lambda *a, **k: cfg)
    at = AppTest.from_file(APP, default_timeout=60)
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert "Archive not found" in at.error[0].value
