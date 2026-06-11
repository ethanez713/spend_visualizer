"""Pytest bootstrap: ensure the repo root is importable (`import src.*`).

``pytest.ini`` sets ``pythonpath = .`` for this too; this file is kept so an ad-hoc
``pytest`` from any cwd still resolves the ``src`` package. No fixtures live here —
shared offline fixtures are in ``tests/conftest.py``.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
