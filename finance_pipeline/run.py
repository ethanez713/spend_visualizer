#!/usr/bin/env python3
"""Entry-point shim → src.pipeline:main (house layout: real code lives in src/).

Run the whole pipeline:   ./run.py            (or: python3 run.py) — fetch, categorize,
                          data-repo push, last complete month's Google Sheet, UI
Useful flags:             --no-drive --no-llm --llm-defer --no-ui --no-push-data
                          --no-sheet --sheet-month YYYY-MM --no-browser --port N
Needs only the standard library — each component runs under its own venv.
"""
from src.pipeline import main

if __name__ == "__main__":
    main()
