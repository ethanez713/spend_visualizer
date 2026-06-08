"""persister — durable, deduped, reconciled, Drive-synced JSONL stores.

A *generic* persistence/reconcile/sync library: it operates on lists/dicts of records
keyed by a configurable ``key_field`` (default ``transaction_id``) and knows nothing
about Plaid. Both ``transactions`` and ``plaid_category_transformer`` import it.

Public API (see PLAN.md):
    store    — load_jsonl / load_jsonl_bytes / save_jsonl / derive_csv / dedupe_supersede
    reconcile — reconcile → ReconcileReport
    windows  — compute_window → Window
    merge    — merge_golden
    drive    — DriveSync (pull / push)
    audit    — log_reconcile
    csv_safe — csv_safe
"""
from .audit import log_reconcile
from .csv_safe import csv_safe
from .drive_sync import DriveSync
from .merge import merge_golden
from .reconcile import ReconcileReport, reconcile
from .store import (
    dedupe_supersede,
    derive_csv,
    load_jsonl,
    load_jsonl_bytes,
    save_jsonl,
)
from .windows import Window, compute_window

__all__ = [
    "load_jsonl",
    "load_jsonl_bytes",
    "save_jsonl",
    "derive_csv",
    "dedupe_supersede",
    "reconcile",
    "ReconcileReport",
    "compute_window",
    "Window",
    "merge_golden",
    "DriveSync",
    "log_reconcile",
    "csv_safe",
]

__version__ = "0.1.0"
