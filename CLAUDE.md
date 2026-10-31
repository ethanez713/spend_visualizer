# CLAUDE.md — agent guide to spend_visualizer

This file is published with the repo. Personal or machine-specific notes go in the
gitignored `CLAUDE.local.md`, which Claude Code loads right after it.

Personal-finance monorepo: Plaid collector → category auditor → Streamlit UI, plus a
shared persistence library and an orchestrator. Five components, each with its OWN
venv, README, tests, and `.secrets/`. Code lives here; **all personal data lives
outside the repo** (see "Data root").

## Golden rules

1. **No live fetch, Drive push, or data-repo push by default — only with the user's
   explicit, in-session go-ahead.**

   *What counts as live:*
   - `fetch_transactions.py` / `app.py` — hit Plaid.
   - `--persist`/Drive-enabled runs, and `categorize.py` without `--no-drive` — push to
     Google Drive.
   - `run.py` — by default ALSO pushes the data repo to GitHub and, when a converter is
     configured, uploads a new Google Sheet (`--no-push-data` / `--no-sheet` turn them
     off). `deploy/bin/finance-daily.sh` pushes the data repo too.
   - `deploy/install.sh` — enables live timers.

   *Default to* verifying with the offline test suites and synthetic data, and handing
   live runs (`./run.py`) to the user.

   *When the user explicitly authorizes a live run in the current conversation* (e.g.
   "actually run `run.py`", "push to Drive"), you may execute it — that permission is
   scoped to that request/session, not standing: don't carry it into a later session or
   an unrelated command, and confirm scope if the ask is ambiguous.

   *Always fine:* local-only file operations. (Read-only local ops like the Streamlit
   analyzer UI over the existing archive were never restricted.)
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

Each component resolves this itself, deliberately duplicated so components stay
self-contained: `grep -rn _data_root`, plus `paths.py` in the transformer and an inline
snippet in `deploy/bin/finance-alert.sh`. A change to the order must reach all of them.

The optional **external converter** (Budget-tab categorization) is located by
`finance_pipeline/src/config.py:_converter_dir`: `$SPEND_VISUALIZER_CONVERTER`, else
the first non-comment line of `<data_root>/converter_root`, else None (step skipped).
A plain-text pointer, not YAML, because finance_pipeline is stdlib-only.

## Conventions

Each component has its own venv and tests: from its directory, `pyrun python -m pytest`.
spend_analyzer's browser e2e is opt-in (`pytest tests/e2e -m e2e`).

Tests are named `given_<precondition>_when_<action>_then_<result>`
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
- **Drive head adoption (categorized store)**: the store has TWO legitimate writers,
  serialized through Drive, and every Drive-enabled transformer run adopts the remote head
  first (rules in the transformer's CLAUDE.md). Simultaneous runs on both machines race the
  final push, so avoid them. persister's DriveSync is append-only by construction.
- **One GitHub pusher**: when a server runs the daily job, the server is the SOLE
  pusher of the finance_data repo; other machines `git pull` only and must run
  `run.py --no-push-data` (the push is on by default). Cross-machine data merging
  is the Drive reconcile's job, not git's.
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

- Joint accounts linked by both users double-count (two Plaid Items, different
  transaction_ids) — documented limitation; each shared account is linked by
  exactly one user.
- **Optional external converter → Budget tab**: when configured (see Data root),
  the pipeline's step 2b runs the converter's `refresh.py --all --no-upload` to
  write `budget_ledger.csv`; the analyzer's Budget tab reads it iff `ledger.csv`
  is set in the personal `budget.yaml` (else built-in tier1). Two separate
  switches (stdlib pipeline pointer vs analyzer YAML) by design — keep them
  pointing at the same CSV. `--no-upload` keeps it off the network; the step is
  non-fatal (derives a view). Invocation is one-directional (pipeline →
  converter): refresh.py never fetches — don't reintroduce a fetch there, it
  would recurse into run.py.
- **Incremental Sheet (step 2c, on by default when a converter is configured;
  `--no-sheet` skips it)**: a second converter invocation with upload ON creates
  a new Google Sheet with everything earlier Sheets don't hold, up to today, pending
  rows included; its URL (via the converter's `--url-file`) plus
  `<data_root>/pinned_tabs` URLs open as extra browser tabs after the UI.
  Non-fatal, runs outside the pipeline lock. Same live-run authorization rules as
  the rest of `run.py` (golden rule 1). The converter owns the bookkeeping (what each
  Sheet held, pending-row reconciliation, warning rows) in
  `<data_root>/sheet_exports/manifest.jsonl` — the orchestrator passes no window.
- `PLAN.md` files are historical design docs; READMEs are the current truth.
- Component-level `CLAUDE.md` files hold per-component specifics.
