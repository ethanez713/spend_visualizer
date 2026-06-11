"""Shared guard: the live archive, intent log, and corrections queue must never
be mutated by a test. Both the AppTest UI suite and the browser e2e suite hash
these files before/after every test and fail loudly on any change."""
from __future__ import annotations

import hashlib
from pathlib import Path

import corrections as corr
from config_io import load_app_config

_cfg = load_app_config()
LIVE_FILES = [Path(p) for p in _cfg.resolved_archive_paths] + [
    Path(_cfg.resolved_transformer_root) / "data" / "manual_edits.jsonl",
    corr.STORE,  # captured at import time, before any redirection
]


def digest_all() -> dict[str, str | None]:
    return {str(p): _digest(p) for p in LIVE_FILES}


def _digest(p: Path) -> str | None:
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.exists() else None
