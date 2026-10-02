"""Review: adjudicate the category suggestions the pipeline raised but did not apply.

Every decision is an intent in the transformer's manual-edit log, so it is replayed
on every categorize run: accepting sets the suggested category, picking another is
a plain override, and keeping the current one is a dismissal of that suggestion.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

import manual_edits
from viz import humanize_atom, md_text, money
from views._widgets import raw_record, recategorize_form

_SOURCES = {"mechanical": "a rule", "llm": "the local model", "claude": "the Claude audit"}


def pending(df: pd.DataFrame) -> pd.DataFrame:
    """Rows carrying a suggestion, largest first. Never filtered by the sidebar:
    a suggestion outside the selected period still needs an answer."""
    if "review_pending" not in df.columns:
        return df.iloc[0:0]
    rows = df[df["review_pending"].fillna(False).astype(bool)]
    return rows.sort_values("abs_amount", ascending=False)


def render(df: pd.DataFrame) -> None:
    err = manual_edits.status()
    if err:
        st.caption(f"Review unavailable: {err}")
        return
    rows = pending(df)
    if rows.empty:
        st.info("Nothing to review. Suggestions appear here when a rule or an audit "
                "disagrees with a transaction's category.")
        return
    st.caption(f"{len(rows)} suggestions, largest first. Each decision applies at once; "
               "a changed category can be revoked from Overrides.")
    for _, row in rows.iterrows():
        _card(row)


def _pair(primary, detailed) -> str:
    detail = str(detailed).removeprefix(f"{primary}_")
    return f"{humanize_atom(primary).capitalize()} / {humanize_atom(detail).lower()}"


def _card(row) -> None:
    tid = str(row["transaction_id"])
    raw = raw_record(tid)
    with st.container(border=True):
        st.markdown(f"**{md_text(row['merchant'])}** · {money(row['display_amount'])} · "
                    f"{row['date_resolved']}")
        if row.get("name") and row["name"] != row["merchant"]:
            st.caption(md_text(row["name"]))
        source = _SOURCES.get(row["review_source"], row["review_source"] or "the pipeline")
        st.markdown(f"Now **{_pair(row['pfc_primary'], row['pfc_detailed'])}**. "
                    f"Suggested by {source}: "
                    f"**{_pair(row['review_primary'], row['review_detailed'])}**"
                    + (f" — {md_text(row['review_reason'])}" if row["review_reason"] else ""))
        if raw is None:
            st.caption("Raw record not found in the archive. Reload data and retry.")
            return
        has_merchant = bool(raw.get("merchant_entity_id") or raw.get("merchant_name"))
        whole_merchant = has_merchant and st.checkbox(
            f"Apply to every transaction from “{raw.get('merchant_name') or row['merchant']}”",
            value=True, key=f"rv_scope_{tid}")
        c = st.columns([2, 2, 5])
        if c[0].button("Accept suggestion", key=f"rv_accept_{tid}", type="primary",
                       width="stretch"):
            _decide(raw, "merchant" if whole_merchant else "transaction",
                    row["review_primary"], row["review_detailed"],
                    f"accepted suggestion from {source}", "Suggestion accepted.")
        if c[1].button("Keep current", key=f"rv_keep_{tid}", width="stretch"):
            _save(lambda: manual_edits.dismiss(raw, note=f"dismissed suggestion from {source}",
                                               source="ui-review"), "Kept as it is.")
        with st.expander("Pick another category"):
            recategorize_form(row=row, key=f"rv_{tid}")


def _decide(raw: dict, scope: str, primary: str, detailed: str, note: str, done: str) -> None:
    _save(lambda: manual_edits.add_edit(raw, scope=scope, primary=primary, detailed=detailed,
                                        note=note, source="ui-review"), done)


def _save(write, done: str) -> None:
    try:
        write()
    except (ValueError, OSError) as e:
        st.error(f"Could not save the decision: {e}")
        return
    st.toast(done)
    st.rerun()
