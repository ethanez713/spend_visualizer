"""Reshaping a category x month spend pivot into what the Budget tab shows.

Pure functions over the pivot (index = budget category, columns = ``YYYY-MM``), kept out
of the Streamlit view so each rule is testable on its own.
"""
from __future__ import annotations

import pandas as pd

from config_io import Offset, Project


def apply_offsets(pivot: pd.DataFrame, offsets: list[Offset]) -> tuple[pd.DataFrame, list[str]]:
    """Spread each offset's source row across its target categories. Returns the new
    pivot and the sources that had no row to spread."""
    out, missing = pivot.copy(), []
    for offset in offsets:
        if offset.source not in out.index:
            missing.append(offset.source)
            continue
        source = out.loc[offset.source].copy()
        for target, weight in offset.spread.items():
            share = source * weight
            out.loc[target] = out.loc[target].add(share, fill_value=0.0) if target in out.index else share
        remainder = 1.0 - sum(offset.spread.values())
        if abs(remainder) < 1e-9:
            out = out.drop(index=offset.source)
        else:
            out.loc[offset.source] = source * remainder
    return out, missing


def split_projects(pivot: pd.DataFrame, projects: list[Project]) -> tuple[pd.DataFrame, pd.DataFrame]:
    """The pivot without project rows, and one row per project (spent, earmark, left)."""
    names = [p.name for p in projects]
    rest = pivot.drop(index=[n for n in names if n in pivot.index])
    rows = []
    for project in projects:
        spent = float(pivot.loc[project.name].sum()) if project.name in pivot.index else 0.0
        left = None if project.earmark is None else project.earmark - spent
        used = None if not project.earmark else spent / project.earmark * 100.0
        rows.append({"Project": project.name, "Spent": spent,
                     "Earmark": project.earmark if project.earmark is not None else float("nan"),
                     "Left": left if left is not None else float("nan"),
                     "% used": used if used is not None else float("nan")})
    return rest, pd.DataFrame(rows, columns=["Project", "Spent", "Earmark", "Left", "% used"])


def monthly_grid(pivot: pd.DataFrame, months: list[str]) -> pd.DataFrame:
    """Category x month spend with each category's monthly average over ``months``, that
    average annualized, and a Total row."""
    if pivot.empty:
        return pd.DataFrame()
    grid = pivot.reindex(sorted(pivot.columns), axis=1)
    grid["Avg"] = pivot.reindex(columns=months, fill_value=0.0).mean(axis=1) if months else 0.0
    grid["Annualized"] = grid["Avg"] * 12
    grid.loc["Total"] = grid.sum()
    return grid
