"""Tiny helpers for loading the human-owned YAML config (PLAN.md §9)."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_DIR = ROOT / "config"


@dataclass
class AppConfig:
    archive_paths: list[str] = field(default_factory=list)
    trailing_avg_months: int = 3
    home_metro: str | None = None

    @property
    def resolved_archive_paths(self) -> list[str]:
        out = []
        for p in self.archive_paths:
            pp = Path(p)
            if not pp.is_absolute():
                pp = (ROOT / p).resolve()
            out.append(str(pp))
        return out


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
    )


def load_accounts(path: str | Path = CONFIG_DIR / "accounts.yaml") -> dict[str, dict]:
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


def load_budget(path: str | Path = CONFIG_DIR / "budget.yaml") -> Budget:
    """Return monthly tier-1 goals (PLAN.md §14). Empty if the file is absent."""
    p = Path(path)
    if not p.exists():
        return Budget()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    goals = {k: float(v) for k, v in (data.get("goals") or {}).items()}
    return Budget(period=data.get("period", "monthly"), goals=goals)
