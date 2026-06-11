# CLAUDE.md — agent guide to spend_visualizer

Personal-finance monorepo: Plaid collector → category auditor → Streamlit UI, plus a
shared persistence library and an orchestrator. Five components, each with its OWN
venv, README, tests, and `.secrets/`. Code lives here; **all personal data lives
outside the repo** (see "Data root").

## Golden rules

1. **Never run a live fetch or Drive push.** `fetch_transactions.py` / `app.py` hit
   Plaid; `--persist`/Drive-enabled runs and `categorize.py` without `--no-drive`
   push to Google Drive. Verify with the offline test suites and synthetic data;
   hand live runs (`./run.py`) to the user. Local-only file operations are fine.
2. **Never write data into this repo.** All stores/state/personal config belong under
   the data root. If a change would create files in `*/data/` here, the paths are
   wrong. `accounts.yaml`/`budget.yaml` under the data root are personal; never
   commit financial data, account ids, or budgets to this repo (history was purged
   once already — don't make it needed twice).
3. **Secrets stay in `.secrets/`** (gitignored, 0700/0600): Plaid creds + tokens in
   `transactions/.secrets/`, Drive creds + `drive_state.json` + merchant memory in
   each pushing component's `.secrets/`. Never print token values; code re-applies
   perms on write.
4. Tests are **offline by design** — no network, no real data mutation. The analyzer
   UI suite reads the live archive read-only behind a before/after digest guard and
   skips (never errors) when no archive exists.

## Data root

Everything personal resolves to one external dir (mirrors the monorepo layout):
`$SPEND_VISUALIZER_DATA` → else first non-comment line of `<repo>/data_root` → else
`~/finance_data`. The user's data root is a separate PRIVATE git repo.

Resolver implementations (kept deliberately duplicated — components are
self-contained): `transactions/src/plaid_client.py:_data_root`,
`plaid_category_transformer/src/paths.py`, `spend_analyzer/config_io.py:data_root`,
`finance_pipeline/tools/migrate_multiuser.py:_data_root`.

| Data | Path under the data root |
|---|---|
| raw Plaid store (source of truth, xz JSONL) | `transactions/data/transactions_raw.jsonl.xz` |
| durable reconciled store (Drive-synced) | `transactions/data/transactions.jsonl` |
| sync cursors / overfetch + reconcile state | `transactions/data/*.json(l)` |
| CSV deliverable | `transactions/transactions.csv` |
| categorized store + worklist + manual edits | `plaid_category_transformer/data/…` |
| personal analyzer config (accounts, budget) | `spend_analyzer/config/…` |
| corrections queue | `spend_analyzer/data/corrections.jsonl` |

## Components, entry points, tests

| Component | venv | Entry points | Tests |
|---|---|---|---|
| `transactions/` | `./venv` | `app.py --user <name>` (link), `fetch_transactions.py [--user]` (sync) | `./venv/bin/python -m pytest` |
| `persister/` | `./.venv` | library (`import persister`); `persist.py` CLI | `./.venv/bin/python -m pytest` |
| `plaid_category_transformer/` | `./.venv` | `categorize.py [--no-drive --no-llm --full --review --edit]` | `./.venv/bin/python -m pytest` |
| `spend_analyzer/` | `./venv` | `streamlit run app.py` | `pytest` (unit+UI); browser e2e opt-in: `pytest tests/e2e -m e2e` |
| `finance_pipeline/` | `./venv` | `./run.py [--no-drive --no-llm --no-ui]`; `tools/migrate_multiuser.py` | `./venv/bin/python -m pytest` |

Conventions: tests are named `given_<precondition>_when_<action>_then_<result>`
(pytest.ini adds `given_*`), except spend_analyzer which uses plain `test_*`.
Dependencies are pinned `==` in per-component requirements files AND hash-locked
(`requirements.lock.txt`, installed with `--require-hashes`; regenerate via
`pip-compile --generate-hashes --allow-unsafe` after a deliberate pin bump).
persister is installed editable (`--no-deps`, separate step — editables can't be
hash-pinned) into the venvs that use it. Plain stdlib in finance_pipeline.

## Data flow + invariants (don't rediscover these)

```
Plaid ──sync (cursor/Item)──▶ raw xz store ──persist/reconcile──▶ transactions.jsonl ──▶ Drive
                                   │                                      │
                                   └──────────── categorize ──────────────▶ transactions_categorized.jsonl ──▶ Drive
                                                                                  │
                                                                       spend_analyzer (read-only UI)
```

- **Multi-user**: each tokens.json entry carries an `owner` (set at link time by
  `app.py --user`). Every record is stamped `txn_owner` at BOTH fetch boundaries —
  `sync_item` (fetch_transactions.py) and `_fetch_window_items` (fetch_window.py,
  which covers the 90-day overfetch AND reconcile repair fetches) — so golden
  overwrites can never strip the stamp. `load_tokens()` hard-fails on owner-less
  entries (pre-migration data).
- **`txn_owner` is metadata, not content**: excluded from the transformer's
  `source_content_hash` (`incremental._NON_SOURCE_FIELDS`) so it never triggers
  re-audits, and passed as `metadata_fields=("txn_owner",)` to
  `persister.reconcile()` at both call sites (persist_runner, transformer drive
  gate) so it never reads as Drive drift. Any future metadata field needs the same
  two exclusions.
- **Dedupe/identity**: everything is keyed by Plaid's globally-unique
  `transaction_id`; sync cursors are per-Item; pending rows superseded by posted
  ones are dropped at persist (`dedupe_supersede`).
- **Plaid is golden, but never deletes**: overfetch adds/overwrites and only FLAGS
  stale rows; reconcile conflicts trigger a bounded repair fetch; unresolved
  conflicts STOP the run before anything is persisted or pushed.
- **Drive gates**: the transformer refuses to push over a diverged remote
  (`--force-push` to override); persister's DriveSync is append-only by
  construction (revisions, never delete) with file-id memory in
  `.secrets/drive_state.json` — local path moves don't affect Drive identity.
- **Categorization authority**: mechanical `auto` rules overwrite; the LLM only
  flags (configurable); manual edit intents (`manual_edits.jsonl`, append-only,
  replayed every run) are the highest authority. Corrections in the UI append
  intents — records are never edited in place by the UI.
- **CSV cells are formula-injection-escaped** (`'`-prefix on `=+-@\t\r`) everywhere
  CSVs are written.
- **2-year history** is fixed per-Item at link time (`days_requested=730`,
  transactions/src/app.py); the safety-net overfetch window is 90 days on a
  ~30-day cadence.

## Gotchas

- The analyzer imports the transformer's code via `transformer_root` (app.yaml) for
  intent validation/taxonomy — but the transformer's DATA paths come from the data
  root. Don't conflate them.
- `spend_analyzer/tests/ui` + `tests/e2e` run the REAL app over the REAL archive
  read-only; write paths are monkeypatched to tmp and `tests/_liveguard.py` fails
  any test that mutates live files. Preserve both patterns when adding UI tests.
- Joint accounts linked by both users double-count (two Plaid Items, different
  transaction_ids) — documented limitation; each shared account is linked by
  exactly one user.
- `PLAN.md` files are historical design docs; READMEs are the current truth.
- Component-level `CLAUDE.md` files hold per-component specifics.
