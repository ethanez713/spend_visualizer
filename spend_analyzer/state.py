"""UI state: hidden categories (persisted) + helpers.

Hidden rules are *view-layer* state — they never mutate the underlying records
(PLAN.md spirit; explicit user requirement). Hidden items are excluded from
aggregates but left as visual indicators.

Persistence: hide rules survive reloads/restarts by backing to a small JSON under
the data root (``spend_analyzer/data/hidden_rules.json``, 0600) — the same place
the corrections queue lives, and NOT in this repo. ``st.session_state`` is the
in-session working copy; disk is the durable backing, rewritten on every change.
``SPEND_ANALYZER_DATA_DIR`` redirects the store (tests/e2e point it at a throwaway
dir), exactly like ``corrections``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pandas as pd
import streamlit as st

from config_io import DATA_ROOT

HIDDEN_KEY = "hidden_rules"          # list[{"dim","value","label"}]

# Test hook: e2e/UI runs redirect this at a tmp dir so a running app process can
# never write the live hide-rules file (mirrors corrections.DATA_DIR / STORE).
DATA_DIR = Path(os.environ.get("SPEND_ANALYZER_DATA_DIR",
                               DATA_ROOT / "spend_analyzer" / "data"))
STORE = DATA_DIR / "hidden_rules.json"


# --------------------------------------------------------------- persistence
def _load() -> list[dict]:
    """Read the durable hide rules; [] if missing or unreadable (fail-soft)."""
    if not STORE.exists():
        return []
    try:
        data = json.loads(STORE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    # Keep only well-formed rule dicts — a hand-edited file shouldn't crash the app.
    return [r for r in data if isinstance(r, dict) and "dim" in r and "value" in r]


def _save(rules: list[dict]) -> None:
    """Write the hide rules durably (owner-only), best-effort."""
    try:
        DATA_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        STORE.write_text(json.dumps(rules, indent=2), encoding="utf-8")
        os.chmod(STORE, 0o600)
    except OSError:
        pass                                    # a read-only FS must not break the UI


# --------------------------------------------------------------------- hiding
def hidden_rules() -> list[dict]:
    """The active hide rules, loaded from disk once per session then cached."""
    if HIDDEN_KEY not in st.session_state:
        st.session_state[HIDDEN_KEY] = _load()
    return st.session_state[HIDDEN_KEY]


def is_hidden(dim: str, value) -> bool:
    return any(r["dim"] == dim and r["value"] == value for r in hidden_rules())


def hide(dim: str, value, label: str | None = None) -> None:
    if not is_hidden(dim, value):
        hidden_rules().append({"dim": dim, "value": value, "label": label or f"{dim}={value}"})
        _save(hidden_rules())


def unhide(dim: str, value) -> None:
    st.session_state[HIDDEN_KEY] = [
        r for r in hidden_rules() if not (r["dim"] == dim and r["value"] == value)
    ]
    _save(st.session_state[HIDDEN_KEY])


def toggle_hidden(dim: str, value, label: str | None = None) -> None:
    if is_hidden(dim, value):
        unhide(dim, value)
    else:
        hide(dim, value, label)


def clear_hidden() -> None:
    st.session_state[HIDDEN_KEY] = []
    _save([])


def hidden_mask(df: pd.DataFrame, rules: list[dict] | None = None) -> pd.Series:
    """Boolean Series: True where a row matches any active hide rule."""
    rules = rules if rules is not None else hidden_rules()
    mask = pd.Series(False, index=df.index)
    for r in rules:
        dim, val = r["dim"], r["value"]
        if dim in df.columns:
            mask = mask | (df[dim] == val)
    return mask
