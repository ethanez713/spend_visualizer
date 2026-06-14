#!/usr/bin/env python3
"""Entry-point shim → src.pipeline:main (house layout: real code lives in src/).

Run the whole pipeline:   ./run.py            (or: python3 run.py)
Useful flags:             --no-drive --no-llm --llm-defer --no-ui --push-data --no-browser --port N
Needs only the standard library — each component runs under its own venv.
"""
from src.pipeline import main

if __name__ == "__main__":
    main()
