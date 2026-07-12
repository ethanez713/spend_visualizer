"""Read-only bridge to the categorization rule tables (transformer + converter).

The Rules tab (and the per-row "why this category?" explainers) render the rule
data that today lives as Python literals in two other projects:

- the transformer's mechanical tables (``KEYWORD_RULES`` / ``POS_PREFIX_RULES`` /
  ``WEBSITE_RULES``, already merged with the personal overlay) and its merchant
  memory — imported via ``transformer_root`` exactly like ``manual_edits`` does,
  so what we display can never drift from what the categorizer executes;
- the external converter's PFC → budget-category maps (the Google-Sheet-only
  taxonomy), loaded standalone via importlib from the same pointer the pipeline
  uses (``$SPEND_VISUALIZER_CONVERTER`` → ``<data_root>/converter_root``); the
  converter is optional, so every accessor fails soft to None.

Everything here is READ-ONLY: rule authoring stays a code edit in the owning
project (a deliberate decision — see docs/categorization-ux-ideation.md).

Match counts are computed by running the transformer's own ``apply_rules``
cascade over the raw archive (first-hit-wins, read-only memory), which answers
"what does this rule catch NOW?" — independent of whether a hit was later
applied, flagged, or superseded by a manual intent.
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

from config_io import DATA_ROOT, load_app_config
from ingest.load import load_sources, stat_source

_transformer_mods: tuple | None = None  # (src.config, src.rules) once imported

# Rule-table kind -> how a pattern becomes the RuleHit.rule_name join key
# (mirrors rules.apply_rules; category_update_reason / category_review_reason
# carry these exact strings on every row a rule touched).
_RULE_ID = {
    "pos_prefix": lambda p: f"pos:{p.lower().rstrip('*')}",
    "website": lambda p: f"website:{p}",
    "keyword": lambda p: f"keyword:{p}",
}


def _transformer_root() -> str:
    return load_app_config().resolved_transformer_root


def _import_transformer() -> tuple:
    global _transformer_mods
    if _transformer_mods is None:
        root = _transformer_root()
        if not (Path(root) / "src" / "rules.py").is_file():
            raise FileNotFoundError(
                f"transformer repo not found at {root} "
                "(set config/app.yaml → transformer_root)")
        if root not in sys.path:
            sys.path.append(root)  # append, never insert: don't shadow our own modules
        from src import config as t_config, rules as t_rules
        _transformer_mods = (t_config, t_rules)
    return _transformer_mods


def status() -> str | None:
    """None when the transformer bridge is usable, else a reason for the UI."""
    try:
        _import_transformer()
        return None
    except Exception as e:  # noqa: BLE001 — any failure just disables the feature
        return str(e)


def tables_sig() -> tuple:
    """Cache key: rule tables + memory + archive stats (mtime/size)."""
    sig = []
    try:
        root = Path(_transformer_root())
        for p in (root / "src" / "config.py",
                  root / ".secrets" / "merchant_memory.json",
                  DATA_ROOT / "plaid_category_transformer" / "config" / "personal_rules.json"):
            s = p.stat() if p.exists() else None
            sig.append((str(p), s.st_mtime if s else 0.0, s.st_size if s else 0))
    except Exception:  # noqa: BLE001 — a broken config just yields a cold cache key
        sig.append(("transformer", 0.0, 0))
    for p in load_app_config().resolved_archive_paths:
        try:
            sig.append(stat_source(p).cache_key)
        except FileNotFoundError:
            sig.append((p, 0.0, 0))
    return tuple(sig)


def _personal_patterns() -> set[tuple[str, str]]:
    """(kind, pattern) pairs from the data-root personal overlay (fail-soft)."""
    path = DATA_ROOT / "plaid_category_transformer" / "config" / "personal_rules.json"
    out: set[tuple[str, str]] = set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    for kind in ("pos_prefix", "website", "keyword"):
        for entry in data.get(kind, []) or []:
            if entry:
                out.add((kind, str(entry[0])))
    return out


def transformer_rules() -> list[dict]:
    """The mechanical pattern tables, flattened for display.

    One dict per rule: kind, rule_id (== RuleHit.rule_name — the join key to row
    provenance), pattern, primary, detailed, trust, origin (built-in | personal).
    """
    t_config, _ = _import_transformer()
    personal = _personal_patterns()
    tables = (("pos_prefix", t_config.POS_PREFIX_RULES),
              ("website", t_config.WEBSITE_RULES),
              ("keyword", t_config.KEYWORD_RULES))
    out = []
    for kind, table in tables:
        for pattern, (primary, detailed), trust in table:
            out.append({
                "kind": kind,
                "rule_id": _RULE_ID[kind](pattern),
                "pattern": pattern,
                "primary": primary,
                "detailed": detailed,
                "trust": trust,
                "origin": "personal" if (kind, pattern) in personal else "built-in",
            })
    return out


def rule_details() -> dict[str, dict]:
    """{rule_id: rule dict} for cross-referencing a row's provenance to its rule.

    The two memory rule names are synthesized here (memory hits aren't table
    rows, but rows stamped by them still need an explanation).
    """
    details = {r["rule_id"]: r for r in transformer_rules()}
    details.setdefault("memory:entity_id", {
        "kind": "merchant memory", "rule_id": "memory:entity_id",
        "pattern": "exact merchant_entity_id remembered from a past resolution",
        "primary": "(per merchant)", "detailed": "(per merchant)",
        "trust": "auto", "origin": "memory"})
    details.setdefault("memory:name", {
        "kind": "merchant memory", "rule_id": "memory:name",
        "pattern": "normalized merchant-name remembered from a past resolution",
        "primary": "(per merchant)", "detailed": "(per merchant)",
        "trust": "flag", "origin": "memory"})
    return details


def _memory(read_path: bool = True):
    _, t_rules = _import_transformer()
    path = str(Path(_transformer_root()) / ".secrets" / "merchant_memory.json")
    return t_rules.MerchantMemory(path if read_path else None, read_only=True)


def merchant_memory_entries(memory=None) -> list[dict]:
    """The merchant-memory store, flattened: match kind, merchant key, category."""
    mem = memory if memory is not None else _memory()
    out = []
    for key, val in sorted(mem.store.items()):
        space, _, ident = key.partition(":")
        out.append({
            "key": key,
            "match": "entity id (auto)" if space == "ent" else "name (flag)",
            "merchant": ident,
            "primary": val.get("primary", ""),
            "detailed": val.get("detailed", ""),
        })
    return out


def _row_summary(rec: dict) -> dict:
    pfc = rec.get("personal_finance_category") or {}
    return {
        "transaction_id": rec.get("transaction_id"),
        "date": rec.get("date"),
        "description": rec.get("merchant_name") or rec.get("name"),
        "amount": rec.get("amount"),
        "current category": f"{pfc.get('primary')} / {pfc.get('detailed')}",
    }


def match_scan(records: list[dict] | None = None, memory=None) -> dict:
    """Run the mechanical cascade over the archive; group hits by rule and memory key.

    Returns {"by_rule": {rule_id: [row summaries]}, "by_memory_key": {key: count}}.
    ``records`` defaults to the configured raw archive(s); ``memory`` to the
    transformer's live merchant memory (read-only).
    """
    _, t_rules = _import_transformer()
    mem = memory if memory is not None else _memory()
    if records is None:
        records = load_sources(load_app_config().resolved_archive_paths)

    by_rule: dict[str, list[dict]] = {}
    by_memory_key: dict[str, int] = {}
    for rec in records:
        hit = t_rules.apply_rules(rec, mem)
        if hit is None:
            continue
        by_rule.setdefault(hit.rule_name, []).append(_row_summary(rec))
        if hit.rule_name == "memory:entity_id":
            key = f"ent:{rec.get('merchant_entity_id')}"
            by_memory_key[key] = by_memory_key.get(key, 0) + 1
        elif hit.rule_name == "memory:name":
            key = f"name:{t_rules.normalize_merchant(rec.get('merchant_name'))}"
            by_memory_key[key] = by_memory_key.get(key, 0) + 1
    return {"by_rule": by_rule, "by_memory_key": by_memory_key}


# ── Converter (Google-Sheet budget mapping) — optional, separate taxonomy ─────

def converter_dir() -> Path | None:
    """Resolve the external converter project like finance_pipeline does."""
    env = os.environ.get("SPEND_VISUALIZER_CONVERTER")
    if env:
        p = Path(env).expanduser()
        return p if p.is_dir() else None
    pointer = DATA_ROOT / "converter_root"
    try:
        for line in pointer.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                p = Path(line).expanduser()
                return p if p.is_dir() else None
    except OSError:
        pass
    return None


def converter_rules() -> dict | None:
    """The converter's PFC → budget-category policy tables, or None when absent.

    Loaded standalone via importlib (the converter's config.py is import-free by
    design); any failure means the Sheet section simply doesn't render.
    """
    d = converter_dir()
    if d is None:
        return None
    cfg = d / "src" / "config.py"
    if not cfg.is_file():
        return None
    try:
        spec = importlib.util.spec_from_file_location("_sheet_converter_config", cfg)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except Exception:  # noqa: BLE001 — optional feature, never take the app down
        return None
    return {
        "path": str(cfg),
        "pinned": [(cat, list(tokens))
                   for cat, tokens in getattr(mod, "PINNED_RULES", [])],
        "primary_map": dict(getattr(mod, "PFC_PRIMARY_MAP", {})),
        "detailed_map": dict(getattr(mod, "PFC_DETAILED_MAP", {})),
        "drop_primary": sorted(getattr(mod, "PFC_DROP_PRIMARY", ())),
        "drop_detailed": sorted(getattr(mod, "PFC_DROP_DETAILED", ())),
    }
