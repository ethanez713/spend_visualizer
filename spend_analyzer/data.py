"""Assemble the cube from configs: ingest -> taxonomy -> enrich -> Cube + QC.

Pure (no Streamlit) so it is unit-testable; app.py wraps build_cube in
st.cache_data keyed on archive + taxonomy file stats (PLAN.md §11).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from config_io import AppConfig, load_accounts, load_app_config, CONFIG_DIR
from cube import Cube
from enrich import enrich
from ingest import ingest
from taxonomy import load_taxonomy, Taxonomy


@dataclass
class BuildResult:
    cube: Cube
    df: pd.DataFrame
    qc: dict = field(default_factory=dict)


def _qc(df: pd.DataFrame, taxonomy: Taxonomy, ingest_qc: dict) -> dict:
    qc = dict(ingest_qc)
    if df.empty:
        return qc
    spend_total = float(df["spend"].sum())
    excl = df[df["flow"] == "excluded"]
    other = df[(df["flow"] == "spend") & (df["tier1"] == "Other")]
    unmapped_atoms = sorted(set(df.loc[df["unmapped"], "atom"]))

    qc.update(
        {
            "spend_total": spend_total,
            "income_total": float(df["income"].sum()),
            "net_total": float(df["income"].sum() - spend_total),
            "n_excluded": int(len(excl)),
            "excluded_sum": float(excl["abs_amount"].sum()),
            "unmapped_atoms": unmapped_atoms,
            "n_unmapped_rows": int(df["unmapped"].sum()),
            "pct_spend_other": (float(other["spend"].sum()) / spend_total * 100.0)
            if spend_total else 0.0,
            "n_recurring_merchants": int(
                df.loc[df["recurrence"] == "recurring", "merchant_id"].nunique()
            ),
        }
    )
    return qc


def build_cube(
    app: AppConfig | None = None,
    taxonomy_path: str | Path = CONFIG_DIR / "taxonomy.yaml",
    accounts_path: str | Path = CONFIG_DIR / "accounts.yaml",
) -> BuildResult:
    app = app or load_app_config()
    taxonomy = load_taxonomy(taxonomy_path)
    accounts = load_accounts(accounts_path)

    res = ingest(app.resolved_archive_paths)
    df = enrich(res.transactions, taxonomy, accounts)
    qc = _qc(df, taxonomy, res.qc)
    return BuildResult(cube=Cube(df), df=df, qc=qc)
