"""Overrides tab: the authoritative manual intents first, the triage notes demoted.

Two very different things live here, in deliberate visual order:
1. **My overrides** — the transformer's manual-edit intents: sticky, replayed on
   every categorize run, the highest categorization authority. This is the "real"
   fix path.
2. **Triage notes** — the report-only corrections queue: never changes anything;
   a note-to-self for later upstream/taxonomy work.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import streamlit as st

import corrections as corr
import manual_edits
from viz import sanitize_for_csv


def render() -> None:
    _render_overrides()
    st.divider()
    _render_triage_notes()


@st.cache_data(show_spinner=False)
def _affected(sig: tuple, edits_mtime: float) -> dict[str, int]:
    """Rows-covered count per intent (sig/mtime bust the cache on data change)."""
    return manual_edits.affected_counts(manual_edits.intents())


def _render_overrides() -> None:
    """The manual-edit intents: what I've overridden and what each is doing."""
    st.subheader("My overrides (manual intents)")
    err = manual_edits.status()
    if err:
        st.caption(f"⚠ Intent log unavailable: {err}")
        return
    items = manual_edits.intents()
    st.caption(
        "The **authoritative** fix path — appended to "
        f"`{manual_edits.edits_path()}` and replayed by the categorizer on "
        "**every** run: overrides trump rules and the LLM, survive full "
        "re-audits, and merchant scope covers future transactions too. "
        "Revoking hands the row back to the pipeline on the next run."
    )
    if not items:
        st.info("No overrides yet. Tick 🚩 on a transaction in Drilldown or "
                "Merchants, then use ‘✅ Override category’.")
        return

    try:
        mtime = Path(manual_edits.edits_path()).stat().st_mtime
    except OSError:
        mtime = 0.0
    counts = _affected(manual_edits.archive_sig(), mtime)

    rows = [{
        "id": it["id"],
        "when": it["created_at"],
        "scope": it["scope"],
        "target": (it["match"].get("transaction_id")
                   or (it.get("snapshot") or {}).get("merchant_name")
                   or it["match"].get("merchant_name_normalized")
                   or it["match"].get("merchant_entity_id")),
        "→ category": f"{it['set']['primary']} / {it['set']['detailed']}",
        "rows covered": counts.get(it["id"], 0),
        "source": it.get("source", ""),
        "note": it.get("note", ""),
    } for it in items]
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True,
                 column_config={"rows covered": st.column_config.NumberColumn(
                     "rows covered",
                     help="Archive rows this intent currently applies to "
                          "(merchant scope keeps growing; 0 = target row not "
                          "in the archive yet)")})
    st.caption("Revoke an override (the row reverts and is re-audited next run):")
    for it, row in zip(items, rows):
        c1, c2 = st.columns([5, 1])
        c1.write(f"`{it['id']}` · {it['scope']} · **{row['target']}** → "
                 f"{it['set']['detailed']} · covers {row['rows covered']} row(s)"
                 + (f" — {it['note']}" if it.get("note") else ""))
        if c2.button("Revoke", key=f"revoke_{it['id']}"):
            manual_edits.revoke(it["id"])
            st.rerun()


def _render_triage_notes() -> None:
    st.subheader("Triage notes (report only)")
    st.caption(
        "Notes filed via ‘📝 Note for triage’ — **nothing here changes records**: "
        "it's a to-do list for upstream (collector) vs local (taxonomy.yaml) "
        "work. For an actual category fix, use ‘✅ Override category’ instead."
    )
    items = corr.load_corrections()
    if not items:
        st.info("No triage notes.")
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
    st.caption("Remove a single note:")
    for c in items:
        cols = st.columns([5, 1])
        diff = ", ".join(f"{k}→{c['suggestion'][k]}" for k in c["changed_fields"])
        cols[0].write(f"`{c['layer']}` · **{c['target'].get('label','')}** — {diff}")
        if cols[1].button("Delete", key=f"del_{c['id']}"):
            corr.delete_correction(c["id"])
            st.rerun()
