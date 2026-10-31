"""Opening one page from a row on another: a pick writes a pending navigation, and the target
page applies and clears it before creating its widgets."""
from __future__ import annotations

import streamlit as st

_PAGES_KEY = "_pages"
NAV_KEY = "_nav"


def register(pages: dict) -> None:
    st.session_state[_PAGES_KEY] = pages


def open_spending(*, focus: list | None = None, search: str = "", budget_cat: str | None = None,
                  period: tuple[str, str] | None = None, period_label: str = "") -> None:
    st.session_state[NAV_KEY] = {"page": "Spending", "focus": focus or [],
                                 "filters": {"search": search, "budget_cat": budget_cat},
                                 "period": (*period, period_label) if period else None}
    st.switch_page(st.session_state[_PAGES_KEY]["Spending"])


def picked_row(event) -> int | None:
    """The row a single-row st.dataframe selection holds, if any."""
    try:
        rows = event.selection["rows"]
    except (AttributeError, KeyError, TypeError):
        return None
    return rows[0] if rows else None
