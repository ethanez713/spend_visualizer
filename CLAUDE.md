# CLAUDE.md — agent guide to spend_visualizer

Personal-finance monorepo: Plaid collector → category auditor → Streamlit UI, plus a
shared persistence library and an orchestrator. Five components, each with its OWN
venv, README, tests, and `.secrets/`. Code lives here; **all personal data lives
outside the repo** (see "Data root").

## Golden rules

1. **Never run a live fetch or Drive push.** `fetch_transactions.py` / `app.py` hit
   Plaid; `--persist`/Drive-enabled runs and `categorize.py` without `--no-drive`
   push to Google Drive; `--push-data` (and `deploy/bin/finance-daily.sh`, which
   passes it) pushes the data repo to GitHub; `deploy/install.sh` enables live
   timers. Verify with the offline test suites and synthetic data; hand live runs
   (`./run.py`) to the user. Local-only file operations are fine.
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
`finance_pipeline/tools/migrate_multiuser.py:_data_root`,
`finance_pipeline/src/config.py:_data_root`, and an inline snippet in
`deploy/bin/finance-alert.sh`.

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
| `plaid_category_transformer/` | `./.venv` | `categorize.py [--no-drive --llm --llm-defer --full --review --edit --claude-export --claude-apply]` (local LLM OFF by default — Claude ritual is the reviewer) | `./.venv/bin/python -m pytest` |
| `spend_analyzer/` | `./venv` | `streamlit run app.py` | `pytest` (unit+UI); browser e2e opt-in: `pytest tests/e2e -m e2e` |
| `finance_pipeline/` | `./venv` | `./run.py [--no-drive --llm --llm-defer --no-ui --push-data]`; `tools/migrate_multiuser.py` | `./venv/bin/python -m pytest` |
| `deploy/` | `./.venv` | static server artifacts — units/scripts are user-run only (see its CLAUDE.md) | `./.venv/bin/python -m pytest` |

Conventions: tests are named `given_<precondition>_when_<action>_then_<result>`
(pytest.ini adds `given_*`), except spend_analyzer which uses plain `test_*`.
Dependencies are pinned `==` in per-component requirements files AND hash-locked
(`requirements.lock.txt`, installed with `--require-hashes`; regenerate via
`pip-compile --generate-hashes --allow-unsafe` after a deliberate pin bump).
persister is installed editable (`--no-deps`, separate step — editables can't be
hash-pinned) into the venvs that use it. Plain stdlib in finance_pipeline.

Skill: when asked to audit/clean up categorization or do the periodic spending
review, follow the project-local `audit-transactions` skill
(`.claude/skills/audit-transactions/`) — it wraps `categorize.py --claude-export`
→ one Claude judgment pass (review + sweeps + rule proposals) → `--claude-apply`
→ `--review`. It's `disable-model-invocation` (deliberate egress), so suggest
`/audit-transactions` rather than auto-running it.

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
- **Drive head adoption (categorized store)**: the store has TWO legitimate
  writers (scheduled server run + occasional desktop Claude audit/review runs),
  serialized through Drive — every Drive-enabled transformer run STARTS by adopting the
  remote head: remote-only rows taken, local-only kept, conflicts resolved by
  **newest `category_audited_at` stamp** (local-ahead work never loses; ties →
  remote), with BOTH versions of every conflict appended to
  `…/data/adopt_conflicts.jsonl` (git-pushed daily — nothing silently
  discarded). The stamp moves only on real audit-content changes (see the
  transformer CLAUDE.md's stamp invariant). The manual-edits intent log is
  union-merged in the same step (a stale local log would otherwise revert the
  other machine's corrections at replay). Pull failure stops the run;
  `--force-push` skips adoption (local authoritative). Simultaneous runs on both
  machines race the final push — avoid (self-converges, but wastes LLM work).
  persister's DriveSync is append-only by construction (revisions, never delete)
  with file-id memory in `.secrets/drive_state.json` — local path moves don't
  affect Drive identity.
- **One GitHub pusher**: when a server runs the daily job (`--push-data`), the
  server is the SOLE pusher of the finance_data repo; other machines `git pull`
  only. Cross-machine data merging is the Drive reconcile's job, not git's.
  Same-machine run overlap is guarded by `<data_root>/.pipeline.lock` (flock,
  held for the data steps, released before the UI).
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
