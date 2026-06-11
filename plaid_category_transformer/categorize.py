"""Root entry-point shim → src.transformer:main.

    ./.venv/bin/python categorize.py [--input ...] [--no-drive] [--no-llm]

All real code lives in ``src/`` (run via this thin shim, matching the house layout of
``transactions`` / ``converter``). See ``src/transformer.py`` and ``PLAN.md``.
"""
from src.transformer import main

if __name__ == "__main__":
    main()
