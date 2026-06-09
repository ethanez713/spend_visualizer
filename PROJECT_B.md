# PROJECT B — wire `transactions/` into the `persister` library

> **You are starting cold in `~/transactions` (a git repo).** This file is your
> complete brief. Also read `~/persister/PLAN.md` — its **"Shared context"**
> section (house style + security baseline) and **"PROJECT A"** section (the persister public
> API you will call) are authoritative; this file is the `transactions`-side specifics.

## Goal / context
`transactions/` fetches Plaid bank/card transactions via cursor-based `/transactions/sync`
into a local raw store (`data/transactions_raw.jsonl.xz`, keyed by `transaction_id`) and a
derived `transactions.csv`. We are adding **durable, deduped, reconciled, Google-Drive-synced
persistence** using the new generic **`persister`** library (sibling repo at
`~/persister`). Division of labor:
- **`persister` (already built)** = generic persistence/reconcile/date-window/Drive-sync. No
  Plaid knowledge.
- **`transactions` (this task)** = the Plaid-specific business logic: a windowed **repair**
  fetch, plus orchestration that drives the persister.

**Keep `/transactions/sync` as the everyday path** — it's Plaid's most consistent endpoint
(never misses added/modified/removed deltas; surfaces pending→posted; the local store
durably accumulates beyond Plaid's history window; inherently avoids over-fetching). Add
**`/transactions/get`** strictly as a **bounded repair fetch** the persister triggers when it
detects local↔remote drift. Plaid is the golden source **only when it returns data, not on
API errors**.

## Prerequisite
`persister` must be built and installable first. Verify, then editable-install it into this
repo's venv:

    ls ~/persister/pyproject.toml          # should exist
    ./venv/bin/pip install -e ~/persister   # exposes `import persister`

If `persister` isn't ready yet, build against its documented public API
(`~/persister/PLAN.md` → PROJECT A) and stub it in tests; install editable later.

## Orientation — current `transactions/` repo (read these first)
- `src/fetch_transactions.py`: `CSV_COLUMNS` (54 cols, the derived-CSV schema), `txn_to_row(txn, account_meta)`
  (raw Plaid dict → flat row), `get_account_meta(client, tokens)` (`account_id → {institution, account_name, …}`),
  `sync_item(client, entry, raw_store)` (cursor sync loop — **leave as-is**), helpers `_v`, `_g`, `_csv_safe`,
  and `main()` (the fetch entry point).
- `src/plaid_client.py`: `get_client()`, `load_tokens()`, `load_raw_store()/save_raw_store()`
  (xz-compressed JSONL keyed by `transaction_id`), `data/` path constants, atomic + `0600` secure writes
  (`_ensure_secure_dir`, `_save_json`).
- `src/app.py`: Plaid Link flow; note `days_requested=730` is set at Item creation (the max history window).
- `fetch_transactions.py` (root), `app.py` (root): thin entry-point shims → `src/…:main`.
- `tests/conftest.py`: the `state` fixture that isolates all file I/O to a tmp dir (use it).
  `tests/test_sync.py`: a `FakeClient` pattern returning canned sync pages (copy it for the new tests).
- House style: `src/` + root shim + `data/` (0700; secrets 0600) + pinned `requirements.txt` + offline pytest.
  Tests must never hit the network or real `data/`/output paths.

## persister public API you will call (see persister/PLAN.md PROJECT A for exact signatures)
- `load_jsonl(path, key_field="transaction_id") -> dict[str, dict]`
- `save_jsonl(path, store, key_field="transaction_id")` (atomic; sorted for clean git diffs)
- `derive_csv(store, csv_path, columns, row_fn, csv_safe=True)`
- `dedupe_supersede(store) -> store` (drop settled-pending duplicates only)
- `reconcile(local, remote, key_field) -> ReconcileReport(in_sync, local_only, remote_only, conflicts, merged)`
- `compute_window(store, extra_tids=()) -> Window(start_date, end_date, pending_ids)`
- `merge_golden(base, fresh, key_field) -> store` (fresh overwrites by key; never drops base-only keys)
- `DriveSync(file_name, folder_name="transactions_archive", data_dir=…)` with `.pull() -> bytes|None`
  and `.push(local_path, mime) -> url|None`
- `log_reconcile(path, report, source=…)` (append-only `data/reconcile_log.jsonl`)

## Work item 1 — `src/fetch_window.py` (NEW): windowed `/transactions/get` repair fetch
    def fetch_window(client, tokens, start_date, end_date) -> list[dict]:
        """Bounded repair fetch using /transactions/get (NOT sync). For each token/item,
        paginate:
            req = TransactionsGetRequest(
                access_token=t["access_token"], start_date=start_date, end_date=end_date,
                options=TransactionsGetRequestOptions(
                    include_personal_finance_category=True, count=500, offset=offset))
            resp = client.transactions_get(req)
            collect resp["transactions"] (.to_dict() each) until
              offset + len(collected_this_item) >= resp["total_transactions"].
        Join account metadata via get_account_meta(client, tokens). Return raw dicts in the
        SAME shape as load_raw_store() values (so they merge by transaction_id downstream).
        On ApiException: log the error code and SKIP that item — never let a Plaid error
        delete/overwrite local data (Plaid is golden only on success)."""
- Imports: `from plaid.model.transactions_get_request import TransactionsGetRequest`,
  `from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions`,
  `from plaid.exceptions import ApiException`. Reuse `get_account_meta`, `_v`, `_g`.

## Work item 2 — `src/persist_runner.py` (NEW): orchestration that drives the persister
    def run_persist(*, do_drive=True, allow_refetch=True, data_dir="~/persister/data") -> None:
        """Run AFTER a normal sync (main()). Steps:
          1. local  = persister.load_jsonl-from(load_raw_store())   # existing xz store -> dict
          2. drive  = persister.DriveSync("transactions.jsonl", folder_name="transactions_archive")
             remote = persister.parse(drive.pull()) if do_drive else {}     # {} if no remote yet
          3. report = persister.reconcile(local, remote)
          4. merged = report.merged
             if allow_refetch and report.conflicts:                 # Plaid golden -> repair
                 win   = persister.compute_window(merged, extra_tids=report.conflicts)
                 fresh = fetch_window(get_client(), load_tokens(), win.start_date, win.end_date)
                 merged = persister.merge_golden(merged, fresh)
          5. merged = persister.dedupe_supersede(merged)
          6. account_meta = get_account_meta(get_client(), load_tokens())
             persister.save_jsonl(f"{data_dir}/transactions.jsonl", merged)
             persister.derive_csv(merged, f"{data_dir}/transactions.csv", CSV_COLUMNS,
                                  row_fn=lambda r: txn_to_row(r, account_meta))
          7. if do_drive:
                 drive.push(f"{data_dir}/transactions.jsonl", mime="application/x-ndjson")
                 persister.DriveSync("transactions.csv", …).push(f"{data_dir}/transactions.csv", "text/csv")
             persister.log_reconcile("data/reconcile_log.jsonl", report, source="transactions")
          8. Do NOT auto-commit the data files. Leave the working tree dirty for the user to
             review/commit (house norm: commit only when asked)."""
- **Where the durable data lives:** `~/persister/data/transactions.jsonl` (+ `.csv`).
  The persister repo owns the system-of-record data and is a **private** repo (it commits real
  financial data). Make `data_dir` a parameter/constant. **Confirm with the user before
  committing data anywhere.**

## Work item 3 — wire into the entry point
- Add flags to the fetch CLI (`src/fetch_transactions.py` `main()` / argparse):
  `--persist` (default ON) / `--no-persist`, `--no-drive`, `--no-refetch`. After the sync
  loop completes, call `run_persist(do_drive=not no_drive, allow_refetch=not no_refetch)`
  unless `--no-persist`.
- Add deps: create `requirements-persist.txt` with `-e ~/persister` plus the Google
  libs (`google-api-python-client==2.197.0`, `google-auth==2.53.0`, `google-auth-oauthlib==1.4.0`
  — match the versions in `converter/requirements.txt`). Keep persist OFF in unit tests.

## Tests (offline, deterministic — `tests/`)
- `tests/test_fetch_window.py`: a `FakeClient` (copy `tests/test_sync.py`'s pattern) whose
  `transactions_get` returns canned **paginated** pages; assert pagination stops at
  `total_transactions`, records are `.to_dict()`-shaped with account meta joined, and an
  `ApiException` on one item skips it (no crash, other items still returned).
- `tests/test_persist_runner.py`: stub `persister.DriveSync` and `fetch_window`; tmp `data_dir`;
  assert the sequence reconcile→(conflict⇒window+refetch+merge_golden)→dedupe→save_jsonl→
  derive_csv→push; assert no Drive calls when `do_drive=False`. No network.
- Run the full suite (existing 39 + new) green: `./venv/bin/python -m pytest`.

## Security / house style (must hold)
- **Offline by default:** Drive sync only when `do_drive` (default on per user, but
  `--no-drive` disables). Print a one-line "uploading to Google Drive" notice before egress.
- **Secrets in `.secrets/`** (`token.json`, `client_secret.json` for Drive) — `0600`; never logged
  or committed. `.gitignore` already covers `.secrets/`.
- **Formula-injection guard** on the derived CSV — pass `csv_safe=True` / reuse `_csv_safe`.
- **Pinned deps**, per-project venv. The committed **data** file is deliberately NOT gitignored
  (that's the point), but it lives in the **private** persister repo — never add it to a public repo.
- Plaid golden **only on success**; on ApiException keep local data and log.

## Verification (end-to-end)
1. Offline: `./venv/bin/python -m pytest` — existing + new tests green.
2. Live (GUARDED — outward Plaid + Drive egress; **ask the user first**):
   - `./venv/bin/python fetch_transactions.py --persist` → confirm
     `~/persister/data/transactions.jsonl` (+ `.csv`) written, deduped, sane row count.
   - Confirm a new Drive revision appears (Drive UI → file → version history).
   - Re-run → reconcile reports `in_sync` (no spurious conflicts).
   - Hand-edit one record in the local store → re-run → reconcile flags a `conflict` and the
     windowed `/transactions/get` repair overwrites it from Plaid (Plaid golden).

## Coordination notes
- Requires `persister` built + editable-installed first.
- Don't auto-commit data; ask before any live Plaid/Drive run; both data-bearing repos are private.
- (FYI) The transformer↔persister wiring is a *separate* project (`plaid_category_transformer`,
  PLAN.md PROJECT C) owned by another instance — not part of this task.
