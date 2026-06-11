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


@dataclass
class Budget:
    period: str = "monthly"
    goals: dict[str, float] = field(default_factory=dict)

    def goal(self, tier1: str) -> float | None:
        return self.goals.get(tier1)


def load_budget(path: str | Path = PERSONAL_CONFIG_DIR / "budget.yaml") -> Budget:
    """Return monthly tier-1 goals (PLAN.md §14). Empty if the file is absent."""
    p = Path(path)
    if not p.exists():
        return Budget()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    goals = {k: float(v) for k, v in (data.get("goals") or {}).items()}
    return Budget(period=data.get("period", "monthly"), goals=goals)
