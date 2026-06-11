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
 2. categorize   plaid_category_         Drive divergence gate → incremental audit of
    │            transformer/            new/changed rows (mechanical rules + local Ollama
    │                                    LLM as flag-only reviewer) → transactions_
    │                                    categorized.{jsonl,csv} + flagged_for_review.csv
    │                                    → Drive push (3 files, new revisions)
    ▼
 3. analyze      spend_analyzer/         Streamlit UI over the CATEGORIZED store
                                         (http://localhost:8501) + default browser opened
```

**Stop-on-conflict:** the pipeline halts (non-zero exit, nothing further runs) when a
dataset conflicts with its Drive copy:

- **raw** — reconcile conflicts are first repaired from Plaid (the golden source, via a
  bounded `/transactions/get`); only conflicts Plaid *cannot confirm* stop the run,
  **before** the durable store or Drive is touched.
- **categorized** — there is no golden source for corrections, so *any* divergence
  (content conflicts or remote-only rows) stops the run before any audit or write.
  If the local store is the right one (e.g. after offline runs), re-run with
  `--force-push`.

## Flags

| flag | effect |
|---|---|
| `--no-drive` | fully offline: no Drive pull/reconcile/push in any component |
| `--no-llm` | skip the transformer's LLM review stage (mechanical rules still apply) |
| `--force-push` | override the transformer's divergence gate: local store wins |
| `--no-ui` | stop after the data steps (e.g. for scheduled runs) |
| `--no-browser` | serve the UI but don't open a browser |
| `--port N` | Streamlit port (default 8501) |

Interactive review of flagged categorizations is *not* part of the pipeline — run it
directly when you want to work the queue:
`cd plaid_category_transformer && ./.venv/bin/python categorize.py --review`
(paths relative to the monorepo root)

## First-run notes

- Banks must already be linked (`transactions/`: `./venv/bin/python app.py --user <name>`;
  each linked Item belongs to that user, and every fetched record is stamped with it
  as `txn_owner`).
- Data fetched before multi-user support has no owner stamps — back-fill once with
  `python3 tools/migrate_multiuser.py --dry-run` (then `--yes`). The fetch refuses
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
│   ├── pipeline.py   # preflight + the three steps + CLI
│   └── config.py     # where the component repos/venvs live
└── tests/            # offline: fake component repos in tmp dirs
```

## Tests

```bash
python3 -m venv venv && ./venv/bin/pip install -r requirements-dev.txt   # once
./venv/bin/python -m pytest -q
```

Fast, offline, deterministic: the components are replaced by fake executables in tmp
dirs that record how they were invoked; no network, no real data, no real Drive.

## Security posture

Follows the global baseline. The orchestrator itself holds no secrets and adds no
dependencies; Drive egress and the LLM are the components' own (notice-printing,
opt-out via `--no-drive` / `--no-llm`). The credential seeding copy is local-only and
re-applies `0600`/`0700` perms.
