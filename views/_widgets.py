"""Reusable UI fragments: correction form, transaction-detail table, hide button."""
from __future__ import annotations

import pandas as pd
import streamlit as st

import corrections as corr
import state
from viz import blank_if_missing, humanize_atom, money

# Columns shown in a raw transaction-detail table.
_TXN_COLS = ["date_resolved", "name", "merchant", "tier1", "tier2", "atom_display",
             "abs_amount", "account_name", "confidence"]


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
            "account_name": "Account",
        },
    )
    flagged = [i for i, f in enumerate(edited["🚩"].tolist()) if f]
    for i in flagged[:6]:
        r = ordered.iloc[i]
        original = {"tier1": r.get("tier1"), "tier2": r.get("tier2"),
                    "pfc_detailed": r.get("pfc_detailed"), "merchant_name": r.get("merchant")}
        correction_form(
            scope="transaction", original=original,
            target={"label": f"{r.get('merchant')} · {money(r.get('abs_amount', 0))} · {r.get('date_resolved')}",
                    "transaction_id": str(r.get("transaction_id")),
                    "merchant": str(r.get("merchant"))},
            key=f"txn_{key}_{i}",
        )


def hide_button(dim: str, value, label: str, *, key: str) -> None:
    """An 'eye' toggle that hides/shows a category across all views."""
    hidden = state.is_hidden(dim, value)
    icon = "🙈 Show" if hidden else "👁 Hide"
    if st.button(f"{icon} {label}", key=key, use_container_width=True):
        state.toggle_hidden(dim, value, label)
        st.rerun()


def correction_form(*, scope: str, original: dict, target: dict, key: str) -> None:
    """Form to flag a miscategorization → appends to the corrections queue.

    Never edits records: it captures original + suggested diff for triage.
    """
    with st.expander(f"✏️ Flag a categorization problem — {target.get('label', scope)}"):
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
