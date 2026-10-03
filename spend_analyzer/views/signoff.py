"""Review: confirm the budget category of every new transaction, one category at a time.

The rows no sign-off covers yet are grouped by budget category, so each group asks one
question ("are all of these Dining Out?"). A wrong row is fixed in place; saving writes a
transaction-scope budget edit, and signing off writes one ``budget_review`` intent for the
group (see ``signoff``). Ignores the sidebar: a row outside the selected period still
needs an answer.
"""
from __future__ import annotations

import re

import pandas as pd
import streamlit as st

import budget_edits
import manual_edits
import signoff
from budget_model import load_budget_model
from budget_table import known_categories
from config_io import Budget
from viz import md_text, money_cents, money_md, provenance_why, table_height
from views._widgets import all_raw_records, raw_record

CHANGED_GROUP = "Changed since you signed off"
DECIDE_GROUP = "Needs a budget decision"
GEN_KEY = "so_gen"
_EDITABLE = ["category", "note"]


def queue(df: pd.DataFrame) -> pd.DataFrame:
    """The rows still to review (new or changed since sign-off)."""
    if manual_edits.status() is not None:
        return df.iloc[0:0]
    rows = signoff.states(df, signoff.load_entries(), signoff.pending_parents(all_raw_records()),
                          load_budget_model().needs_decision)
    return rows[rows["review_state"] != signoff.REVIEWED]


def render(df: pd.DataFrame, todo: pd.DataFrame, budget: Budget) -> None:
    err = manual_edits.status()
    if err:
        st.caption(f"Review unavailable: {err}")
        return
    entries = signoff.load_entries()
    if not signoff.has_any(entries):
        _baseline_panel(df)
    if todo.empty:
        st.info("Everything is signed off. New transactions appear here after the next fetch.")
    else:
        st.caption(f"{len(todo)} rows to review, grouped by budget category. Fix a category "
                   "or note in place, then sign off the group.")
        history = signoff.merchant_history(df, exclude=set(todo["transaction_id"]))
        options = [*known_categories(budget, df["budget_cat"].dropna().unique()),
                   signoff.NOT_IN_BUDGET]
        edits_by_id = {e.get("id"): e for e in entries if e.get("action") == budget_edits.ACTION}
        for label, rows in _groups(todo):
            _block(label, rows, df, history, options, edits_by_id)
    _recent(entries)


def _groups(todo: pd.DataFrame) -> list[tuple[str, pd.DataFrame]]:
    changed = todo[todo["review_state"] == signoff.CHANGED]
    new = todo[todo["review_state"] == signoff.NEW]
    groups = [(CHANGED_GROUP, changed)] if not changed.empty else []
    for category in sorted(new["budget_cat"].dropna().unique()):
        groups.append((category, new[new["budget_cat"] == category]))
    undecided = new[new["budget_cat"].isna()]
    if not undecided.empty:
        groups.append((DECIDE_GROUP, undecided))
    return [(label, rows.sort_values("date_resolved", kind="stable")) for label, rows in groups]


def _why(row, edits_by_id: dict) -> str:
    source = row.get("budget_source") or ""
    if source == "edit":
        edit = edits_by_id.get(row.get("budget_edit_id"), {})
        scope = edit.get("scope", "transaction")
        if scope == "description":
            return f"your rule: description has “{edit.get('match', {}).get('description_contains', '')}”"
        return "your merchant rule" if scope == "merchant" else "your edit"
    if source.startswith("pinned:"):
        return f"pinned rule “{source.removeprefix('pinned:')}”"
    if source == "atom":
        return "budget atom rule"
    if source == "transfer":
        return "payment-app income"
    return provenance_why(row.get("category_update_step"), row.get("category_update_reason"),
                          _true(row.get("review_pending"))) or "Plaid category"


def _true(value) -> bool:
    return bool(value) and not pd.isna(value)


def _flags(row, counts) -> str:
    flags = []
    if _true(row.get("pending")):
        flags.append("pending")
    if signoff.differs_from_history(row["budget_cat"], counts):
        flags.append(f"history says {counts.most_common(1)[0][0]}")
    if _true(row.get("review_pending")):
        flags.append("has a suggestion")
    return " · ".join(flags)


def _grid(rows: pd.DataFrame, history: dict, edits_by_id: dict) -> pd.DataFrame:
    grid = pd.DataFrame({
        "transaction_id": rows["transaction_id"].astype(str),
        "person": rows["person"].fillna(""),
        "date": rows["date_resolved"].astype(str),
        "description": rows["merchant"].fillna(rows["name"]).fillna(""),
        "amount": rows["display_amount"].astype(float),
        "account": rows["account_name"].fillna(""),
        "category": rows["budget_cat"].map(signoff.category_label),
        "note": rows["budget_note"].fillna(""),
        "was": rows["reviewed_as"].map(lambda c: "" if c is None else signoff.category_label(c)),
        "history": [signoff.history_label(history.get(m)) for m in rows["merchant_id"]],
        "why": [_why(r, edits_by_id) for _, r in rows.iterrows()],
        "flags": [_flags(r, history.get(r["merchant_id"])) for _, r in rows.iterrows()],
    })
    return grid.reset_index(drop=True)


def _block(label: str, rows: pd.DataFrame, df: pd.DataFrame, history: dict,
           options: list[str], edits_by_id: dict) -> None:
    key = f"so_{_slug(label)}_{st.session_state.get(GEN_KEY, 0)}"
    grid = _grid(rows, history, edits_by_id)
    with st.container(border=True):
        st.markdown(f"**{md_text(label)}** · {len(rows)} rows · "
                    f"{money_md(float(rows['display_amount'].sum()))}")
        if label == CHANGED_GROUP:
            st.caption("You signed these off under the category in Was; a rule or a re-audit "
                       "has moved them since. Sign off to accept where they are now.")
        elif label == DECIDE_GROUP:
            st.caption("Money moved through a payment app stays out of the budget until you "
                       "give it a category, e.g. your share of a dinner someone paid for.")
        columns = ["person", "date", "description", "amount", "account", "category", "note",
                   *(["was"] if label == CHANGED_GROUP else []), "history", "why", "flags"]
        edited = st.data_editor(
            grid, key=key, hide_index=True, width="stretch", column_order=columns,
            height=table_height(len(grid), 20),
            disabled=[c for c in grid.columns if c not in _EDITABLE],
            column_config={
                "person": "Person", "date": "Date", "account": "Account",
                "description": st.column_config.TextColumn("Description", width="medium"),
                "amount": st.column_config.NumberColumn("Amount", format="dollar",
                                                        help="A negative amount is money in."),
                "category": st.column_config.SelectboxColumn(
                    "Budget category", options=options, required=True, width="medium"),
                "note": st.column_config.TextColumn("Note"),
                "was": "Was",
                "history": st.column_config.TextColumn(
                    "History", help="Budget categories of this merchant's other transactions."),
                "why": st.column_config.TextColumn("Why", help="What chose the category."),
                "flags": "Check",
            })
        changed = int((edited[_EDITABLE] != grid[_EDITABLE]).any(axis=1).sum())
        c = st.columns([2, 3, 5])
        if c[0].button(f"Save {changed} change{'s' if changed != 1 else ''}", key=f"{key}_save",
                       disabled=not changed, width="stretch"):
            _save(grid, edited, sign=False)
        if c[1].button(f"Save and sign off {len(grid)} rows", key=f"{key}_sign", type="primary",
                       width="stretch"):
            _save(grid, edited, sign=True)
        _merchant_lookup(rows, df, key)


def _merchant_lookup(rows: pd.DataFrame, df: pd.DataFrame, key: str) -> None:
    with st.expander("Past transactions for a merchant"):
        names = sorted(rows["merchant"].dropna().unique())
        if not names:
            st.caption("No merchant named in this group.")
            return
        name = st.selectbox("Merchant", names, key=f"{key}_merchant")
        ids = set(rows.loc[rows["merchant"] == name, "merchant_id"])
        past = df[df["merchant_id"].isin(ids) & ~df["transaction_id"].isin(rows["transaction_id"])]
        if past.empty:
            st.caption("No other transactions from this merchant.")
            return
        past = past.sort_values("date_resolved", ascending=False).head(10)
        st.dataframe(pd.DataFrame({
            "Date": past["date_resolved"], "Amount": past["display_amount"].map(money_cents),
            "Budget category": past["budget_cat"].map(signoff.category_label),
            "Note": past["budget_note"].fillna("")}), hide_index=True, width="stretch")


def _save(grid: pd.DataFrame, edited: pd.DataFrame, *, sign: bool) -> None:
    try:
        edits = signoff.edits_from_grid(grid, edited, raw_record)
        intents = list(edits)
        if sign:
            final = {t: (None if c == signoff.NOT_IN_BUDGET else c)
                     for t, c in zip(edited["transaction_id"], edited["category"])}
            intents.append(signoff.build_signoff(final))
        if not intents:
            return
        signoff.append(intents)
    except (ValueError, OSError) as e:
        st.error(f"Could not save: {e}")
        return
    st.session_state[GEN_KEY] = st.session_state.get(GEN_KEY, 0) + 1
    done = f"Saved {len(edits)} change{'s' if len(edits) != 1 else ''}"
    st.toast(f"{done} and signed off {len(grid)} rows. Undo under Recent sign-offs."
             if sign else f"{done}.")
    st.rerun()


def _baseline_panel(df: pd.DataFrame) -> None:
    covered = signoff.baseline(df, signoff.manifest_path(), load_budget_model().needs_decision)
    with st.container(border=True):
        st.markdown("**Start from what the Google Sheets covered**")
        if not covered:
            st.caption("No Sheet export history found. Sign off the groups below as you go.")
            return
        st.caption(f"{len(covered)} transactions went out in the monthly Sheets, which you "
                   "reviewed there. Mark them signed off so this list starts after them.")
        if st.button(f"Mark {len(covered)} rows as signed off", key="so_baseline", type="primary"):
            try:
                signoff.append([signoff.build_signoff(
                    covered, note="the rows the Google Sheets covered", source="ui-baseline")])
            except (ValueError, OSError) as e:
                st.error(f"Could not save: {e}")
                return
            st.toast(f"Signed off {len(covered)} rows.")
            st.rerun()


def _recent(entries: list[dict], limit: int = 5) -> None:
    revoked = {e.get("revokes") for e in entries if e.get("action") == "revoke"}
    recent = [e for e in entries if e.get("action") == signoff.ACTION
              and e.get("id") not in revoked][-limit:][::-1]
    if not recent:
        return
    with st.expander("Recent sign-offs"):
        for entry in recent:
            n = len((entry.get("set") or {}).get("reviewed") or {})
            c = st.columns([6, 1])
            c[0].caption(f"{entry.get('created_at', '')} · {n} rows"
                         + (f" · {md_text(entry['note'])}" if entry.get("note") else ""))
            if c[1].button("Undo", key=f"so_undo_{entry['id']}"):
                try:
                    manual_edits.revoke(entry["id"], note="sign-off undone")
                except (ValueError, OSError) as e:
                    st.error(f"Could not undo: {e}")
                    return
                st.toast("Sign-off undone; its rows are back in review.")
                st.rerun()


def _slug(label: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")
