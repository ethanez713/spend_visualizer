"""Categorization-correction queue + triage report.

A correction NEVER edits the underlying transaction records (hard requirement).
It records the original record + a suggested diff, tagged with the *layer* where
the fix belongs:

  • upstream  — the transaction-generation service (Plaid collector). Use when
    the source data itself is wrong: bad PFC `detailed` code, wrong/missing
    merchant. Fixing it there improves the data for every consumer.
  • local     — this rendering service's taxonomy.yaml. Use when the data is
    fine but our *grouping* is off (atom -> tier mapping, a merchant override).

The report is copy/paste-able into the collector, and for local fixes we also
emit a ready-to-paste taxonomy.yaml snippet.

Persisted append-only to data/corrections.jsonl (gitignored, 0600) so a queue
survives reloads but never leaves the machine.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
STORE = DATA_DIR / "corrections.jsonl"

LAYERS = {
    "upstream": "Fix in transaction-generation service (source data wrong)",
    "local": "Fix here in taxonomy.yaml (our grouping is wrong)",
}

# Fields a correction may suggest changing, and where each is owned.
FIELD_LAYER = {
    "pfc_detailed": "upstream",
    "merchant_name": "upstream",
    "merchant_entity_id": "upstream",
    "tier0": "local",
    "tier1": "local",
    "tier2": "local",
}


def _ensure_store() -> None:
    DATA_DIR.mkdir(mode=0o700, exist_ok=True)
    try:
        os.chmod(DATA_DIR, 0o700)
    except OSError:
        pass
    if not STORE.exists():
        STORE.touch()
    try:
        os.chmod(STORE, 0o600)
    except OSError:
        pass


def suggest_layer(changed_fields: list[str]) -> str:
    """Heuristic: any source-owned field implies an upstream fix."""
    for f in changed_fields:
        if FIELD_LAYER.get(f) == "upstream":
            return "upstream"
    return "local"


def add_correction(
    *,
    scope: str,
    target: dict,
    original: dict,
    suggestion: dict,
    layer: str | None = None,
    note: str = "",
) -> dict:
    _ensure_store()
    changed = [k for k, v in suggestion.items() if str(v) != str(original.get(k, ""))]
    rec = {
        "id": uuid.uuid4().hex[:8],
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "scope": scope,                 # transaction | merchant | atom
        "target": target,
        "original": original,
        "suggestion": suggestion,
        "changed_fields": changed,
        "layer": layer or suggest_layer(changed),
        "note": note,
    }
    with STORE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")
    return rec


def load_corrections() -> list[dict]:
    if not STORE.exists():
        return []
    out = []
    for line in STORE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def clear_corrections() -> None:
    _ensure_store()
    STORE.write_text("", encoding="utf-8")


def delete_correction(cid: str) -> None:
    keep = [c for c in load_corrections() if c["id"] != cid]
    _ensure_store()
    with STORE.open("w", encoding="utf-8") as fh:
        for c in keep:
            fh.write(json.dumps(c) + "\n")


def to_dataframe(corrections: list[dict]) -> pd.DataFrame:
    rows = []
    for c in corrections:
        diff = ", ".join(
            f"{k}: {c['original'].get(k, '∅')} → {c['suggestion'][k]}"
            for k in c["changed_fields"]
        )
        rows.append(
            {
                "id": c["id"],
                "layer": c["layer"],
                "scope": c["scope"],
                "what": c["target"].get("label", ""),
                "diff": diff,
                "note": c.get("note", ""),
                "when": c["created_at"],
            }
        )
    return pd.DataFrame(rows)


def report_markdown(corrections: list[dict]) -> str:
    """Triage report grouped by layer for copy/paste into the right service."""
    lines = ["# Categorization corrections report", ""]
    for layer, desc in LAYERS.items():
        items = [c for c in corrections if c["layer"] == layer]
        if not items:
            continue
        lines.append(f"## {layer.upper()} — {desc}")
        lines.append("")
        for c in items:
            lines.append(f"### {c['target'].get('label', c['scope'])}  ·  `{c['id']}`")
            tgt = c["target"]
            ids = " ".join(f"{k}=`{v}`" for k, v in tgt.items() if k != "label")
            if ids:
                lines.append(f"- target: {ids}")
            for k in c["changed_fields"]:
                lines.append(f"- **{k}**: `{c['original'].get(k, '∅')}` → `{c['suggestion'][k]}`")
            if c.get("note"):
                lines.append(f"- note: {c['note']}")
            lines.append("")
        if layer == "local":
            lines.append(_taxonomy_snippet(items))
    return "\n".join(lines)


def _taxonomy_snippet(local_items: list[dict]) -> str:
    """Ready-to-paste merchant_overrides for the local (taxonomy.yaml) fixes."""
    overrides = []
    for c in local_items:
        merchant = c["target"].get("merchant") or c["original"].get("merchant_name")
        spec = {k: c["suggestion"][k] for k in ("tier1", "tier2") if k in c["suggestion"]}
        if merchant and spec:
            kv = ", ".join(f"{k}: {v!r}" for k, v in spec.items())
            overrides.append(f"  '(?i){merchant}': {{{kv}}}")
    if not overrides:
        return ""
    return "```yaml\n# paste into config/taxonomy.yaml under merchant_overrides:\n" \
           "merchant_overrides:\n" + "\n".join(overrides) + "\n```\n"
