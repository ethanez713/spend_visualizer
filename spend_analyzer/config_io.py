"""Tiny helpers for loading the human-owned YAML config (PLAN.md §9)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
MONOREPO_ROOT = ROOT.parent


def data_root() -> Path:
    """Where ALL personal financial data lives — never inside this repo.

    Priority: $SPEND_VISUALIZER_DATA, else the first non-comment line of the
    monorepo-root ``data_root`` file, else ``~/finance_data``. The directory
    mirrors the monorepo layout.
    """
    env = os.environ.get("SPEND_VISUALIZER_DATA")
    if env:
        return Path(env).expanduser()
    cfg = MONOREPO_ROOT / "data_root"
    if cfg.is_file():
        for line in cfg.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return Path(line).expanduser()
    return Path("~/finance_data").expanduser()


DATA_ROOT = data_root()

# Test hook: the e2e suite runs the app in a subprocess pointed at a throwaway
# config dir, so browser-driven tests can never read/write the live config. When
# set, it covers BOTH config families below.
_ENV_CONFIG_DIR = os.environ.get("SPEND_ANALYZER_CONFIG_DIR")
# Project config (shareable, committed): app.yaml + taxonomy.yaml.
CONFIG_DIR = Path(_ENV_CONFIG_DIR) if _ENV_CONFIG_DIR else ROOT / "config"
# Personal config (data, never committed here): accounts.yaml + budget.yaml.
PERSONAL_CONFIG_DIR = (Path(_ENV_CONFIG_DIR) if _ENV_CONFIG_DIR
                       else DATA_ROOT / "spend_analyzer" / "config")


@dataclass
class AppConfig:
    archive_paths: list[str] = field(default_factory=list)
    trailing_avg_months: int = 3
    home_metro: str | None = None
    transformer_root: str = "../plaid_category_transformer"

    @property
    def resolved_archive_paths(self) -> list[str]:
        # Relative paths resolve from the DATA ROOT (all data lives there),
        # so app.yaml stays valid no matter where the data root points.
        out = []
        for p in self.archive_paths:
            pp = Path(p).expanduser()
            if not pp.is_absolute():
                pp = (DATA_ROOT / p).resolve()
            out.append(str(pp))
        return out

    @property
    def resolved_transformer_root(self) -> str:
        p = Path(self.transformer_root)
        if not p.is_absolute():
            p = (ROOT / self.transformer_root).resolve()
        return str(p)


def load_app_config(path: str | Path = CONFIG_DIR / "app.yaml") -> AppConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    paths = data.get("archive_paths")
    if not paths:
        single = data.get("archive_path")
        paths = [single] if single else []
    return AppConfig(
        archive_paths=paths,
        trailing_avg_months=int(data.get("trailing_avg_months", 3)),
        home_metro=data.get("home_metro"),
        transformer_root=data.get("transformer_root", "../plaid_category_transformer"),
    )


def load_accounts(path: str | Path = PERSONAL_CONFIG_DIR / "accounts.yaml") -> dict[str, dict]:
    """Return {account_id: {person, name, type, subtype, institution, include}}."""
    p = Path(path)
    if not p.exists():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return data.get("accounts") or {}


@dataclass(frozen=True)
class Offset:
    """Money in under ``source`` that nets against spending: each ``spread`` category takes
    its weight of the source's total (what is left stays under ``source``)."""
    source: str
    spread: dict[str, float]


@dataclass(frozen=True)
class Project:
    """A one-off spending bucket tracked against an earmark, outside the monthly budget."""
    name: str
    earmark: float | None = None


COMPLETE_MONTHS, MONTHS_WITH_DATA = "complete_months", "months_with_data"


@dataclass
class Budget:
    period: str = "monthly"
    goals: dict[str, float] = field(default_factory=dict)
    # Optional: path to an EXTERNAL pre-categorized ledger CSV whose `Category`
    # column already matches these goal keys (the converter project's output).
    # When set, the Budget tab offers it next to the built-in budget model.
    # Relative paths resolve from the data root; None ⇒ the budget model only.
    ledger_csv: str | None = None
    # A goal given as `{base: N}` grows by `goal_inflation` a year from `goal_base_year`.
    indexed_goals: dict[str, float] = field(default_factory=dict)
    goal_base_year: int | None = None
    goal_inflation: float = 0.0
    offsets: list[Offset] = field(default_factory=list)
    projects: list[Project] = field(default_factory=list)
    averaging: str = COMPLETE_MONTHS

    def goal(self, category: str, year: int | None = None) -> float | None:
        if category in self.indexed_goals:
            if year is None or self.goal_base_year is None:
                return self.indexed_goals[category]
            return self.indexed_goals[category] * (1 + self.goal_inflation) ** (year - self.goal_base_year)
        return self.goals.get(category)

    def goal_categories(self) -> list[str]:
        return list(self.goals) + [c for c in self.indexed_goals if c not in self.goals]

    @property
    def resolved_ledger_csv(self) -> str | None:
        if not self.ledger_csv:
            return None
        p = Path(self.ledger_csv).expanduser()
        if not p.is_absolute():
            p = (DATA_ROOT / self.ledger_csv).resolve()
        return str(p)


def _number(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def load_budget(path: str | Path = PERSONAL_CONFIG_DIR / "budget.yaml") -> Budget:
    """Return monthly goals and the Budget tab's settings (PLAN.md §14). Empty if the
    file is absent.

    A goal is a number, or ``{base: N}`` to index it by ``goal_index``. An optional
    ``ledger.csv`` key offers an externally categorized ledger next to the budget model
    (see ``Budget.ledger_csv``). Malformed entries are skipped, never fatal.
    """
    p = Path(path)
    if not p.exists():
        return Budget()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    goals, indexed = {}, {}
    for category, goal in (data.get("goals") or {}).items():
        if isinstance(goal, dict) and _number(goal.get("base")) is not None:
            indexed[category] = float(goal["base"])
        elif _number(goal) is not None:
            goals[category] = float(goal)
    index = data.get("goal_index") or {}
    offsets = [Offset(str(o["source"]), {str(k): float(v) for k, v in (o.get("spread") or {}).items()
                                         if _number(v) is not None})
               for o in data.get("offsets") or [] if isinstance(o, dict) and o.get("source")]
    projects = [Project(str(j["name"]), _number(j.get("earmark")))
                for j in data.get("projects") or [] if isinstance(j, dict) and j.get("name")]
    averaging = data.get("averaging")
    return Budget(period=data.get("period", "monthly"), goals=goals,
                  ledger_csv=(data.get("ledger") or {}).get("csv"),
                  indexed_goals=indexed,
                  goal_base_year=int(index["base_year"]) if _number(index.get("base_year")) is not None else None,
                  goal_inflation=_number(index.get("inflation")) or 0.0,
                  offsets=[o for o in offsets if o.spread], projects=projects,
                  averaging=averaging if averaging in (COMPLETE_MONTHS, MONTHS_WITH_DATA)
                  else COMPLETE_MONTHS)
