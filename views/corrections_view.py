"""Corrections report tab: triage miscategorizations by fix-layer + export."""
from __future__ import annotations

import streamlit as st

import corrections as corr
from viz import sanitize_for_csv


def render() -> None:
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
