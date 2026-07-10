# finance_pipeline

**The end-to-end entry point** for the personal-finance stack. One command fetches
everything new from Plaid, audits/corrects the categories, persists both the raw and
the categorized stores (locally **and** to Google Drive), then serves the Spend
Analyzer UI and opens it in your default browser:

```bash
./run.py
```

This component is deliberately thin — just preflight + choreography over the sibling
component directories, which own all the domain logic. It needs **only the Python standard
library**: each component runs as a subprocess under its **own venv**, from its own
repo root.

## What a run does

```
 1. fetch        transactions/           /transactions/sync (cursor-based: ALL new/changed
    │                                    rows since the last run) → raw xz archive →
    │                                    reconcile vs Drive remote (Plaid = golden repair)
    │                                    → durable store in its own data/ → Drive push
    │                                    (transactions.jsonl + transactions.csv revisions)
    ▼
 2. categorize   plaid_category_         adopt Drive head (other machine's audits +
    │            transformer/            intent log) → incremental audit of new/changed
    │                                    rows (mechanical rules + local Ollama LLM as
    │                                    flag-only reviewer) → transactions_categorized
    │                                    .{jsonl,csv} + flagged_for_review.csv
    │                                    → Drive push (new revisions)
    ▼
2b. convert      converter/  (optional)  regenerate the budget ledger from the categorized
    │                                    store (refresh.py --all --no-upload):
    │                                    PFC → the established budget's categories →
    │                                    <data_root>/spend_analyzer/data/budget_ledger.csv.
    │                                    Skipped unless a converter is configured; local-only
    │                                    and non-fatal (it only derives a view).
    ▼
2c. sheet        converter/  (--sheet)   convert the chosen month and upload it as a new
    │                                    Google Sheet (explicit opt-in egress); the Sheet
    │                                    URL comes back via --url-file and opens as an
    │                                    extra browser tab, plus any URLs pinned in
    │                                    <data_root>/pinned_tabs. Non-fatal.
    ▼
 3. analyze      spend_analyzer/         Streamlit UI over the CATEGORIZED store
                                         (http://localhost:8501) + default browser opened
```

**Drive safety model:**

- **raw** — reconcile conflicts are first repaired from Plaid (the golden source, via a
  bounded `/transactions/get`); only conflicts Plaid *cannot confirm* stop the run
  (non-zero exit), **before** the durable store or Drive is touched.
- **categorized** — two machines may legitimately write it (a scheduled server run and
  occasional desktop LLM runs), so each Drive-enabled run starts by **adopting the
  Drive head**: remote-only rows and conflicting rows take the remote value, local-only
  rows are kept, and the manual-edits intent log is union-merged alongside. A pull
  failure stops the run; `--force-push` skips adoption and declares the local store
  authoritative (e.g. restoring from a known-good local copy after offline runs).

## Flags

| flag | effect |
|---|---|
| `--no-drive` | fully offline: no Drive pull/reconcile/push in any component |
| `--no-llm` | skip the transformer's LLM review stage deliberately (rules still apply; rows stamp as fully audited) |
| `--llm-defer` | server mode: rules-only now, rows stay LLM-pending so a later LLM-enabled run audits them (mutually exclusive with `--no-llm`) |
| `--force-push` | skip the transformer's Drive head adoption: local store wins |
| `--no-convert` | skip the optional budget-ledger regen even if a converter is configured |
| `--sheet` | monthly ritual: also run the converter's Google-Sheet upload (opt-in egress) and open the fresh Sheet + `<data_root>/pinned_tabs` URLs as extra browser tabs |
| `--sheet-month YYYY-MM` | month window for `--sheet` (default: current calendar month) |
| `--no-ui` | stop after the data steps (e.g. for scheduled runs) |
| `--push-data` | after the data steps: commit the data-root git repo (if dirty) and push to its `origin` — explicit opt-in upload (no remote ⇒ warn, commit locally) |
| `--no-browser` | serve the UI but don't open a browser |
| `--port N` | Streamlit port (default 8501) |

The data steps (and `--push-data`) run under an exclusive lock on
`<data_root>/.pipeline.lock`, so a scheduled run and a manual one can't
interleave on the same machine; a second run exits immediately with a clear
message. The lock is released before the UI step. Scheduled/server runs are
`deploy/`'s job — see `deploy/RUNBOOK.md`.

Interactive review of flagged categorizations is *not* part of the pipeline — run it
directly when you want to work the queue:
`cd plaid_category_transformer && ./.venv/bin/python categorize.py --review`
(paths relative to the monorepo root)

## Optional: external budget ledger (step 2b)

If you keep your budget in a *different* category set than this stack's built-in
`tier1` taxonomy, point the pipeline at an external **converter** project that maps the
categorized store onto those categories. When configured, the pipeline runs the
converter after categorize (inside the lock, before `--push-data`) to regenerate
`<data_root>/spend_analyzer/data/budget_ledger.csv`, which the Spend Analyzer's Budget
tab reads when `ledger.csv` is set in its `budget.yaml` (see spend_analyzer's README).

- **Configure it** with `$SPEND_VISUALIZER_CONVERTER`, or the first non-comment line of
  `<data_root>/converter_root` — a plain-text pointer (this orchestrator stays
  stdlib-only, so the pointer lives outside the analyzer's YAML). Both live in the
  private data root, so this repo stays generic. Unset ⇒ the step is simply skipped.
- The step runs the converter as `refresh.py --all --no-upload` under its own venv: no
  Google-Sheet egress, and no fetch — the converter never fetches; this pipeline *is*
  its upstream (invocation is deliberately one-directional, pipeline → converter). It is
  **non-fatal** — a converter hiccup warns loudly and the run continues (it derives a
  view; it never touches the source stores). `--no-convert` skips it outright.

### `--sheet`: the monthly ritual (step 2c)

`./run.py --sheet` additionally runs the converter in its month-Sheet mode
(`refresh.py --url-file …`, upload ON — the explicit opt-in egress), then opens **all**
the month-in-review tabs at once: the Streamlit UI, the freshly uploaded Google Sheet,
and every URL listed in `<data_root>/pinned_tabs` (one per line, `#` comments allowed —
e.g. the master budget spreadsheet; personal URLs, so the file lives in the private
data root). The step runs *outside* the pipeline lock (it's read-only over the store)
and is non-fatal. A `run_finances` wrapper script on the PATH that just executes
`run.py --sheet "$@"` makes this a single command.

## First-run notes

- Banks must already be linked (`transactions/`: `./venv/bin/python app.py --user <name>`;
  each linked Item belongs to that user, and every fetched record is stamped with it
  as `txn_owner`).
- Data fetched before multi-user support has no owner stamps — back-fill once with
  `python3 tools/migrate_multiuser.py --owner <name> --dry-run` (then `--yes`). The fetch refuses
  to run on un-stamped tokens until then.
- Google Drive credentials must exist in `transactions/.secrets/` (see persister's
  README for the one-time OAuth setup; persister itself is a pure library and holds
  no state). Preflight seeds `plaid_category_transformer/.secrets/` from there
  (local copy, `0600`) so the transformer can push too.
- If Ollama isn't running, the LLM stage is skipped gracefully (warning printed);
  `ollama serve` + the `qwen2.5:7b` model enable full audits.
- The first categorize run audits the whole history through the local LLM —
  expect minutes, not seconds. Later runs only audit the delta.

## Layout

```
.
├── run.py            # entry-point shim → src/pipeline.py
├── src/
│   ├── pipeline.py   # preflight + the steps (incl. optional convert) + CLI + the lock
│   ├── git_push.py   # --push-data: snapshot-commit + push the data-root repo
│   └── config.py     # where the component repos/venvs, data root, + optional converter live
└── tests/            # offline: fake component repos + a local bare "GitHub" in tmp dirs
```

## Tests

```bash
python3 -m venv venv && ./venv/bin/pip install --require-hashes -r requirements.lock.txt   # once
./venv/bin/python -m pytest -q
```

Fast, offline, deterministic: the components are replaced by fake executables in tmp
dirs that record how they were invoked; no network, no real data, no real Drive.

## Security posture

Follows the global baseline. The orchestrator itself holds no secrets and adds no
dependencies; Drive egress and the LLM are the components' own (notice-printing,
opt-out via `--no-drive` / `--no-llm`). The credential seeding copy is local-only and
re-applies `0600`/`0700` perms.
