"""Standalone CLI for the persister — mostly for testing / manual ops.

The real callers (``transactions``, ``plaid_category_transformer``) drive the library
API directly. This CLI exposes the three handy commands:

    persist reconcile --store data/transactions.jsonl [--drive-file NAME] [--no-drive]
    persist push      --store data/transactions.jsonl [--drive-file NAME] [--no-drive]
    persist window    --store data/transactions.jsonl

Drive sync is ON by default (per user) but always disable-able with ``--no-drive``.
When Drive is used it prints a one-line "data leaving machine" notice first.
"""
from __future__ import annotations

import argparse
import os
import sys

from .audit import log_reconcile
from .drive_sync import DriveSync
from .reconcile import reconcile
from .store import load_jsonl, load_jsonl_bytes
from .windows import compute_window

_EGRESS_NOTICE = "  ⚠ Drive sync ON — transaction data will leave this machine (Google Drive). Use --no-drive to stay offline."


def _drive_file_name(args) -> str:
    """Logical Drive file name: explicit --drive-file, else the store's basename."""
    return args.drive_file or os.path.basename(args.store)


def _cmd_window(args) -> int:
    store = load_jsonl(args.store)
    win = compute_window(store)
    print(f"start_date : {win.start_date}")
    print(f"end_date   : {win.end_date}")
    print(f"pending    : {len(win.pending_ids)} id(s)")
    return 0


def _cmd_reconcile(args) -> int:
    local = load_jsonl(args.store)
    remote: dict = {}
    if not args.no_drive:
        print(_EGRESS_NOTICE)
        remote = load_jsonl_bytes(DriveSync(_drive_file_name(args)).pull())
    report = reconcile(local, remote)
    print(f"in_sync     : {len(report.in_sync)}")
    print(f"local_only  : {len(report.local_only)}")
    print(f"remote_only : {len(report.remote_only)}")
    print(f"conflicts   : {len(report.conflicts)}")
    if report.conflicts:
        print(f"  conflict keys: {', '.join(report.conflicts)}")
    # Audit log lives next to the store-owner's var/; use the DriveSync default var dir.
    log_path = os.path.join(DriveSync(_drive_file_name(args)).var_dir, "reconcile_log.jsonl")
    log_reconcile(log_path, report, source="cli")
    return 0


def _cmd_push(args) -> int:
    if args.no_drive:
        print("  --no-drive set — nothing to push.")
        return 0
    if not os.path.exists(args.store):
        print(f"  push: store not found at {args.store}", file=sys.stderr)
        return 1
    print(_EGRESS_NOTICE)
    link = DriveSync(_drive_file_name(args)).push(args.store)
    if link:
        print(f"  pushed → {link}")
        return 0
    print("  push failed (see message above); local data is untouched.", file=sys.stderr)
    return 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="persist", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name, help_ in (("reconcile", "diff local store vs Drive"),
                        ("push", "push the store to Drive (new revision)")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--store", required=True, help="path to the local JSONL store")
        p.add_argument("--drive-file", default=None,
                       help="logical Drive file name (default: store basename)")
        p.add_argument("--no-drive", action="store_true", help="stay fully offline")

    p_win = sub.add_parser("window", help="print the computed repair date window")
    p_win.add_argument("--store", required=True, help="path to the local JSONL store")

    args = parser.parse_args(argv)
    handler = {
        "reconcile": _cmd_reconcile,
        "push": _cmd_push,
        "window": _cmd_window,
    }[args.cmd]
    return handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
