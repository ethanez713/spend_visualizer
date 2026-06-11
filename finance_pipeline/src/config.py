"""Where the components live — the one file to touch if a component moves.

Everything is resolved relative to the monorepo root by ``default_config()``;
tests build their own ``Config`` pointing at fake components in a tmp dir.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Config:
    # Component repo roots.
    transactions_dir: Path       # Plaid collector (fetch + durable persist)
    transformer_dir: Path        # category auditor/corrector
    analyzer_dir: Path           # Streamlit UI

    # Each component runs under ITS OWN venv interpreter, from its own repo root.
    transactions_python: Path
    transformer_python: Path
    analyzer_streamlit: Path

    # Drive credentials: each Drive-pushing repo owns its own .secrets (persister is a
    # pure library). transactions/.secrets is the original (OAuth'd) set; the
    # transformer keeps its own copy, seeded from there on first run.
    transactions_secrets: Path
    transformer_secrets: Path

    ui_port: int = 8501
    ui_start_timeout_s: float = 120.0   # streamlit cold start can be slow
    ollama_port: int = 11434            # local LLM; probed (warn-only) in preflight


def default_config() -> Config:
    # Monorepo root: this file lives at <root>/finance_pipeline/src/config.py.
    root = Path(__file__).resolve().parents[2]
    transactions = root / "transactions"
    transformer = root / "plaid_category_transformer"
    analyzer = root / "spend_analyzer"
    return Config(
        transactions_dir=transactions,
        transformer_dir=transformer,
        analyzer_dir=analyzer,
        transactions_python=transactions / "venv" / "bin" / "python",
        transformer_python=transformer / ".venv" / "bin" / "python",
        analyzer_streamlit=analyzer / "venv" / "bin" / "streamlit",
        transactions_secrets=transactions / ".secrets",
        transformer_secrets=transformer / ".secrets",
    )
