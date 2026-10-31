"""Boot + cross-view consistency: every view renders, and the views agree.

Only the active view runs, so each one is opened in turn over the real archive —
the class of bug previously only caught by opening the app by hand.
"""
import pytest
from streamlit.testing.v1 import AppTest

from tests.ui._harness import widget, APP, VIEWS, go, metric, metrics, money_to_f



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


def test_given_drilldown_and_budget_when_opened_then_averages_name_their_period(boot_app):
    import re
    drill = boot_app("Drilldown")
    assert any(re.fullmatch(r"Avg/mo, [A-Z][a-z]{2}.*–[A-Z][a-z]{2}.*", str(c))
               for d in drill.dataframe for c in d.value.columns)
    budget = boot_app("Budget")
    assert any(re.fullmatch(r"\d{4} avg", str(c)) for d in budget.dataframe for c in d.value.columns)


def test_given_two_annualized_figures_when_opened_then_each_says_what_it_measures(boot_app):
    drill = boot_app("Drilldown")
    assert any("Every spending category" in (m.proto.help or "") for m in drill.metric)
    budget = boot_app("Budget")
    assert any("Budget categories only" in c.value for c in budget.caption)


def test_given_a_month_in_progress_when_cash_flow_opens_then_it_is_labelled_partial(boot_app):
    at = boot_app("Cash flow")
    assert any("Lighter bars cover part of a month" in c.value for c in at.caption)
    assert any("(to " in str(label) for d in at.dataframe for label in d.value.index)
    for timeframe in ("Quarter", "Year"):
        widget(at.radio, key="cash_timeframe").set_value(timeframe).run()
        assert not at.exception
        assert any(" to " in str(label) or "(to " in str(label)
                   for d in at.dataframe for label in d.value.index)
