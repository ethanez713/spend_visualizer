"""The budget's category model: the analyzer's tier-1 categories folded into the
categories a household budgets by.

Each spend row gets one ``budget_cat`` (``None`` when the row is not budget spending), by
the first rule that applies: a tier-1 category in ``drop``; a pinned merchant phrase; an
atom exception; the tier-1 name after ``fold``. Pure functions over the enriched frame,
so the Budget view and the tests share one definition.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import yaml

from config_io import CONFIG_DIR, PERSONAL_CONFIG_DIR


@dataclass(frozen=True)
class BudgetModel:
    fold: dict[str, str] = field(default_factory=dict)
    atoms: dict[str, str] = field(default_factory=dict)
    drop: frozenset[str] = frozenset()
    pinned: tuple[tuple[str, tuple[str, ...]], ...] = ()

    def assign(self, tier1: str, atom: str, text: str) -> str | None:
        if tier1 in self.drop:
            return None
        lowered = (text or "").lower()
        for category, phrases in self.pinned:
            if any(_has_phrase(lowered, p) for p in phrases):
                return category
        if atom in self.atoms:
            return self.atoms[atom]
        return self.fold.get(tier1, tier1)


def _has_phrase(text: str, phrase: str) -> bool:
    return re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", text) is not None


def _read(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def _pinned(entries) -> tuple[tuple[str, tuple[str, ...]], ...]:
    rules = []
    for entry in entries or []:
        if not isinstance(entry, dict) or not entry.get("category"):
            continue
        phrases = tuple(str(p).lower() for p in entry.get("match") or [] if str(p).strip())
        if phrases:
            rules.append((str(entry["category"]), phrases))
    return tuple(rules)


def load_budget_model(generic: Path = CONFIG_DIR / "budget_model.yaml",
                      personal: Path = PERSONAL_CONFIG_DIR / "budget.yaml") -> BudgetModel:
    """The repo's generic mapping, with the personal budget.yaml's ``fold``/``atoms``
    entries layered over it and its ordered ``pinned`` rules added."""
    base, mine = _read(generic), _read(personal)
    return BudgetModel(
        fold={**(base.get("fold") or {}), **(mine.get("fold") or {})},
        atoms={**(base.get("atoms") or {}), **(mine.get("atoms") or {})},
        drop=frozenset(base.get("drop") or []) | frozenset(mine.get("drop") or []),
        pinned=_pinned(mine.get("pinned")))


def annotate(df: pd.DataFrame, model: BudgetModel) -> pd.DataFrame:
    """``df`` plus ``budget_cat``: set on spend rows, ``None`` everywhere else."""
    if df.empty:
        return df
    out = df.copy()
    spend = out["flow"] == "spend"
    out["budget_cat"] = pd.Series([None] * len(out), index=out.index, dtype="object")
    out.loc[spend, "budget_cat"] = [
        model.assign(t, a, d) for t, a, d in
        zip(out.loc[spend, "tier1"], out.loc[spend, "atom"], out.loc[spend, "description"])]
    return out
