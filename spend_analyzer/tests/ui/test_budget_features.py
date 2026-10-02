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
