"""Orchestration that drives the generic `persister` library with Plaid-specific logic.

Run AFTER a normal /transactions/sync (fetch_transactions.main()). This is the business
logic the persister itself has no knowledge of: it reconciles the local raw store against
the Drive-synced remote, repairs any drift with a bounded /transactions/get fetch (Plaid
golden ONLY on success), dedupes settled pendings, then writes the durable JSONL store +
derived CSV and pushes new Drive revisions.

THIS repo owns its durable data and its Drive credentials/state — `persister` is a pure
library (no data of its own; any consumer passes its own paths + secrets_dir). The
durable store lives in this repo's gitignored data/ dir; its audit history is Drive's
native revision trail (persister's Drive access is append-only). `data_dir` is a
parameter so it can be redirected (e.g. to a tmp dir in tests).
"""
import sys

import persister

from .fetch_transactions import CSV_COLUMNS, get_account_meta, txn_to_row
from .fetch_window import fetch_window
from .plaid_client import DATA_DIR, SECRETS_DIR, get_client, load_raw_store, load_tokens

# Default home of the durable store: this repo's data/ dir (gitignored, 0700) —
# alongside the raw xz archive it is derived from.
DEFAULT_DATA_DIR = str(DATA_DIR)

# Logical Drive file names (one canonical JSONL + one human-viewable CSV), kept in place
# with native Drive revision history under this folder.
_DRIVE_FOLDER = "transactions_archive"
_JSONL_NAME = "transactions.jsonl"
_CSV_NAME = "transactions.csv"

# Local, gitignored (0600) audit trail complementing git + Drive revisions.
_RECONCILE_LOG = str(DATA_DIR / "reconcile_log.jsonl")


def run_persist(*, do_drive: bool = True, allow_refetch: bool = True,
                data_dir: str = DEFAULT_DATA_DIR) -> None:
    """Reconcile → (golden repair) → dedupe → persist → Drive-sync the transaction store.

    Steps:
      1. local  = the existing xz raw store as {transaction_id: record}.
      2. remote = the Drive-synced canonical store (or {} if --no-drive / nothing pushed).
      3. report = reconcile(local, remote).
      4. On content conflicts (and allow_refetch): compute a tight repair window covering
         the conflicting ids, re-fetch them via /transactions/get, and let Plaid (golden)
         overwrite via merge_golden. A Plaid error skips the item — never deletes local data.
         Any conflict the golden re-fetch does NOT confirm (or every conflict, when
         allow_refetch is False) is unresolved → SystemExit (non-zero) BEFORE the durable
         store is written or Drive is pushed, so callers (e.g. the finance_pipeline
         orchestrator) stop instead of persisting divergent data.
      5. dedupe_supersede drops settled pendings.
      6. Write the durable JSONL store + derived CSV under data_dir.
      7. If do_drive: push new Drive revisions of both (prints an egress notice first).
         Always append a reconcile-log line.
    """
    jsonl_path = f"{data_dir}/{_JSONL_NAME}"
    csv_path = f"{data_dir}/{_CSV_NAME}"

    # 1. Local store (existing xz JSONL archive) as a keyed dict.
    local = load_raw_store()

    # 2. Remote store from Drive (offline by default unless do_drive). Drive credentials
    #    + file-id state are THIS repo's (.secrets/) — persister is a pure library.
    drive = persister.DriveSync(_JSONL_NAME, folder_name=_DRIVE_FOLDER,
                                secrets_dir=str(SECRETS_DIR))
    remote = persister.load_jsonl_bytes(drive.pull()) if do_drive else {}

    # 3. Classify local vs remote.
    report = persister.reconcile(local, remote)
    print(
        f"  persist: reconcile — {len(report.in_sync)} in sync, "
        f"{len(report.local_only)} local-only, {len(report.remote_only)} remote-only, "
        f"{len(report.conflicts)} conflict(s)"
    )

    merged = report.merged

    # 4. Plaid is golden on conflicts: re-fetch a bounded window and overwrite. A conflict
    #    the re-fetch cannot confirm (id absent from the response — aged out of Plaid's
    #    window, or the item errored) is UNRESOLVED: stop before the durable store or
    #    Drive is touched, rather than silently persisting divergent data.
    if report.conflicts:
        unresolved = sorted(report.conflicts)
        if allow_refetch:
            win = persister.compute_window(merged, extra_tids=report.conflicts)
            print(
                f"  persist: repairing {len(report.conflicts)} conflict(s) via "
                f"/transactions/get [{win.start_date} .. {win.end_date}]"
            )
            fresh = fetch_window(get_client(), load_tokens(), win.start_date, win.end_date)
            merged = persister.merge_golden(merged, fresh)
            fresh_ids = {r.get("transaction_id") for r in fresh}
            unresolved = sorted(set(report.conflicts) - fresh_ids)
        if unresolved:
            persister.log_reconcile(_RECONCILE_LOG, report, source="transactions")
            shown = ", ".join(unresolved[:10]) + (", …" if len(unresolved) > 10 else "")
            why = ("the repair re-fetch is disabled (--no-refetch)" if not allow_refetch
                   else "the Plaid golden re-fetch could not confirm them")
            raise SystemExit(
                f"  persist: STOP — {len(unresolved)} unresolved conflict(s) between the "
                f"local raw store and the Drive remote: {shown}. {why.capitalize()}. "
                "Nothing was written to the durable store or Drive. Inspect the records "
                "(e.g. ../persister: persist.py reconcile) and re-run."
            )

    # 5. Drop pending rows superseded by a posted row.
    merged = persister.dedupe_supersede(merged)

    # 6. Persist the durable store + derived CSV. Account meta (institution / account_name)
    #    is joined only here, at CSV projection — the JSONL records stay pure (reconcile parity).
    account_meta = get_account_meta(get_client(), load_tokens())
    persister.save_jsonl(jsonl_path, merged)
    persister.derive_csv(
        merged, csv_path, CSV_COLUMNS,
        row_fn=lambda r: txn_to_row(r, account_meta), csv_safe=True,
    )
    print(f"  persist: wrote {len(merged)} records → {jsonl_path} (+ .csv)")

    # 7. Drive sync (opt-out via --no-drive) + audit log.
    if do_drive:
        print("  persist: uploading transactions store to Google Drive (data leaving machine)…",
              file=sys.stderr)
        drive.push(jsonl_path, mime="application/x-ndjson")
        persister.DriveSync(_CSV_NAME, folder_name=_DRIVE_FOLDER,
                            secrets_dir=str(SECRETS_DIR)).push(csv_path, mime="text/csv")
    persister.log_reconcile(_RECONCILE_LOG, report, source="transactions")
