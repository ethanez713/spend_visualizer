"""The budget's category model: the analyzer's tier-1 categories folded into the
categories a household budgets by.

Each spend row gets one ``budget_cat`` (``None`` when the row is not budget spending), by
the first rule that applies: an atom matching ``drop_atoms``; a pinned merchant phrase; an
atom exception; the tier-1 name after ``fold``. A row outside spending counts only when its
atom is in ``transfer_categories`` (money from a payment app the household treats as a
budget credit). ``decide_atoms`` names the rows outside the budget that still need a human
decision in review. Pure functions over the enriched frame, so the Budget view and the
tests share one definition.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path

import pandas as pd
import yaml

from config_io import CONFIG_DIR, PERSONAL_CONFIG_DIR


@dataclass(frozen=True)
class BudgetModel:
    fold: dict[str, str] = field(default_factory=dict)
    atoms: dict[str, str] = field(default_factory=dict)
    drop_atoms: tuple[str, ...] = ()
    pinned: tuple[tuple[str, tuple[str, ...]], ...] = ()
    transfer_categories: dict[str, str] = field(default_factory=dict)
    decide_atoms: tuple[str, ...] = ()

    def assign(self, tier1: str, atom: str, text: str) -> str | None:
        return self.decide(tier1, atom, text)[0]

    def decide(self, tier1: str, atom: str, text: str) -> tuple[str | None, str]:
        """A spend row's budget category and the rule that chose it: ``pinned:<phrase>``,
        ``atom``, ``fold``, or ``""`` when the atom is dropped."""
        if any(fnmatchcase(atom, pattern) for pattern in self.drop_atoms):
            return None, ""
        lowered = (text or "").lower()
        for category, phrases in self.pinned:
            hit = next((p for p in phrases if _has_phrase(lowered, p)), None)
            if hit:
                return category, f"pinned:{hit}"
        if atom in self.atoms:
            return self.atoms[atom], "atom"
        return self.fold.get(tier1, tier1), "fold"

    def needs_decision(self, atom: str) -> bool:
        return any(fnmatchcase(atom, pattern) for pattern in self.decide_atoms)


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
        drop_atoms=tuple(base.get("drop_atoms") or []) + tuple(mine.get("drop_atoms") or []),
        pinned=_pinned(mine.get("pinned")),
        transfer_categories={**_str_map(base.get("transfer_categories")),
                             **_str_map(mine.get("transfer_categories"))},
        decide_atoms=tuple(base.get("decide_atoms") or []) + tuple(mine.get("decide_atoms") or []))


def _str_map(entries) -> dict[str, str]:
    if not isinstance(entries, dict):
        return {}
    return {str(k): str(v) for k, v in entries.items() if str(v).strip()}


def annotate(df: pd.DataFrame, model: BudgetModel, overrides: dict[str, dict] | None = None) -> pd.DataFrame:
    """``df`` plus the budget columns: ``budget_cat`` (``None`` when the row is not budget
    spending), ``budget_amount`` (its signed amount, 0 when not counted), ``budget_note``,
    ``budget_edit_id`` and ``budget_source`` (what chose the category: ``edit``,
    ``transfer`` or a ``BudgetModel.decide`` source).

    ``overrides`` are the household's budget edits by transaction id (see
    ``budget_edits.overrides_for``); an edit that sets a category or excludes the row beats
    the model, and can count a row the model would not (a transfer someone paid back).
    """
    if df.empty:
        return df
    overrides = overrides or {}
    out = df.copy()
    categories, notes, edit_ids, sources = [], [], [], []
    for tid, flow, tier1, atom, text in zip(out["transaction_id"], out["flow"], out["tier1"],
                                            out["atom"], out["description"]):
        override = overrides.get(tid)
        if override and override["edit"]:
            category, source = override["category"], "edit"
        elif flow == "spend":
            category, source = model.decide(tier1, atom, text)
        else:
            category = model.transfer_categories.get(atom)
            source = "transfer" if category else ""
        categories.append(category)
        sources.append(source)
        notes.append(override["note"] if override else "")
        edit_ids.append(override["edit"] if override else "")
    out["budget_cat"] = pd.Series(categories, index=out.index, dtype="object")
    out["budget_amount"] = out["amount"].where(out["budget_cat"].notna(), 0.0)
    out["budget_note"] = notes
    out["budget_edit_id"] = edit_ids
    out["budget_source"] = sources
    return out
