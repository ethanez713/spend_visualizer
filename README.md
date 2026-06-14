# spend_visualizer

Personal finance pipeline: fetch bank/card transactions from Plaid, audit and correct
their categories (deterministic rules, with periodic deep review by Claude — the local
LLM reviewer is off by default), and explore spending in a local Streamlit UI.
Multi-user: each Plaid Item is owned by a named person and every transaction is stamped
with its owner; the UI renders everyone's data as one dataset with per-person attribution
and filtering.

**Privacy by construction:** this repo contains code only. All personal financial data
lives OUTSIDE it under a configurable **data root** (see below), and all credentials
live in gitignored `.secrets/` directories — so the repo can be shared without exposing
anything. The core path is **offline**; Plaid is only called by the collector, Google
Drive sync is local/opt-in, and the local LLM reviewer is off by default (periodic deep
review is the opt-in Claude ritual — step 2 below).

## Everyday workflow

The end-to-end loop, in order. Steps are independent — run only what you need. Each
component runs under its **own** venv (build them once — see "Full setup"). The local LLM
is **off by default**, so categorization is purely deterministic rules unless you pass
`--llm`. (The raw Plaid store is INPUT only — no categorizer step ever writes it.)

**1 · Refresh data** — fetch new transactions → deterministic categorize → push to Drive:
```bash
cd finance_pipeline && ./run.py --no-ui          # add --push-data to also git-push the data repo; --no-drive to stay local
```

**2 · Audit with Claude** — in a Claude Code session at the repo, review records and flag
suspicious ones, run the quality sweeps, and propose rules (writes flags locally; pushes
nothing):
```bash
/audit-transactions          # new/unreviewed records only (default)
/audit-transactions full     # re-review the ENTIRE history (e.g. after a category/rule change)
```

**3 · Apply edits** — adjudicate and/or codify, which updates + uploads the categorized
store (Drive). Run whichever apply:
```bash
cd plaid_category_transformer
./.venv/bin/python categorize.py --review        # adjudicate Claude's flags: accept / reject / re-pick
./.venv/bin/python categorize.py --edit          # one-off manual fix (sticky intent), applied now
# systemic rule update: paste the audit's proposed rules into src/config.py, then:
./.venv/bin/python categorize.py --full          # re-categorize everything with the new rules
```

**4 · Open the UI** — read-only spending explorer over the categorized store:
```bash
cd spend_analyzer && ./venv/bin/streamlit run app.py   # binds 127.0.0.1:8501
```

> **One-shot** (no audit in between): `cd finance_pipeline && ./run.py` does **1 + 4** —
> refresh the data, then open the UI.

## Components

| Directory | Role |
|---|---|
| `finance_pipeline/` | Orchestrator — runs the components below in sequence, each under its own venv |
| `transactions/` | Plaid collector — incremental sync into a durable, Drive-replicated store |
| `persister/` | Shared library — durable, deduped, reconciled, Drive-synced JSONL stores |
| `plaid_category_transformer/` | Category auditor — deterministic rules + Claude-ritual review (flag-don't-overwrite; local LLM off by default) |
| `spend_analyzer/` | Streamlit UI — faceted spending analysis over the categorized archive |
| `deploy/` | Server artifacts — systemd units + runbook to run the stack as an unattended service |

Components locate each other by sibling-relative paths; don't rename the directories
independently. Each component has its own README with full details, and its own venv.

## Command reference (everyday use)

All paths are relative to the repo root; every component runs under its **own
venv** (build them once — see "Full setup" below). Each component's README has
the full flag list.

### The pipeline (one command, end to end)

| Task | Command |
|---|---|
| Full run: fetch → deterministic categorize → open the UI | `cd finance_pipeline && ./run.py` |
| Data steps only, no UI (refresh + push to Drive) | `./run.py --no-ui` |
| …also git-push the data-root repo to its remote | `./run.py --no-ui --push-data` |
| Fully offline (no Drive pull/push) | `./run.py --no-drive` |
| Opt IN to the local LLM reviewer (off by default; needs Ollama) | `./run.py --llm` |

### Transactions (Plaid collector)

| Task | Command |
|---|---|
| Link a new bank (one Flask session per user) | `cd transactions && ./venv/bin/python app.py --user <name>` |
| Fetch/sync only (incremental + Drive persist) | `cd transactions && ./venv/bin/python fetch_transactions.py` |
| Same, fully offline | `./venv/bin/python fetch_transactions.py --no-drive` |
| Re-link after a bank forces re-auth | same as linking; from a server: `ssh -L 5000:127.0.0.1:5000`, then run it there |

### Categorization (rules + LLM auditor)

| Task | Command |
|---|---|
| Categorize only (delta; deterministic rules, LLM off) | `cd plaid_category_transformer && ./.venv/bin/python categorize.py` |
| Claude audit ritual (export → judge → apply) | `/audit-transactions` skill, or `categorize.py --claude-export` / `--claude-apply` |
| Work the review queue — adjudicate flags (interactive) | `./.venv/bin/python categorize.py --review` |
| Capture a manual one-off edit (interactive) | `./.venv/bin/python categorize.py --edit` |
| Full re-audit (after editing `src/config.py` rules) | `./.venv/bin/python categorize.py --full` |
| Opt IN to the local LLM for this run | `./.venv/bin/python categorize.py --llm` |
| Mine manual edits for rule improvements | `./.venv/bin/python analyze_edits.py` |

### The UI (spend analyzer)

| Task | Command |
|---|---|
| Launch the UI alone, locally | `cd spend_analyzer && ./venv/bin/streamlit run app.py` |
| (it binds `127.0.0.1:8501` and reads the categorized store read-only) | |

### Run it as a service (server)

Bring-up is the ordered runbook: [`deploy/RUNBOOK.md`](deploy/RUNBOOK.md). Day-to-day:

| Task | Command |
|---|---|
| Install + enable UI service & daily timer | `./deploy/install.sh` (on the server, after RUNBOOK §§1–7) |
| What the timer runs each morning | `deploy/bin/finance-daily.sh` → `run.py --no-ui --no-llm --push-data` |
| Open the UI | `https://<server>.<tailnet>.ts.net` (tailscale serve → loopback 8501) |
| Did last night's run work? | `journalctl --user -u finance-daily -n 50` |
| Pause / resume the schedule | `systemctl --user stop finance-daily.timer` / `start` |
| Remove the service | `./deploy/uninstall.sh` |

**Two-machine rule of thumb:** the categorized store and intent log rebase onto
the Drive head at every run start, so the server's daily job and an occasional
desktop Claude audit/review run coexist safely — just don't run both at the same
moment, and only the server pushes `finance_data` to GitHub (RUNBOOK §12).

## Where the data lives (the data root)

All stores, state, and personal config resolve to one external directory — the
**data root** — found via, in priority order:

1. the `SPEND_VISUALIZER_DATA` environment variable,
2. the first non-comment line of the [`data_root`](data_root) file at this repo's root,
3. the default `~/finance_data`.

Inside, it mirrors the monorepo layout (`transactions/data/…`,
`plaid_category_transformer/data/…`, `spend_analyzer/config/…`). Make it a **private
git repo** so pipeline runs get a local audit history:

```bash
mkdir -p -m 700 ~/finance_data && cd ~/finance_data && git init
```

Secrets do **not** live there: Plaid credentials/tokens and Drive credentials stay in
each component's gitignored `.secrets/` (0700, files 0600), machine-local only.

## Full setup for a new user

### 0. Clone + per-component venvs

Every component installs from its **hash-locked** `requirements.lock.txt`
(`--require-hashes` verifies each downloaded artifact against pinned SHA-256s —
supply-chain hardening). The sibling `persister` library is installed editable in a
separate `--no-deps` step because editables can't be hash-pinned; its dependencies
are already in each consumer's lock.

```bash
git clone <this repo> spend_visualizer && cd spend_visualizer

(cd persister && python3 -m venv .venv && ./.venv/bin/pip install --require-hashes -r requirements.lock.txt && ./.venv/bin/pip install --no-deps -e .)
(cd transactions && python3 -m venv venv && ./venv/bin/pip install --require-hashes -r requirements.lock.txt && ./venv/bin/pip install --no-deps -e ../persister)
(cd plaid_category_transformer && python3 -m venv .venv && ./.venv/bin/pip install --require-hashes -r requirements.lock.txt && ./.venv/bin/pip install --no-deps -e ../persister)
(cd spend_analyzer && python3 -m venv venv && ./venv/bin/pip install --require-hashes -r requirements.lock.txt)
(cd finance_pipeline && python3 -m venv venv && ./venv/bin/pip install --require-hashes -r requirements.lock.txt)
```

After a deliberate pin bump in a component's `requirements*.txt`, regenerate its lock:
`pip install pip-tools && pip-compile --generate-hashes --allow-unsafe
<the component's requirements*.txt files> -o requirements.lock.txt`.

Create the data root (step above), or edit `data_root` to point somewhere else.

### 1. Plaid account (required)

Plaid is the bank-data API; you need your own (free) developer account:

1. Sign up at **<https://dashboard.plaid.com/signup>** (choose "personal project" /
   any team name — no company needed).
2. In the dashboard, grab your **client_id** and the **Sandbox secret** from
   *Developers → Keys*.
3. For real banks, request **Production access** (*Settings → Activate Production*).
   You'll answer a short questionnaire (use case: personal finance, data: Transactions).
   Approval is typically quick; pay-as-you-go pricing has a free monthly allotment
   that personal use stays well inside. Note: the number of bank connections
   ("Items") is capped — link only banks you need, and know that **deleting an Item
   does not free its slot**.
4. Put the credentials in `transactions/.secrets/.env` (format in
   `transactions/.env.example`); `chmod 700 transactions/.secrets`, `chmod 600` the file:

   ```
   PLAID_CLIENT_ID=...
   PLAID_SECRET=...          # Sandbox secret first; switch to Production when approved
   PLAID_ENV=sandbox         # then: production
   ```

To dry-run the entire pipeline on fake data first, keep `PLAID_ENV=sandbox`, link any
bank with Plaid's test credentials `user_good` / `pass_good`, and point
`SPEND_VISUALIZER_DATA` at a throwaway dir (see `transactions/README.md` → "Testing in
sandbox first").

### 2. Google Drive sync (optional but recommended)

Gives the two durable stores an off-machine, append-only revision history. Skip it
entirely by always running with `--no-drive`.

1. <https://console.cloud.google.com> → create a project → *Enable APIs* → **Google
   Drive API**.
2. *OAuth consent screen*: External, add yourself as a **Test user**.
3. *Credentials → Create credentials → OAuth client ID → Desktop app* → download the
   JSON → save as `transactions/.secrets/client_secret.json` (0600).
4. First Drive-enabled run prints an auth URL; open it, approve, done — the token is
   cached. The pipeline copies the credentials to the transformer's `.secrets/`
   automatically. Scope is `drive.file` (least privilege: the app only ever sees files
   it created).

### 3. Link banks (one server per user)

```bash
cd transactions && ./venv/bin/python app.py --user <name>     # e.g. --user Alice
```

Open <http://127.0.0.1:5000/>, connect each of that person's banks, Ctrl+C. Re-run
with the other `--user` for the second person's banks. Every Item linked belongs to
that user; all of their transactions are stamped with it (`txn_owner`), and the
initial sync pulls the full 24 months of history.

### 4. First run

```bash
cd finance_pipeline && ./run.py        # or --no-drive / --no-llm to start simpler
```

Then map accounts to friendly names in `<data root>/spend_analyzer/config/accounts.yaml`
(bootstrap it with `cd spend_analyzer && ./venv/bin/python -m tools.gen_accounts`;
`person` is pre-seeded from the owner stamps) and optionally set budget goals in
`<data root>/spend_analyzer/config/budget.yaml`.

For the LLM review stage, install [Ollama](https://ollama.com) and `ollama pull
qwen2.5:7b`; without it the pipeline still runs (mechanical rules only, warning printed).

## Tests

Every component is offline-tested under its own venv:

```bash
(cd persister && ./.venv/bin/python -m pytest)
(cd transactions && ./venv/bin/python -m pytest)
(cd plaid_category_transformer && ./.venv/bin/python -m pytest)
(cd spend_analyzer && ./venv/bin/python -m pytest)            # + browser e2e: pytest tests/e2e -m e2e
(cd finance_pipeline && ./venv/bin/python -m pytest)
(cd deploy && ./.venv/bin/python -m pytest)                   # server-artifact sanity checks
```

No test ever touches the network, Plaid, Drive, or your real data (the analyzer's UI
suite reads the live archive read-only and guards it with before/after digests).

## For AI agents

See [`CLAUDE.md`](CLAUDE.md) — architecture map, invariants, conventions, and the
rules for working in this repo without touching live data.
