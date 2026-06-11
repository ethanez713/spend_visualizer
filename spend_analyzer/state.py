"""Session-scoped UI state: hidden categories + the corrections queue.

Both are *view-layer* state — they never mutate the underlying records (PLAN.md
spirit; explicit user requirement). Hidden items are excluded from aggregates
but left as visual indicators; corrections are collected into a report.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

HIDDEN_KEY = "hidden_rules"          # list[{"dim","value","label"}]


# --------------------------------------------------------------------- hiding
def hidden_rules() -> list[dict]:
    return st.session_state.setdefault(HIDDEN_KEY, [])


def is_hidden(dim: str, value) -> bool:
    return any(r["dim"] == dim and r["value"] == value for r in hidden_rules())


def hide(dim: str, value, label: str | None = None) -> None:
    if not is_hidden(dim, value):
        hidden_rules().append({"dim": dim, "value": value, "label": label or f"{dim}={value}"})


def unhide(dim: str, value) -> None:
    st.session_state[HIDDEN_KEY] = [
        r for r in hidden_rules() if not (r["dim"] == dim and r["value"] == value)
    ]


def toggle_hidden(dim: str, value, label: str | None = None) -> None:
    if is_hidden(dim, value):
        unhide(dim, value)
    else:
        hide(dim, value, label)


def clear_hidden() -> None:
    st.session_state[HIDDEN_KEY] = []


def hidden_mask(df: pd.DataFrame, rules: list[dict] | None = None) -> pd.Series:
    """Boolean Series: True where a row matches any active hide rule."""
    rules = rules if rules is not None else hidden_rules()
    mask = pd.Series(False, index=df.index)
    for r in rules:
        dim, val = r["dim"], r["value"]
        if dim in df.columns:
            mask = mask | (df[dim] == val)
    return mask
