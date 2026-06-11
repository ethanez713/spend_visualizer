#!/usr/bin/env bash
# Launch the Spend Analyzer locally. Usage: ./run.sh  (optionally pass extra
# streamlit flags, e.g. ./run.sh --server.port 8600)
cd "$(dirname "$0")" && exec ./venv/bin/streamlit run app.py "$@"
