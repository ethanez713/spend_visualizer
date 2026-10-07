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

import budget_edits
import corrections as corr
import manual_edits
from viz import money_cents, plural, sanitize_for_csv, table_height
from views._widgets import all_raw_records, raw_record


def render() -> None:
    _render_overrides()
    st.divider()
    _render_budget_edits()
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
        st.caption(f"Intent log unavailable: {err}")
        return
    items = manual_edits.intents()
    st.caption(
        "Category overrides beat every rule and survive full re-audits; a merchant "
        "override also covers that merchant's future transactions. Revoking one "
        "restores the earlier category at once.",
        help=f"Stored in `{manual_edits.edits_path()}` and replayed by the categorizer "
             "on every run.")
    if not items:
        st.info("No overrides yet. Tick Flag on a transaction in Drilldown or "
                "Merchants, then use ‘Override category’.")
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
        "target": _target(it),
        "→ category": f"{it['set']['primary']} / {it['set']['detailed']}",
        "rows covered": counts.get(it["id"], 0),
        "source": it.get("source", ""),
        "note": it.get("note", ""),
    } for it in items]
    st.dataframe(pd.DataFrame(rows).iloc[::-1], width="stretch", hide_index=True,
                 height=table_height(len(rows), 12),
                 column_config={"rows covered": st.column_config.NumberColumn(
                     "rows covered",
                     help="Archive rows this intent currently applies to "
                          "(merchant scope keeps growing; 0 = target row not "
                          "in the archive yet)")})
    with st.expander(f"Revoke an override · {len(items)}"):
        st.caption("The row reverts and is re-audited on the next categorize run.")
        _revoke_list(items, rows)


def _outcome(edit: dict) -> str:
    chosen = edit["set"]
    if chosen.get("budget_category"):
        return chosen["budget_category"]
    return "Not budget spending" if chosen.get("exclude") else "(note only)"


def _render_budget_edits() -> None:
    """Where the household counts each transaction in the Budget tab."""
    st.subheader("Budget edits")
    if manual_edits.status():
        st.caption("Intent log unavailable.")
        return
    items = budget_edits.current()
    st.caption("A budget edit sets the category the Budget tab counts a transaction under, "
               "leaves it out, or only adds a note. It never changes the Plaid category, and "
               "revoking one hands the row back to the budget model at once.")
    if not items:
        st.info("No budget edits yet. Use ‘Set the budget category…’ under the Transactions table.")
        return
    records = all_raw_records()
    covers = {**manual_edits.affected_counts([e for e in items if e["set"]], records),
              **manual_edits.affected_counts([e for e in items if not e["set"]], records)}
    rows = [{"id": e["id"], "when": e["created_at"], "scope": e["scope"], "target": _target(e),
             "counts as": _outcome(e), "rows covered": covers.get(e["id"], 0),
             "note": e.get("note", "")} for e in items]
    st.dataframe(pd.DataFrame(rows).iloc[::-1], width="stretch", hide_index=True,
                 height=table_height(len(rows), 12))
    with st.expander(f"Revoke a budget edit · {len(items)}"):
        for e, row in zip(reversed(items), reversed(rows)):
            c1, c2 = st.columns([5, 1])
            c1.write(f"`{e['id']}` · {e['scope']} · **{row['target']}** → {row['counts as']} · "
                     f"covers {plural(row['rows covered'], 'row')}"
                     + (f" — {e['note']}" if e.get("note") else ""))
            if c2.button("Revoke", key=f"revoke_budget_{e['id']}"):
                manual_edits.revoke(e["id"])
                st.rerun()


def _target(it: dict) -> str:
    match = it["match"]
    if it["scope"] == "description":
        return f"description contains “{match.get('description_contains')}”"
    if match.get("transaction_id"):
        return _describe_transaction(match["transaction_id"])
    return ( (it.get("snapshot") or {}).get("merchant_name")
            or match.get("merchant_name_normalized")
            or match.get("merchant_entity_id"))


def _describe_transaction(transaction_id: str) -> str:
    """`2026-03-08 · Clip Mx · $73.61` rather than an opaque Plaid id."""
    raw = raw_record(transaction_id)
    if raw is None:
        return transaction_id
    name = raw.get("merchant_name") or raw.get("name") or ""
    day = raw.get("authorized_date") or raw.get("date") or ""
    return f"{day} · {name} · {money_cents(float(raw.get('amount') or 0))}"


def _revoke_list(items: list[dict], rows: list[dict]) -> None:
    for it, row in zip(reversed(items), reversed(rows)):
        c1, c2 = st.columns([5, 1])
        c1.write(f"`{it['id']}` · {it['scope']} · **{row['target']}** → "
                 f"{it['set']['detailed']} · covers {plural(row['rows covered'], 'row')}"
                 + (f" — {it['note']}" if it.get("note") else ""))
        if c2.button("Revoke", key=f"revoke_{it['id']}"):
            manual_edits.revoke(it["id"])
            st.rerun()


def _render_triage_notes() -> None:
    st.subheader("Triage notes (report only)")
    st.caption(
        "Notes filed via ‘Note for triage’ — **nothing here changes records**: "
        "it's a to-do list for upstream (collector) vs local (taxonomy.yaml) "
        "work. For an actual category fix, use ‘Override category’ instead."
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
    st.dataframe(df, width="stretch", hide_index=True)

    md = corr.report_markdown(items)
    e1, e2, e3 = st.columns(3)
    e1.download_button("Report (Markdown)", md, file_name="corrections_report.md",
                       mime="text/markdown", width="stretch")
    e2.download_button("Table (CSV)", sanitize_for_csv(df).to_csv(index=False),
                       file_name="corrections.csv", mime="text/csv",
                       width="stretch")
    with e3.popover("Clear all", width="stretch"):
        st.caption(f"Deletes all {len(items)} triage notes. This cannot be undone.")
        if st.button(f"Delete {len(items)} notes", key="clear_notes_confirm"):
            corr.clear_corrections()
            st.rerun()

    with st.expander("Preview the copy/paste report"):
        st.markdown(md)

    with st.expander(f"Remove a single note · {len(items)}"):
        _note_list(items)


def _note_list(items: list[dict]) -> None:
    for c in items:
        cols = st.columns([5, 1])
        diff = ", ".join(f"{k}→{c['suggestion'][k]}" for k in c["changed_fields"])
        cols[0].write(f"`{c['layer']}` · **{c['target'].get('label','')}** — {diff}")
        if cols[1].button("Delete", key=f"del_{c['id']}"):
            corr.delete_correction(c["id"])
            st.rerun()
