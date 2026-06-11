"""Data-root resolution: where ALL personal financial data lives (never in-repo).

Every component of the monorepo resolves the same external data root (it mirrors
the monorepo layout inside, e.g. ``<root>/plaid_category_transformer/data/…``).
Priority: $SPEND_VISUALIZER_DATA, else the first non-comment line of the
monorepo-root ``data_root`` file, else ``~/finance_data``.

Secrets (.secrets/) deliberately do NOT move with the data: they are machine-local
credentials/state, never committed anywhere.
"""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MONOREPO_ROOT = PROJECT_ROOT.parent


def data_root() -> Path:
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
# This component's slice of the data root (categorized store, worklist, manual edits).
DATA_DIR = DATA_ROOT / "plaid_category_transformer" / "data"
