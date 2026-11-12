# Using spend_visualizer

The everyday loop, every command, and running it as a service. First-time setup is in
[SETUP.md](SETUP.md).

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

## Command reference

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
| …plus the incremental Google-Sheet export + extra tabs (optional converter) | `./run.py --sheet` |

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
