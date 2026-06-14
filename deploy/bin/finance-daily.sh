#!/usr/bin/env bash
# Daily server run, invoked by finance-daily.service (or by hand for a dry-run
# shape check — note this DOES hit Plaid/Drive/GitHub; it is the live job).
#
#   --no-ui      data steps only — the UI is spend-analyzer.service's job
#   --no-llm     deterministic rules only, rows stamped FINAL. The local LLM is
#                off by default; deep review is the occasional desktop Claude
#                audit ritual (RUNBOOK.md §12). Passed explicitly so the server
#                stays rules-only even if the default ever flips back on.
#   --push-data  commit + push the finance_data repo to GitHub after success
#
# Drive push stays ON (the default). Output goes to stdout → journald:
#   journalctl --user -u finance-daily
# Overlap safety: the pipeline itself holds <data_root>/.pipeline.lock.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")/../../finance_pipeline"

echo "━━━ finance-daily $(date '+%Y-%m-%d %H:%M:%S %Z') ━━━"
exec python3 run.py --no-ui --no-llm --push-data
