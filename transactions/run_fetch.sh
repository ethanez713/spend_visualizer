#!/usr/bin/env bash
# Wrapper for scheduled runs: cd into the project, run the fetch with the venv
# Python, and append timestamped output to <data root>/transactions/data/logs/fetch.log.
# NOTE: superseded for server deployments by deploy/bin/finance-daily.sh, which
# runs the full data path (fetch → categorize → Drive + GitHub push) under
# systemd — this script only fetches. See deploy/RUNBOOK.md.
set -euo pipefail

cd "$(dirname "$0")"

# Resolve the data root the same way src/plaid_client.py does (env var, else the
# monorepo-root `data_root` file, else ~/finance_data) — logs live with the data.
LOG_DIR=$(./venv/bin/python -c "from src.plaid_client import DATA_DIR; print(DATA_DIR / 'logs')")
mkdir -p "$LOG_DIR"

echo "===== $(date '+%Y-%m-%d %H:%M:%S %z') =====" >> "$LOG_DIR/fetch.log"
./venv/bin/python fetch_transactions.py >> "$LOG_DIR/fetch.log" 2>&1
