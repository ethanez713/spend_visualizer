"""Bridge to plaid_category_transformer's manual-edit intent log (deliberately coupled).

A category fix made in this UI NEVER edits a record. It is appended as an INTENT to the
transformer's append-only log (``data/manual_edits.jsonl``); the transformer's final
pipeline stage replays every intent on every categorize run, which makes edits sticky —
they survive ``--full`` re-audits, and a merchant-scope intent covers that merchant's
future transactions too.

We import the transformer's own ``src.manual`` / ``src.pfc_taxonomy`` (pure stdlib — no
second venv involved), so intent validation, merchant-name normalization, and the
analysis snapshot format can never drift from the categorizer's. The transformer repo
root comes from ``config/app.yaml → transformer_root``. Intents are built from the RAW
archive record (not the cube row): it carries the merchant entity id, the original
description, and the provenance columns the snapshot needs.
"""
from __future__ import annotations

import sys
import os
from pathlib import Path

from config_io import DATA_ROOT, load_app_config
from ingest.load import load_sources, stat_source

_modules: tuple | None = None  # (src.manual, src.pfc_taxonomy) once imported


def _import() -> tuple:
    global _modules
    if _modules is None:
        root = load_app_config().resolved_transformer_root
        if not (Path(root) / "src" / "manual.py").is_file():
            raise FileNotFoundError(
                f"transformer repo not found at {root} "
                "(set config/app.yaml → transformer_root)")
        if root not in sys.path:
            sys.path.append(root)  # append, never insert: don't shadow our own modules
        from src import manual, pfc_taxonomy
        _modules = (manual, pfc_taxonomy)
    return _modules


def status() -> str | None:
    """None when the bridge is usable, else a short reason to show in the UI."""
    try:
        _import()
        return None
    except Exception as e:  # noqa: BLE001 — any failure just disables the feature
        return str(e)


def edits_path() -> str:
    # The transformer's CODE comes from transformer_root, but its DATA (incl. the
    # intent log) lives under the shared external data root.
    return str(DATA_ROOT / "plaid_category_transformer" / "data" / "manual_edits.jsonl")


def taxonomy() -> tuple[list[str], dict[str, list[str]]]:
    """The transformer's vendored PFC menu: (PRIMARY list, {primary: [detailed...]})."""
    _, tax = _import()
    return tax.PRIMARY, tax.DETAILED


def add_edit(raw_record: dict, *, scope: str, primary: str, detailed: str,
             note: str = "", path: str | None = None, source: str = "ui",
             phrase: str | None = None) -> dict:
    """Validate + append one intent. Raises ValueError on bad input. ``phrase`` is the
    text a description-scope intent matches on."""
    manual, _ = _import()
    intent = manual.build_intent(scope=scope, primary=primary, detailed=detailed,
                                 record=raw_record, note=note, source=source, phrase=phrase)
    return manual.append_intent(path or edits_path(), intent)


def normalize_phrase(text: str) -> str:
    """A description phrase as the transformer will match it."""
    manual, _ = _import()
    return manual.normalize_phrase(text)


def description_matches(records: list[dict], phrase: str) -> list[dict]:
    """The records a description-scope intent with this phrase would cover, by the
    transformer's own matcher. [] for a phrase too short to be a rule."""
    manual, _ = _import()
    norm = manual.normalize_phrase(phrase)
    if len(norm) < manual.MIN_PHRASE_CHARS:
        return []
    return [r for r in records if manual.description_matches(r, norm)]


def dismiss(raw_record: dict, *, note: str = "", path: str | None = None,
            source: str = "ui") -> dict:
    """Append an intent rejecting the row's pending suggestion; its category stays."""
    manual, _ = _import()
    return manual.append_intent(
        path or edits_path(), manual.build_dismissal(raw_record, note=note, source=source))


def intents(path: str | None = None) -> list[dict]:
    """The currently applicable intents (revokes resolved), in append order."""
    manual, _ = _import()
    return manual.resolve_intents(manual.load_intents(path or edits_path()))


def revoke(intent_id: str, *, note: str = "", path: str | None = None) -> dict:
    """Append a tombstone: the row reverts and re-audits on the next categorize run."""
    manual, _ = _import()
    return manual.append_intent(
        path or edits_path(), manual.build_revoke(intent_id, note=note, source="ui"))


def overlay(records: list[dict], path: str | None = None) -> None:
    """Replay the live intents over raw records in memory (mutates them).

    The categorized store only reflects an intent after the next categorize run;
    replaying here with the transformer's own code shows the same result at once,
    so an override, a revoke or a review decision is visible as soon as it is
    saved. Fail-soft: without the transformer the records are left as stored.
    """
    if status() is not None:
        return
    manual, _ = _import()
    store = {r["transaction_id"]: r for r in records if r.get("transaction_id")}
    manual.apply_manual_edits(store, manual.load_index(path or edits_path()))


def affected_counts(items: list[dict], records: list[dict] | None = None) -> dict[str, int]:
    """{intent id: how many archive rows it currently covers}.

    Uses the transformer's own ``ManualIndex.match`` over the raw records, so the
    count reflects exactly what replay will touch (txn scope ⇒ 0 or 1; merchant
    scope grows as new transactions arrive). ``records`` defaults to the archive.
    """
    manual, _ = _import()
    idx = manual.ManualIndex(items)
    counts = {it["id"]: 0 for it in items}
    if records is None:
        records = list(raw_index().values())
    for rec in records:
        hit = idx.match(rec)
        if hit is not None:
            counts[hit["id"]] += 1
    return counts


def archive_sig() -> tuple:
    """Cache key for anything derived from the archives plus the intent log
    (mtime/size of each): both change what a loaded record says."""
    sig = [edits_sig()]
    for p in load_app_config().resolved_archive_paths:
        try:
            sig.append(stat_source(p).cache_key)
        except FileNotFoundError:
            sig.append((p, 0.0, 0))
    return tuple(sig)


def edits_sig() -> tuple:
    """(name, mtime, size) of the intent log — a cache-key entry."""
    try:
        s = os.stat(edits_path())
        return ("manual_edits.jsonl", s.st_mtime, s.st_size)
    except OSError:
        return ("manual_edits.jsonl", 0.0, 0)


def raw_index() -> dict[str, dict]:
    """{transaction_id: raw record} across the configured archives (full Plaid
    rows), with the live intents replayed — what the UI is showing."""
    records = load_sources(load_app_config().resolved_archive_paths)
    overlay(records)
    return {r["transaction_id"]: r for r in records}
