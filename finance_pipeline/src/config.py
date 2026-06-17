"""Where the components live — the one file to touch if a component moves.

Everything is resolved relative to the monorepo root by ``default_config()``;
tests build their own ``Config`` pointing at fake components in a tmp dir.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    # Component repo roots.
    transactions_dir: Path       # Plaid collector (fetch + durable persist)
    transformer_dir: Path        # category auditor/corrector
    analyzer_dir: Path           # Streamlit UI

    # External data root (the private finance_data repo): the pipeline-level lock
    # lives there, and --push-data commits/pushes it as a git repo.
    data_root: Path

    # Each component runs under ITS OWN venv interpreter, from its own repo root.
    transactions_python: Path
    transformer_python: Path
    analyzer_streamlit: Path

    # Drive credentials: each Drive-pushing repo owns its own .secrets (persister is a
    # pure library). transactions/.secrets is the original (OAuth'd) set; the
    # transformer keeps its own copy, seeded from there on first run.
    transactions_secrets: Path
    transformer_secrets: Path

    # OPTIONAL external "converter" project: translates the categorized store into
    # the established budget's categories (a ledger CSV the analyzer's Budget tab
    # reads). All three are None unless a converter is configured (see
    # `_converter_dir`), in which case the pipeline regenerates the ledger after
    # categorize. Kept out of this repo so it stays generic.
    converter_dir: Path | None = None
    converter_python: Path | None = None
    budget_ledger_csv: Path | None = None

    ui_port: int = 8501
    ui_start_timeout_s: float = 120.0   # streamlit cold start can be slow
    ollama_port: int = 11434            # local LLM; probed (warn-only) in preflight


def _data_root(monorepo_root: Path) -> Path:
    """The external data root (same resolution as every component, kept
    deliberately duplicated — see the root CLAUDE.md): $SPEND_VISUALIZER_DATA,
    else the monorepo-root ``data_root`` file, else ~/finance_data."""
    env = os.environ.get("SPEND_VISUALIZER_DATA")
    if env:
        return Path(env).expanduser()
    cfg = monorepo_root / "data_root"
    if cfg.is_file():
        for line in cfg.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return Path(line).expanduser()
    return Path("~/finance_data").expanduser()


def _converter_dir(data_root: Path) -> Path | None:
    """OPTIONAL: where the external "converter" project lives, or None.

    Stdlib-only (this orchestrator pulls in no third-party deps), so the pointer
    is a plain value, mirroring the ``data_root`` file convention rather than
    living in the analyzer's YAML: ``$SPEND_VISUALIZER_CONVERTER``, else the first
    non-comment line of ``<data_root>/converter_root``, else None (no converter →
    the ledger-regeneration step is simply skipped). Kept out of this repo so it
    stays generic; both pointers live in the private data root."""
    env = os.environ.get("SPEND_VISUALIZER_CONVERTER")
    if env:
        return Path(env).expanduser()
    cfg = data_root / "converter_root"
    if cfg.is_file():
        for line in cfg.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return Path(line).expanduser()
    return None


def default_config() -> Config:
    # Monorepo root: this file lives at <root>/finance_pipeline/src/config.py.
    root = Path(__file__).resolve().parents[2]
    transactions = root / "transactions"
    transformer = root / "plaid_category_transformer"
    analyzer = root / "spend_analyzer"
    data_root = _data_root(root)

    converter = _converter_dir(data_root)
    converter_python = converter / ".venv" / "bin" / "python" if converter else None
    # The ledger the analyzer's Budget tab reads — under the data root (so
    # --push-data versions it) and matching budget.yaml's default `ledger.csv`.
    budget_ledger_csv = (data_root / "spend_analyzer" / "data" / "budget_ledger.csv"
                         if converter else None)

    return Config(
        transactions_dir=transactions,
        transformer_dir=transformer,
        analyzer_dir=analyzer,
        data_root=data_root,
        transactions_python=transactions / "venv" / "bin" / "python",
        transformer_python=transformer / ".venv" / "bin" / "python",
        analyzer_streamlit=analyzer / "venv" / "bin" / "streamlit",
        transactions_secrets=transactions / ".secrets",
        transformer_secrets=transformer / ".secrets",
        converter_dir=converter,
        converter_python=converter_python,
        budget_ledger_csv=budget_ledger_csv,
    )
