"""Corrections tab: manual recategorize intents + the triage report by fix-layer."""
from __future__ import annotations

import pandas as pd
import streamlit as st

import corrections as corr
import manual_edits
from viz import sanitize_for_csv


def render() -> None:
    _render_manual_intents()
    st.divider()
    st.subheader("Categorization corrections")
    st.caption(
        "Flagged from the Drilldown/Merchants views. Nothing here edits records — "
        "it's a triage report: **upstream** fixes go to the transaction-generation "
        "service; **local** fixes go to `config/taxonomy.yaml`."
    )
    items = corr.load_corrections()
    if not items:
        st.info("No corrections yet. Use the '✏️ Flag a categorization problem' "
                "expander under a category or merchant to add one.")
        return

    up = [c for c in items if c["layer"] == "upstream"]
    local = [c for c in items if c["layer"] == "local"]
    c1, c2 = st.columns(2)
    c1.metric("Upstream fixes (collector)", len(up))
    c2.metric("Local fixes (taxonomy)", len(local))

    df = corr.to_dataframe(items)
    st.dataframe(df, use_container_width=True, hide_index=True)

    md = corr.report_markdown(items)
    e1, e2, e3 = st.columns(3)
    e1.download_button("⬇ Report (Markdown)", md, file_name="corrections_report.md",
                       mime="text/markdown", use_container_width=True)
    e2.download_button("⬇ Table (CSV)", sanitize_for_csv(df).to_csv(index=False),
                       file_name="corrections.csv", mime="text/csv",
                       use_container_width=True)
    if e3.button("🗑 Clear all", use_container_width=True):
        corr.clear_corrections()
        st.rerun()

    with st.expander("Preview the copy/paste report"):
        st.markdown(md)

    st.divider()
    st.caption("Remove a single correction:")
    for c in items:
        cols = st.columns([5, 1])
        diff = ", ".join(f"{k}→{c['suggestion'][k]}" for k in c["changed_fields"])
        cols[0].write(f"`{c['layer']}` · **{c['target'].get('label','')}** — {diff}")
        if cols[1].button("Delete", key=f"del_{c['id']}"):
            corr.delete_correction(c["id"])
            st.rerun()


def _render_manual_intents() -> None:
    """The transformer's manual-edit intents: the durable recategorize edicts.

    Listed from (and revoked into) the transformer's append-only log — this app never
    edits records. Intents apply on the next categorize run and on every run after.
    """
    st.subheader("Manual recategorizations (intents)")
    err = manual_edits.status()
    if err:
        st.caption(f"⚠ Intent log unavailable: {err}")
        return
    items = manual_edits.intents()
    st.caption(
        f"Append-only intent log: `{manual_edits.edits_path()}` — replayed by the "
        "categorizer on **every** run (sticky: edits survive full re-audits; merchant "
        "scope covers future transactions too). Revoking hands the row back to the "
        "pipeline on the next run."
    )
    if not items:
        st.info("No manual edits yet. Use 🚩 → ‘Recategorize (PFC)’ in Drilldown, or "
                "the merchant detail in Merchants.")
        return
    rows = [{
        "id": it["id"],
        "when": it["created_at"],
        "scope": it["scope"],
        "target": (it["match"].get("transaction_id")
                   or (it.get("snapshot") or {}).get("merchant_name")
                   or it["match"].get("merchant_name_normalized")
                   or it["match"].get("merchant_entity_id")),
        "→ category": f"{it['set']['primary']} / {it['set']['detailed']}",
        "source": it.get("source", ""),
        "note": it.get("note", ""),
    } for it in items]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    st.caption("Revoke an intent (the row reverts and is re-audited next run):")
    for it, row in zip(items, rows):
        c1, c2 = st.columns([5, 1])
        c1.write(f"`{it['id']}` · {it['scope']} · **{row['target']}** → "
                 f"{it['set']['detailed']}" + (f" — {it['note']}" if it.get("note") else ""))
        if c2.button("Revoke", key=f"revoke_{it['id']}"):
            manual_edits.revoke(it["id"])
            st.rerun()
