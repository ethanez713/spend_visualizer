"""Root shim → persister CLI (standalone, mostly for testing / manual ops).

The real callers (`transactions`, `plaid_category_transformer`) import the library
API directly. This shim just exposes the CLI:

    ./.venv/bin/python persist.py window    --store data/transactions.jsonl
    ./.venv/bin/python persist.py reconcile --store data/transactions.jsonl
    ./.venv/bin/python persist.py push      --store data/transactions.jsonl

Works whether or not the package is `pip install -e .`'d: falls back to the
local src/ tree if `persister` isn't on the path yet.
"""
try:
    from persister.cli import main
except ModuleNotFoundError:  # not pip-installed — run straight from the source tree
    import os
    import sys

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
    from persister.cli import main

if __name__ == "__main__":
    main()
