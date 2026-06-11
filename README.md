# spend_vizualiser

Personal finance app: fetch bank/card transactions from Plaid, audit and correct their
categories, and explore spending in a local web UI. Formerly five separate repos,
consolidated here as one monorepo with each component kept as its own subdirectory.

## Components

| Directory | Role |
|---|---|
| `finance_pipeline/` | Orchestrator — runs the components below in sequence, each under its own venv |
| `transactions/` | Plaid collector — incremental sync into a durable, Drive-replicated store |
| `persister/` | Shared library — durable, deduped, reconciled, Drive-synced JSONL stores |
| `plaid_category_transformer/` | Category auditor — rules + local LLM flag-don't-overwrite pipeline |
| `spend_analyzer/` | Streamlit UI — faceted spending analysis over the categorized archive |

## Running

The everyday entry point is the orchestrator:

```bash
cd finance_pipeline && ./run.py
```

See each component's README for component-level usage, setup, and flags. Components
locate each other by sibling-relative paths, so the directory names above must not be
renamed independently.

## Privacy

`plaid_category_transformer/data/` deliberately commits real financial data (a dual
git + Drive audit trail). This repository must remain **private**. Secrets live in each
component's gitignored `.secrets/` directory (0700/0600), never in git.
