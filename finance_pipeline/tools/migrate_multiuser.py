#!/usr/bin/env python3
"""One-off migration to multi-user transaction ownership (stamping only, no moves).

Multi-user support attributes every transaction to the user whose Plaid Item produced
it: tokens.json entries carry an ``owner`` and every fetched record is stamped with
``txn_owner`` at the fetch boundary. This script back-fills both onto data that
predates the feature:

  1. ``transactions/.secrets/tokens.json``       — add ``"owner": <owner>`` per entry
  2. ``transactions/data/transactions_raw.jsonl.xz`` — stamp ``txn_owner`` per record
  3. ``transactions/data/transactions.jsonl``        — stamp ``txn_owner`` per record
  4. ``plaid_category_transformer/data/transactions_categorized.jsonl`` — same stamp
     (hash-safe: the transformer's source hash excludes ``txn_owner``, so this cannot
     trigger a re-audit; the persister reconcile likewise ignores it vs the old
     Drive remote, so the next Drive-enabled run pushes cleanly with no force flags)

Two-phase: every file is SCANNED first (a token entry or record already carrying a
*different* owner aborts the whole run before anything is written), then applied with
atomic tmp+replace writes that preserve each file's permissions. Idempotent — a re-run
finds nothing left to stamp. Touches nothing else: no Drive, no network, no secrets
beyond tokens.json, no CSVs (derived; the new column appears on the next fetch).

Usage:
    python3 tools/migrate_multiuser.py --dry-run      # see what would change
    python3 tools/migrate_multiuser.py --yes          # apply
"""
from __future__ import annotations

import argparse
import json
import lzma
import os
import re
import sys
from pathlib import Path

_OWNER_RE = re.compile(r"^[A-Za-z0-9_-]+$")

# Monorepo root: tools/migrate_multiuser.py -> tools/ -> finance_pipeline/ -> <root>
_DEFAULT_ROOT = Path(__file__).resolve().parent.parent.parent


def _data_root(monorepo_root: Path) -> Path:
    """The external data root (same resolution as every component):
    $SPEND_VISUALIZER_DATA, else the monorepo-root ``data_root`` file,
    else ~/finance_data."""
    env = os.environ.get("SPEND_VISUALIZER_DATA")
    if env:
        return Path(env).expanduser()
    cfg = monorepo_root / "data_root"
    if cfg.is_file():
        for line in cfg.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                return Path(line).expanduser()
    return Path("~/finance_data").expanduser()


class Abort(SystemExit):
    def __init__(self, msg: str):
        super().__init__(f"✖ ABORT — {msg}\nNothing was modified.")


# ── Scan phase (read-only) ─────────────────────────────────────────────────────

def scan_tokens(path: Path, owner: str) -> dict:
    """Plan for tokens.json: which entries need an owner. Mismatch aborts."""
    entries = json.loads(path.read_text(encoding="utf-8"))
    todo = 0
    for e in entries:
        have = e.get("owner")
        if have is None:
            todo += 1
        elif have != owner:
            raise Abort(
                f"{path} entry for {e.get('institution', e.get('item_id'))!r} already "
                f"belongs to {have!r} (stamping {owner!r} would mis-attribute it). "
                "A blanket migration only fits a single-owner history."
            )
    return {"path": path, "kind": "tokens", "entries": entries, "todo": todo,
            "total": len(entries)}


def _scan_records(path: Path, lines: list[str], owner: str) -> dict:
    records = []
    todo = 0
    for n, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            raise Abort(f"{path}:{n} is not valid JSON ({e}) — refusing to rewrite "
                        "a store I cannot fully parse.")
        have = rec.get("txn_owner")
        if have is None:
            todo += 1
        elif have != owner:
            raise Abort(
                f"{path}:{n} (transaction {rec.get('transaction_id')!r}) already "
                f"carries txn_owner={have!r} — refusing to blanket-stamp {owner!r}."
            )
        records.append(rec)
    return {"path": path, "records": records, "todo": todo, "total": len(records)}


def scan_jsonl(path: Path, owner: str) -> dict:
    plan = _scan_records(path, path.read_text(encoding="utf-8").splitlines(), owner)
    return {**plan, "kind": "jsonl"}


def scan_xz(path: Path, owner: str) -> dict:
    with lzma.open(path, "rt", encoding="utf-8") as f:
        plan = _scan_records(path, f.read().splitlines(), owner)
    return {**plan, "kind": "xz"}


# ── Apply phase (atomic, permission-preserving) ────────────────────────────────

def _atomic_write(path: Path, text: str):
    mode = path.stat().st_mode & 0o777  # preserve the file's existing perms (0600 etc.)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, mode)
    tmp.replace(path)


def _atomic_write_xz(path: Path, lines: list[str]):
    mode = path.stat().st_mode & 0o777
    tmp = path.with_suffix(path.suffix + ".tmp")
    # Match save_raw_store's compression settings so the archive stays consistent.
    with lzma.open(tmp, "wt", encoding="utf-8", preset=9 | lzma.PRESET_EXTREME) as f:
        for line in lines:
            f.write(line + "\n")
    os.chmod(tmp, mode)
    tmp.replace(path)


def _dump(rec: dict) -> str:
    return json.dumps(rec, ensure_ascii=False, default=str)


def apply_plan(plan: dict, owner: str):
    if plan["kind"] == "tokens":
        for e in plan["entries"]:
            e.setdefault("owner", owner)
        _atomic_write(plan["path"], json.dumps(plan["entries"], indent=2, default=str))
        return
    lines = []
    for rec in plan["records"]:
        rec.setdefault("txn_owner", owner)
        lines.append(_dump(rec))
    if plan["kind"] == "xz":
        _atomic_write_xz(plan["path"], lines)
    else:
        _atomic_write(plan["path"], "\n".join(lines) + ("\n" if lines else ""))


# ── Entry point ────────────────────────────────────────────────────────────────

def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--owner", required=True, metavar="NAME",
                   help="display name all existing (pre-multi-user) data belongs to")
    p.add_argument("--root", default=str(_DEFAULT_ROOT), metavar="DIR",
                   help="monorepo root, holds .secrets/tokens.json "
                        "(default: auto-detected from this script)")
    p.add_argument("--data-root", default=None, metavar="DIR",
                   help="external data root holding the stores "
                        "(default: resolved like every component — "
                        "$SPEND_VISUALIZER_DATA / the data_root file / ~/finance_data)")
    p.add_argument("--dry-run", action="store_true",
                   help="report what would change; write nothing")
    p.add_argument("--yes", action="store_true",
                   help="apply without the interactive confirmation")
    args = p.parse_args(argv)

    if not _OWNER_RE.match(args.owner or ""):
        raise SystemExit(f"Invalid owner {args.owner!r} — letters/digits/_/- only.")
    root = Path(args.root).resolve()
    data = (Path(args.data_root).expanduser().resolve() if args.data_root
            else _data_root(root))

    targets = [
        (root / "transactions" / ".secrets" / "tokens.json", scan_tokens),
        (data / "transactions" / "data" / "transactions_raw.jsonl.xz", scan_xz),
        (data / "transactions" / "data" / "transactions.jsonl", scan_jsonl),
        (data / "plaid_category_transformer" / "data" / "transactions_categorized.jsonl",
         scan_jsonl),
    ]

    # Phase 1 — scan everything before touching anything (any mismatch aborts here).
    plans = []
    for path, scan in targets:
        if not path.is_file():
            print(f"  skip (missing): {path}")
            continue
        plans.append(scan(path, args.owner))

    pending = [pl for pl in plans if pl["todo"]]
    for pl in plans:
        what = "entries" if pl["kind"] == "tokens" else "records"
        print(f"  {pl['path']}: {pl['todo']} of {pl['total']} {what} to stamp")
    if not pending:
        print(f"\n✓ Nothing to do — everything already owned by {args.owner!r}.")
        return 0
    if args.dry_run:
        print("\n(dry run — nothing written)")
        return 0
    if not args.yes:
        reply = input(f"\nStamp the above as {args.owner!r}? [y/N] ").strip().lower()
        if reply not in ("y", "yes"):
            print("Cancelled — nothing written.")
            return 1

    # Phase 2 — apply (atomic per file; perms preserved).
    for pl in pending:
        apply_plan(pl, args.owner)
        print(f"  stamped: {pl['path']}")

    print(f"""
✓ Migration complete — all existing data is owned by {args.owner!r}.
  Next steps (yours to run):
  - The data root ({data}) is its own git repo: review + commit the diff there.
  - The next Drive-enabled ./run.py pushes the stamped stores as new revisions of
    the SAME Drive files (the reconcile/divergence gates ignore txn_owner, so no
    --force-push is needed; old revisions survive as history).
  - Link the second user's banks with:  cd transactions && ./venv/bin/python app.py --user <name>""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
