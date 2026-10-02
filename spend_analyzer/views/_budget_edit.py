"""Budget edits from the UI: set a budget category, exclusion or note on the rows a view
shows, as intents in the shared log (see ``budget_edits``)."""
from __future__ import annotations

import pandas as pd
import streamlit as st

import budget_edits
import manual_edits
from views._widgets import all_raw_records, raw_record

KEEP_CATEGORY = "Keep the category (note only)"
EXCLUDE = "Not budget spending"
MAX_ROWS = 500


def bulk_panel(rows: pd.DataFrame, *, search: str, categories: list[str], key: str) -> None:
    """A panel under a transaction table that applies one budget edit to the rows shown,
    to every row of their merchant, or to every row whose description holds the search."""
    err = manual_edits.status()
    if err:
        st.caption(f"Budget edits unavailable: {err}")
        return
    with st.expander(f"Set the budget category for these {len(rows)} rows"):
        choice = st.selectbox("Budget category", [KEEP_CATEGORY, *categories, EXCLUDE],
                              key=f"{key}_category")
        note = st.text_input("Note", key=f"{key}_note",
                             placeholder="Optional, e.g. the trip it belongs to")
        scopes = _scopes(rows, search)
        scope = st.radio("Apply to", list(scopes), format_func=scopes.get, key=f"{key}_scope")
        category = None if choice in (KEEP_CATEGORY, EXCLUDE) else choice
        changing = _rows_changing(rows, category, choice == EXCLUDE)
        st.caption(f"{changing} of the {len(rows)} rows shown would change budget category."
                   if choice != KEEP_CATEGORY else "Only the note is written.")
        if st.button("Apply", key=f"{key}_apply", type="primary"):
            _apply(rows, scope, category=category, exclude=choice == EXCLUDE, note=note,
                   phrase=search)


def _scopes(rows: pd.DataFrame, search: str) -> dict[str, str]:
    scopes = {"transaction": f"just these {len(rows)} rows"}
    merchants = rows["merchant"].dropna().unique()
    if len(merchants) == 1 and _names_a_merchant(rows.iloc[0]["transaction_id"]):
        scopes["merchant"] = f"every transaction from “{merchants[0]}”, including future ones"
    if len(manual_edits.normalize_phrase(search)) >= 4:
        scopes["description"] = f"every transaction whose description contains “{search}”"
    return scopes


def _names_a_merchant(transaction_id: str) -> bool:
    """Plaid names no merchant on some rows (the table then shows the description), and
    nothing but a description phrase can generalize those."""
    record = raw_record(transaction_id) or {}
    return bool(record.get("merchant_entity_id") or record.get("merchant_name"))


def _rows_changing(rows: pd.DataFrame, category: str | None, exclude: bool) -> int:
    current = rows["budget_cat"]
    if exclude:
        return int(current.notna().sum())
    return int((current != category).sum()) if category else 0


def _apply(rows: pd.DataFrame, scope: str, *, category, exclude: bool, note: str,
           phrase: str) -> None:
    try:
        if scope == "transaction":
            if len(rows) > MAX_ROWS:
                st.error(f"That is {len(rows)} rows; narrow the filters to {MAX_ROWS} or fewer, "
                         "or apply to a merchant or a description phrase instead.")
                return
            records = [r for r in map(raw_record, rows["transaction_id"]) if r is not None]
            edits = [budget_edits.build_edit(r, scope=scope, category=category, exclude=exclude,
                                             note=note) for r in records]
        else:
            matching = _representative(rows, scope, phrase)
            edits = [budget_edits.build_edit(matching, scope=scope, category=category,
                                             exclude=exclude, note=note, phrase=phrase)]
        written = budget_edits.append_edits(edits)
    except (ValueError, OSError) as e:
        st.error(f"Could not save the budget edit: {e}")
        return
    st.toast(f"Saved {written} budget edit{'s' if written != 1 else ''}. Revoke from Overrides.")
    st.rerun()


def _representative(rows: pd.DataFrame, scope: str, phrase: str) -> dict:
    """A raw record the scope's match can be written from: for a phrase, one the phrase
    occurs in (the transformer refuses a phrase its source row does not contain)."""
    for tid in rows["transaction_id"]:
        record = raw_record(tid)
        if record and (scope == "merchant" or manual_edits.description_matches([record], phrase)):
            return record
    raise ValueError("no row shown can be used to write that rule")


def covered(edit: dict) -> int:
    """How many archive rows an edit currently applies to."""
    return manual_edits.affected_counts([edit], all_raw_records()).get(edit["id"], 0)
