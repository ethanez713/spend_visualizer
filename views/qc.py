"""Correctness / QC panel (PLAN.md §12)."""
from __future__ import annotations

import pandas as pd
import streamlit as st

from cube import Cube, GroupingSpec, CATEGORY_LEVELS
from viz import money


def render(cube: Cube, qc: dict, spec: GroupingSpec) -> None:
    st.subheader("Pipeline health")
    c = st.columns(4)
    c[0].metric("Transactions", qc.get("n_transactions", 0))
    c[1].metric("Excluded rows", qc.get("n_excluded", 0),
                help="Transfers + credit-card payments (internal money movement).")
    c[2].metric("Excluded $", money(qc.get("excluded_sum", 0.0)))
    c[3].metric("Spend in 'Other'", f"{qc.get('pct_spend_other', 0.0):.1f}%")

    c = st.columns(4)
    c[0].metric("Total spend", money(qc.get("spend_total", 0.0)))
    c[1].metric("Total income", money(qc.get("income_total", 0.0)))
    c[2].metric("Net", money(qc.get("net_total", 0.0)))
    c[3].metric("Settled pending dropped", qc.get("n_settled_pending_dropped", 0))

    # --- unmapped atoms ---
    st.subheader("Taxonomy coverage")
    unmapped = qc.get("unmapped_atoms", [])
    if unmapped:
        st.warning(
            f"{len(unmapped)} atom(s) missing from taxonomy.yaml "
            f"({qc.get('n_unmapped_rows', 0)} rows). Add them and reload taxonomy."
        )
        st.write(unmapped)
    else:
        st.success("All atoms are mapped in taxonomy.yaml.")

    # --- tie-out: grand total == sum over any single grouping ---
    st.subheader("Totals tie-out (double-count guard)")
    grand = cube.total(GroupingSpec(filters={**spec.filters, "flow": "spend"},
                                    date_from=spec.date_from, date_to=spec.date_to))["spend"]
    checks = []
    for col in CATEGORY_LEVELS + ["person", "channel", "account_type"]:
        g = cube.rollup(GroupingSpec(group_by=[col],
                                     filters={**spec.filters, "flow": "spend"},
                                     date_from=spec.date_from, date_to=spec.date_to,
                                     measures=["spend"], order_by=None))
        s = float(g["spend"].sum()) if not g.empty else 0.0
        checks.append({"grouping": col, "sum": s,
                       "ties_out": abs(s - grand) < 0.01})
    tie = pd.DataFrame(checks)
    st.dataframe(tie, use_container_width=True, hide_index=True,
                 column_config={"sum": st.column_config.NumberColumn(format="$%.2f")})
    if tie["ties_out"].all():
        st.success(f"Every grouping sums to {money(grand)} — no double counting.")
    else:
        st.error("A grouping does not tie out — investigate non-exclusive dims.")
