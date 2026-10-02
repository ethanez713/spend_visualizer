"""The Budget tab with the optional settings configured: offsets, projects, indexed goals,
and the other averaging mode. The config is injected; the archive is the real one, read-only."""
import config_io
from config_io import COMPLETE_MONTHS, MONTHS_WITH_DATA, Budget, Offset, Project

from tests.ui._harness import widget


def _configured(monkeypatch):
    budget = Budget(goals={"Mortgage": 4500.0}, indexed_goals={"Dining Out": 1100.0},
                    goal_base_year=2024, goal_inflation=0.03,
                    offsets=[Offset("Venmo - Income", {"Dining Out": 0.5, "Travel": 0.5})],
                    projects=[Project("Remodel", 18000.0)], averaging=MONTHS_WITH_DATA)
    monkeypatch.setattr(config_io, "load_budget", lambda *a, **k: budget)


def test_given_offset_without_rows_when_budget_opens_then_a_note_names_it(monkeypatch, boot_app):
    _configured(monkeypatch)

    at = boot_app("Budget")

    assert any("Venmo - Income" in info.value for info in at.info)


def test_given_projects_configured_when_budget_opens_then_the_panel_and_grid_render(
        monkeypatch, boot_app):
    _configured(monkeypatch)

    at = boot_app("Budget")

    assert "Projects" in [h.value for h in at.subheader]
    assert "Monthly grid" in [e.label for e in at.expander]


def test_given_months_with_data_configured_when_budget_opens_then_that_mode_is_selected(
        monkeypatch, boot_app):
    _configured(monkeypatch)

    at = boot_app("Budget")

    assert widget(at.radio, key="budget_averaging").value == MONTHS_WITH_DATA
    assert any("months that have spending" in c.value for c in at.caption)


def test_given_default_config_when_budget_opens_then_complete_months_is_selected(boot_app):
    at = boot_app("Budget")

    assert widget(at.radio, key="budget_averaging").value == COMPLETE_MONTHS


def _imported_history(monkeypatch, tmp_path):
    import json

    import legacy
    ledger, goals = tmp_path / "legacy_ledger.csv", tmp_path / "legacy_goals.json"
    ledger.write_text(
        "Source,Date,Description,Category,Debit,Credit,Debit Less Credit,Account,Note\n"
        "Ethan,01/05/2026,Dress,Wedding,$3000.00,,$3000.00,,\n"
        "Ethan,02/05/2026,Venue,Wedding,$3000.00,,$3000.00,,\n"
        "Ethan,11/05/2025,Deposit,Wedding,$2800.00,,$2800.00,,\n", encoding="utf-8")
    goals.write_text(json.dumps({"2025": {"Wedding": 2900}}))
    monkeypatch.setattr(legacy, "LEDGER", ledger)
    monkeypatch.setattr(legacy, "GOALS", goals)


def _budget_table(at):
    return next(df.value for df in at.dataframe if "% of Goal" in df.value.columns)


def _model_source(boot_app):
    at = boot_app("Budget")
    widget(at.radio, key="budget_source").set_value("Budget model")
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def test_given_imported_history_when_budget_opens_then_the_note_and_history_panel_appear(
        monkeypatch, tmp_path, boot_app):
    _imported_history(monkeypatch, tmp_path)

    at = _model_source(boot_app)

    assert any("imported sheet history" in c.value for c in at.caption)
    assert "History by year" in [e.label for e in at.expander]


def test_given_imported_months_when_default_period_reaches_back_then_they_count_in_the_table(
        monkeypatch, tmp_path, boot_app):
    _imported_history(monkeypatch, tmp_path)

    at = _model_source(boot_app)

    assert "Wedding" in _budget_table(at).index


def test_given_the_converter_ledger_selected_when_history_is_imported_then_it_is_not_mixed_in(
        monkeypatch, tmp_path, boot_app):
    _imported_history(monkeypatch, tmp_path)

    at = boot_app("Budget")

    assert "Wedding" not in _budget_table(at).index
