"""Stage op 1: load configured source files, decompress, parse.

Read-only against the archive. Supports plain ``.jsonl`` and xz-compressed
``.jsonl.xz``. Parses defensively: a malformed line is skipped (fail-soft per
the security baseline), not fatal.
"""
from __future__ import annotations

import json
import lzma
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class SourceStat:
    path: str
    mtime: float
    size: int

    @property
    def cache_key(self) -> tuple[str, float, int]:
        return (self.path, self.mtime, self.size)


def stat_source(path: str | Path) -> SourceStat:
    p = Path(path)
    st = p.stat()
    return SourceStat(path=str(p.resolve()), mtime=st.st_mtime, size=st.st_size)


def _open_text(path: Path):
    if path.suffix == ".xz" or path.name.endswith(".jsonl.xz"):
        return lzma.open(path, "rt", encoding="utf-8")
    return open(path, "rt", encoding="utf-8")


def load_file(path: str | Path) -> list[dict]:
    """Parse one JSONL(.xz) archive into a list of raw Plaid txn dicts."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"archive not found: {p}")
    rows: list[dict] = []
    with _open_text(p) as fh:
        for lineno, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # fail-soft: skip a corrupt line rather than abort the load
                continue
            if isinstance(obj, dict) and obj.get("transaction_id"):
                rows.append(obj)
    return rows


def load_sources(paths: Iterable[str | Path]) -> list[dict]:
    """Load and concatenate every configured source file."""
    out: list[dict] = []
    for path in paths:
        out.extend(load_file(path))
    return out
