"""JSONL store primitives: load / save (atomic) / derive CSV / dedupe.

The canonical store is **lossless, uncompressed JSONL** — one full record per line,
keyed by a configurable ``key_field`` (default ``transaction_id``). Uncompressed so
git produces clean line-level diffs (audit history). A flat CSV is *derived* from it
for human / Sheets viewing.

This module is **generic** — it knows nothing about Plaid. Records are plain dicts;
the caller supplies a ``row_fn`` to project them for CSV.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

from .csv_safe import csv_safe as _csv_safe


def _json_default(o):
    """Keep date/datetime-likes as ISO strings so serialisation is stable across runs.

    ``datetime.isoformat()`` uses 'T'; falling back to ``str()`` for anything else.
    """
    if hasattr(o, "isoformat"):
        return o.isoformat()
    return str(o)


def _parse_jsonl_lines(lines, key_field: str) -> dict[str, dict]:
    """Parse an iterable of text lines into ``{key: record}``, fail-soft per line."""
    store: dict[str, dict] = {}
    for lineno, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            print(f"  store: skipping malformed JSONL line {lineno}: {e}", file=sys.stderr)
            continue
        if not isinstance(obj, dict):
            print(f"  store: skipping non-object JSONL line {lineno}", file=sys.stderr)
            continue
        key = obj.get(key_field)
        if key is None:
            print(f"  store: skipping line {lineno} missing key field {key_field!r}",
                  file=sys.stderr)
            continue
        store[key] = obj  # keep-last wins for duplicate keys
    return store


def load_jsonl(path: str, key_field: str = "transaction_id") -> dict[str, dict]:
    """Read newline-delimited JSON into ``{key: record}``.

    Missing file → ``{}``. A malformed line is logged and skipped, never crashes.
    """
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as f:
        return _parse_jsonl_lines(f, key_field)


def load_jsonl_bytes(data: bytes | str | None,
                     key_field: str = "transaction_id") -> dict[str, dict]:
    """Parse in-memory JSONL bytes/str (e.g. a Drive ``pull()``) into ``{key: record}``.

    ``None`` / empty → ``{}``. Same fail-soft semantics as :func:`load_jsonl`.
    """
    if not data:
        return {}
    text = data.decode("utf-8") if isinstance(data, bytes) else data
    return _parse_jsonl_lines(text.splitlines(), key_field)


def save_jsonl(path: str, store: dict[str, dict], key_field: str = "transaction_id") -> None:
    """Atomically write the store as JSONL (temp file + ``os.replace``).

    Records are sorted by ``(date, key_field)`` for stable git diffs. One compact JSON
    object per line, ``ensure_ascii=False``. Creates the parent dir if needed.
    """
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)

    rows = sorted(
        store.values(),
        key=lambda r: (str(r.get("date") or ""), str(r.get(key_field) or "")),
    )

    fd, tmp = tempfile.mkstemp(dir=parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False, separators=(",", ":"),
                                   default=_json_default))
                f.write("\n")
        os.replace(tmp, path)  # atomic — never leave a half-written store
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def derive_csv(store: dict[str, dict], csv_path: str, columns: list[str],
               row_fn, csv_safe: bool = True) -> None:
    """Project each record via ``row_fn(record) -> dict`` and write a flat CSV.

    Columns are written in the given order (``extrasaction='ignore'`` so a row_fn may
    return extra keys). When ``csv_safe`` is True every cell is passed through the
    formula-injection guard. Output is deterministically ordered (by ``date`` then the
    full row) so re-deriving an unchanged store yields an identical file.
    """
    import csv as _csv

    parent = os.path.dirname(os.path.abspath(csv_path))
    os.makedirs(parent, exist_ok=True)

    rows = [row_fn(r) for r in store.values()]
    rows.sort(key=lambda r: (str(r.get("date", "")),
                             tuple(str(r.get(c, "")) for c in columns)))

    fd, tmp = tempfile.mkstemp(dir=parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            writer = _csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                if csv_safe:
                    row = {k: _csv_safe(v) for k, v in row.items()}
                writer.writerow(row)
        os.replace(tmp, csv_path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def dedupe_supersede(store: dict[str, dict]) -> dict[str, dict]:
    """Drop pending rows superseded by a posted row, preserve everything else.

    A posted (non-pending) record references the pending one it replaced via
    ``pending_transaction_id``; that pending row (whose store key == the referenced id)
    is the only thing dropped. Keep-last-per-key is already implied by dict keying.

    Ported from ``spend_analyzer/ingest/dedupe.py`` ``drop_settled_pending``.
    """
    superseded = {
        r.get("pending_transaction_id")
        for r in store.values()
        if not r.get("pending") and r.get("pending_transaction_id")
    }
    superseded.discard(None)
    return {
        key: r
        for key, r in store.items()
        if not (r.get("pending") and key in superseded)
    }
