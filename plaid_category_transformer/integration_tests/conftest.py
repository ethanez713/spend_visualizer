"""Shared helpers for the Ollama-gated LLM integration tests.

These tests hit a real local LLM (``qwen2.5:7b`` via Ollama). They are slow and
non-deterministic at the margins, so they live OUTSIDE ``tests/`` (the fast offline
suite). Each test module skips itself at import when Ollama isn't up. Run explicitly:

    ./.venv/bin/python -m pytest integration_tests -s
"""
import urllib.request

from src.llm import LLM_HOST


def ollama_up() -> bool:
    try:
        urllib.request.urlopen(f"{LLM_HOST}/api/tags", timeout=3)
        return True
    except Exception:
        return False
