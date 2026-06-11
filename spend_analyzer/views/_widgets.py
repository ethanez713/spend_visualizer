"""Reusable UI fragments: recategorize/correction forms, transaction table, hide button."""
from __future__ import annotations

import pandas as pd
import streamlit as st

import corrections as corr
import manual_edits
import state
from viz import blank_if_missing, humanize_atom, money

# Columns shown in a raw transaction-detail table.
_TXN_COLS = ["date_resolved", "name", "merchant", "tier1", "tier2", "atom_display",
             "abs_amount", "person", "account_name", "confidence"]


def transaction_detail(rows: pd.DataFrame, title: str = "Transactions",
                       key: str = "txn") -> None:
    """Render the transactions behind a slice/merchant with a 🚩 flag column.

    Checking 🚩 on a row opens a transaction-scoped correction form (prefilled
    from that row). Flagging never edits records — it feeds the report.
    """
    if rows.empty:
        st.caption("No transactions.")
        return
    ordered = rows.sort_values("abs_amount", ascending=False).reset_index(drop=True)
    view = ordered.copy()
    view["atom_display"] = view["atom"].map(humanize_atom)
    cols = [c for c in _TXN_COLS if c in view.columns]
    disp = view[cols].copy()
    disp.insert(0, "🚩", False)

    st.caption(f"{title} — {len(disp)} txns · {money(rows['abs_amount'].sum())}  "
               "·  tick 🚩 to flag a miscategorization")
    edited = st.data_editor(
        disp, use_container_width=True, hide_index=True, key=f"txned_{key}",
        disabled=[c for c in disp.columns if c != "🚩"],
        column_config={
            "🚩": st.column_config.CheckboxColumn("🚩", width="small",
                                                  help="Flag this transaction for correction"),
            "date_resolved": st.column_config.TextColumn("Date"),
            "name": st.column_config.TextColumn("Description", width="large"),
            "merchant": "Merchant",
            "atom_display": "Atom",
            "abs_amount": st.column_config.NumberColumn("Amount", format="$%.2f"),
            "person": "Person",
            "account_name": "Account",
        },
    )
    flagged = [i for i, f in enumerate(edited["🚩"].tolist()) if f]
    for i in flagged[:6]:
        r = ordered.iloc[i]
        label = f"{r.get('merchant')} · {money(r.get('abs_amount', 0))} · {r.get('date_resolved')}"
        fix_categorization(row=r, label=label, key=f"txn_{key}_{i}")


def hide_button(dim: str, value, label: str, *, key: str) -> None:
    """An 'eye' toggle that hides/shows a category across all views."""
    hidden = state.is_hidden(dim, value)
    icon = "🙈 Show" if hidden else "👁 Hide"
    if st.button(f"{icon} {label}", key=key, use_container_width=True):
        state.toggle_hidden(dim, value, label)
        st.rerun()


# ── Recategorize (PFC) → the transformer's manual-edit intent log ─────────────

@st.cache_data(show_spinner=False)
def _raw_index(sig: tuple) -> dict[str, dict]:
    """Raw archive records by transaction_id (sig = archive mtimes, busts the cache)."""
    return manual_edits.raw_index()


def fix_categorization(*, row, label: str, key: str,
                       default_scope: str = "transaction") -> None:
    """Expander with both fix paths: a sticky PFC recategorize intent, or a report entry."""
    with st.expander(f"✏️ Fix categorization — {label}"):
        t_re, t_rep = st.tabs(["Recategorize (PFC)", "Other fix (report only)"])
        with t_re:
            recategorize_form(row=row, key=key, default_scope=default_scope)
        with t_rep:
            original = {"tier1": row.get("tier1"), "tier2": row.get("tier2"),
                        "pfc_detailed": row.get("pfc_detailed"),
                        "merchant_name": row.get("merchant")}
            correction_form(
                scope=default_scope, original=original,
                target={"label": label, "transaction_id": str(row.get("transaction_id")),
                        "merchant": str(row.get("merchant"))},
                key=key, wrap=False,
            )


def recategorize_form(*, row, key: str, default_scope: str = "transaction") -> None:
    """Pick a correct PFC category → append a manual-edit INTENT to the transformer.

    Nothing is edited here (the archive stays read-only): the transformer's manual
    stage replays the intent on the next categorize run — and every run after, so the
    edit survives ``--full`` re-audits. Merchant scope covers ALL of the merchant's
    transactions, current and future. Not inside ``st.form``: the detailed dropdown
    must re-populate when the primary changes, and forms batch widget updates.
    """
    err = manual_edits.status()
    if err:
        st.caption(f"⚠ Recategorize unavailable: {err}")
        return
    raw = _raw_index(manual_edits.archive_sig()).get(str(row.get("transaction_id")))
    if raw is None:
        st.caption("⚠ Raw record not found in the archive — reload data and retry.")
        return
    pfc = raw.get("personal_finance_category") or {}
    cur_p, cur_d = pfc.get("primary"), pfc.get("detailed")
    primaries, detailed_map = manual_edits.taxonomy()
    st.caption(f"Current: `{cur_p} / {cur_d}`. The edit is queued as an intent and "
               "applied by the **next categorize run** (it never expires — revoke it "
               "from the Corrections tab).")
    c1, c2 = st.columns(2)
    p = c1.selectbox("Primary", primaries,
                     index=primaries.index(cur_p) if cur_p in primaries else 0,
                     key=f"rc_p_{key}")
    details = detailed_map[p]
    d = c2.selectbox("Detailed", details,
                     index=details.index(cur_d) if cur_d in details else 0,
                     key=f"rc_d_{key}")
    merchant = raw.get("merchant_name") or row.get("merchant") or "this merchant"
    scopes = ["transaction", "merchant"]
    scope = st.radio(
        "Apply to", scopes, index=scopes.index(default_scope), horizontal=True,
        format_func=lambda s: ("just this transaction" if s == "transaction"
                               else f"ALL transactions from “{merchant}”"),
        key=f"rc_s_{key}")
    note = st.text_input("Note (why?)", key=f"rc_n_{key}")
    if st.button("💾 Save edit", key=f"rc_b_{key}"):
        if (p, d) == (cur_p, cur_d):
            st.warning("That is already the current category — nothing to save.")
            return
        try:
            it = manual_edits.add_edit(raw, scope=scope, primary=p, detailed=d, note=note)
        except (ValueError, OSError) as e:
            st.error(f"Could not save the edit: {e}")
            return
        st.success(f"Saved intent `{it['id']}` ({scope}) → `{p} / {d}`. Applies on the "
                   "next categorize run; pending edits are listed in the Corrections tab.")


def correction_form(*, scope: str, original: dict, target: dict, key: str,
                    wrap: bool = True) -> None:
    """Form to flag a miscategorization → appends to the corrections queue.

    Never edits records: it captures original + suggested diff for triage. PFC category
    fixes are better made via ``recategorize_form`` (a sticky, replayed intent); this
    report path remains for merchant-name and tier/taxonomy fixes. ``wrap=False`` skips
    the expander (for callers already inside one — Streamlit can't nest them).
    """
    if wrap:
        with st.expander(f"✏️ Flag a categorization problem — {target.get('label', scope)}"):
            _correction_form_body(scope=scope, original=original, target=target, key=key)
    else:
        _correction_form_body(scope=scope, original=original, target=target, key=key)


def _correction_form_body(*, scope: str, original: dict, target: dict, key: str) -> None:
    st.caption(
        "This does **not** change any records. It adds a suggested diff to the "
        "corrections report so you can triage upstream (collector) vs local "
        "(taxonomy) fixes."
    )
    with st.form(f"corr_{key}", clear_on_submit=True):
        c1, c2 = st.columns(2)
        new_tier1 = c1.text_input("tier1 →", value=original.get("tier1", ""))
        new_tier2 = c2.text_input("tier2 →", value=original.get("tier2", ""))
        new_atom = c1.text_input("pfc_detailed →", value=original.get("pfc_detailed", ""))
        new_merch = c2.text_input("merchant_name →", value=original.get("merchant_name", ""))
        note = st.text_input("Note (why is it wrong?)")

        suggestion = {}
        for fld, val in (("tier1", new_tier1), ("tier2", new_tier2),
                         ("pfc_detailed", new_atom), ("merchant_name", new_merch)):
            if val and str(val) != str(original.get(fld, "")):
                suggestion[fld] = val

        auto = corr.suggest_layer(list(suggestion.keys())) if suggestion else "local"
        layer = st.radio(
            "Fix belongs to", list(corr.LAYERS.keys()),
            index=list(corr.LAYERS.keys()).index(auto), horizontal=True,
            format_func=lambda L: f"{L} — {corr.LAYERS[L]}",
        )
        submitted = st.form_submit_button("Add to corrections report")
        if submitted:
            if not suggestion:
                st.warning("Change at least one field to record a correction.")
            else:
                corr.add_correction(scope=scope, target=target, original=original,
                                    suggestion=suggestion, layer=layer, note=note)
                st.success("Added to the corrections report (QC → Corrections tab).")
