# persister

A **generic, reusable** Python library + CLI for durable, deduped, reconciled,
Google-Drive-synced **JSONL stores**. No Plaid knowledge — it operates on lists/dicts of
records keyed by a configurable `key_field` (default `transaction_id`).

It exists so a bounded-history upstream (e.g. Plaid's transaction window) can be backed by
a **durable, Google-Drive-replicated archive** (Drive's append-only revision trail is the
audit history) that reconciles local ↔ remote and remains the system of record even after
data ages out upstream.

> **Pure library — no data lives here.** Consuming repos (`transactions`,
> `plaid_category_transformer`) own their stores, their Drive credentials, and their
> `drive_state.json` file-id memory, passing their own paths + ``secrets_dir`` to this
> library. This repo's gitignored `.secrets/` is used only by the manual CLI below and
> the optional integration tests.

## Install

Each project keeps its own virtualenv. The library core needs **no** dependencies; the
Google libs are only for the Drive path (imported lazily).

```bash
cd persister
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt      # Google libs (Drive path)
./.venv/bin/pip install -r requirements-dev.txt  # pytest
./.venv/bin/pip install -e .                      # exposes `import persister`
```

The two sibling projects install it editable so edits propagate:
`./venv/bin/pip install -e ../persister`.

## Public API

```python
from persister import (
    load_jsonl, load_jsonl_bytes, save_jsonl, derive_csv, dedupe_supersede,  # store
    reconcile, ReconcileReport,                                              # reconcile
    compute_window, Window,                                                  # windows
    merge_golden,                                                            # merge
    DriveSync, AppendOnlyError,                                              # drive_sync
    log_reconcile,                                                           # audit
    csv_safe,                                                                # csv_safe
)
```

| Function | Job |
|---|---|
| `load_jsonl(path, key_field="transaction_id")` | Read JSONL → `{key: record}`. Missing file → `{}`. Fail-soft on bad lines. |
| `load_jsonl_bytes(data, ...)` | Same, from in-memory bytes/str (e.g. a Drive `pull()`). |
| `save_jsonl(path, store, ...)` | Atomic write, sorted by `(date, key)` for clean git diffs. |
| `derive_csv(store, csv_path, columns, row_fn, csv_safe=True)` | Project records → flat CSV with formula-injection guard. |
| `dedupe_supersede(store)` | Drop pending rows superseded by a posted row; preserve everything else. |
| `reconcile(local, remote, ...)` | Classify keys → `in_sync / local_only / remote_only / conflicts` + preserved `merged` union. |
| `compute_window(store, extra_tids=())` | Bounded date window for a targeted repair fetch. |
| `merge_golden(base, fresh, ...)` | Golden source overwrites by key; base-only keys kept; dedupe. |
| `DriveSync(file_name, folder_name, secrets_dir).pull()/push()` | Sync ONE file to Drive in place (native revisions). |
| `DriveSync(...).list_revisions()` | List the file's revision history (`{id, modifiedTime, size}`) for audit / rollback. |
| `DriveSync(...).pull_revision(rev_id)` | Download the full bytes of a **prior** revision. Diff two via `load_jsonl_bytes` + `reconcile`. |
| `DriveSync(...).restore_revision(rev_id)` | Roll back by re-pushing an old revision as a **new** head revision (history preserved). |
| `log_reconcile(path, report, *, source)` | Append one audit line to `.secrets/reconcile_log.jsonl`. |

### Append-only — the library cannot delete Drive data

`DriveSync` only reads, creates, and updates-in-place. The real Drive service is wrapped in
a guard that **blocks `delete` on files and revisions and rejects any `trashed=True` body**
(`AppendOnlyError`), so the tooling can never destroy a file or its revision history — even
if a future code change tried to. Roll back by *appending* a revision (`restore_revision`);
delete files yourself in the Drive UI if you ever need to.

## CLI

Standalone, mostly for testing / manual ops (real callers use the library API):

```bash
./.venv/bin/python persist.py window    --store ../transactions/data/transactions.jsonl
./.venv/bin/python persist.py reconcile --store ../transactions/data/transactions.jsonl \
                                        --secrets-dir ../transactions/.secrets [--no-drive]
./.venv/bin/python persist.py push      --store ../transactions/data/transactions.jsonl \
                                        --secrets-dir ../transactions/.secrets [--no-drive]
```

For Drive ops always pass the **owning repo's** `--secrets-dir` (its credentials +
`drive_state.json` file-id memory) — pushing with this repo's default `.secrets/`
would create a *new* Drive file instead of a revision of the existing one.

Drive sync is **ON by default** but disable-able with `--no-drive`; when on, it prints a
one-line "data leaving machine" notice. Least-privilege Google scope: `drive.file`.

## Tests

```bash
./.venv/bin/python -m pytest          # fast, offline, deterministic (Drive stubbed)
```

`integration_tests/` holds the optional real-Drive round-trip (manual; see its README).

## Security

- **Offline by default** — the core path makes zero network calls; Drive is opt-in.
- **Secrets** (`client_secret.json`, `token.json`) live in `.secrets/` (0700; files 0600),
  gitignored. Atomic writes re-assert 0600 on every write.
- **Least privilege** — `drive.file` scope: the app only sees files it created (hence the
  `file_id` is remembered in `.secrets/drive_state.json`).
- **Append-only Drive access** — the service is guarded so the library can never delete or
  trash a file/revision (see above). Durable history is never destroyed by the tooling.
- **Formula-injection guard** on every derived CSV cell.
