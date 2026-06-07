#!/usr/bin/env bash
# Wrapper for scheduled runs: cd into the project, run the fetch with the venv
# Python, and append timestamped output to logs/fetch.log.
set -euo pipefail

cd "$(dirname "$0")"
mkdir -p logs

echo "===== $(date '+%Y-%m-%d %H:%M:%S %z') =====" >> logs/fetch.log
./venv/bin/python fetch_transactions.py >> logs/fetch.log 2>&1
