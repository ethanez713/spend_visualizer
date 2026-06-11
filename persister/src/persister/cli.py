"""Standalone CLI for the persister — mostly for testing / manual ops.

The real callers (``transactions``, ``plaid_category_transformer``) drive the library
API directly. This CLI exposes the three handy commands:

    persist reconcile --store PATH [--drive-file NAME] [--secrets-dir DIR] [--no-drive]
    persist push      --store PATH [--drive-file NAME] [--secrets-dir DIR] [--no-drive]
    persist window    --store PATH

persister is a pure library: the OWNING repo holds the Drive credentials and the
``drive_state.json`` file-id memory, so Drive ops on a repo's store should pass that
repo's ``--secrets-dir`` (e.g. ``--secrets-dir ../transactions/.secrets``) — otherwise
a push from the library-default ``.secrets/`` would create a NEW Drive file instead of
a revision of the existing one.

Drive sync is ON by default (per user) but always disable-able with ``--no-drive``.
When Drive is used it prints a one-line "data leaving machine" notice first.
"""
from __future__ import annotations

import argparse
import os
import sys

from .audit import log_reconcile
from .drive_sync import DrivePullError, DriveSync
from .reconcile import reconcile
from .store import load_jsonl, load_jsonl_bytes
from .windows import compute_window

_EGRESS_NOTICE = "  ⚠ Drive sync ON — transaction data will leave this machine (Google Drive). Use --no-drive to stay offline."


def _drive_file_name(args) -> str:
    """Logical Drive file name: explicit --drive-file, else the store's basename."""
    return args.drive_file or os.path.basename(args.store)


def _drive_sync(args) -> DriveSync:
    """A DriveSync honoring --secrets-dir (the owning repo's credentials + file-id state)."""
    kwargs = {"secrets_dir": args.secrets_dir} if args.secrets_dir else {}
    return DriveSync(_drive_file_name(args), **kwargs)


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
        try:
            remote = load_jsonl_bytes(_drive_sync(args).pull())
        except DrivePullError as e:
            print(f"✖ {e} — cannot reconcile against an unreadable remote. "
                  "Retry, or use --no-drive for a local-only view.", file=sys.stderr)
            return 1
    report = reconcile(local, remote)
    print(f"in_sync     : {len(report.in_sync)}")
    print(f"local_only  : {len(report.local_only)}")
    print(f"remote_only : {len(report.remote_only)}")
    print(f"conflicts   : {len(report.conflicts)}")
    if report.conflicts:
        print(f"  conflict keys: {', '.join(report.conflicts)}")
    # Audit log lives next to the credentials in use (the owning repo's .secrets/).
    log_path = os.path.join(_drive_sync(args).secrets_dir, "reconcile_log.jsonl")
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
    link = _drive_sync(args).push(args.store)
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
        p.add_argument("--secrets-dir", default=None,
                       help="the OWNING repo's .secrets dir (credentials + "
                            "drive_state.json), e.g. ../transactions/.secrets")
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
