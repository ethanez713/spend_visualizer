"""Rules tab: browse every place a category gets decided — read-only.

Three clearly-fenced sections:
1. the transformer's mechanical pattern rules (with live match / applied /
   flag-pending counts against the current archive, plus a per-rule row
   inspector — the rule→rows cross-reference);
2. its merchant memory (learned from past resolutions);
3. the OPTIONAL external converter's PFC → budget-category maps — a genuinely
   separate, Google-Sheet-only taxonomy, shown here so the fourth rule location
   isn't invisible, but visually fenced off to avoid conflating the two.

Editing stays a code edit in the owning project (rules_bridge docstring has the
why); this tab only makes the policy legible.
"""
from __future__ import annotations

import pandas as pd
import streamlit as st

import rules_bridge
from viz import money


@st.cache_data(show_spinner="Scanning archive against the rule tables…")
def _scan(sig: tuple) -> dict:
    return rules_bridge.match_scan()


@st.cache_data(show_spinner=False)
def _rules(sig: tuple) -> list[dict]:
    return rules_bridge.transformer_rules()


@st.cache_data(show_spinner=False)
def _memory(sig: tuple) -> list[dict]:
    return rules_bridge.merchant_memory_entries()


@st.cache_data(show_spinner=False)
def _converter(sig: tuple) -> dict | None:
    return rules_bridge.converter_rules()


def render(df: pd.DataFrame) -> None:
    st.caption(
        "Every rule that can decide a category, in one read-only place. To change "
        "one: built-ins live in `plaid_category_transformer/src/config.py`, personal "
        "rules in the data root's `personal_rules.json`, the Sheet mapping in the "
        "converter's `src/config.py`."
    )
    sig = rules_bridge.tables_sig()
    err = rules_bridge.status()
    if err:
        st.warning(f"Transformer rule tables unavailable: {err}")
    else:
        rules, scan = _rules(sig), _scan(sig)
        _render_pattern_rules(rules, scan, df)
        _render_rule_inspector(rules, scan)
        _render_memory(_memory(sig), scan)
    st.divider()
    _render_converter(_converter(sig), df)


def _provenance_counts(df: pd.DataFrame) -> tuple[pd.Series, pd.Series]:
    """(applied-now, flag-pending) row counts per rule_id from the cube."""
    empty = pd.Series(dtype=int)
    if "category_update_reason" not in df.columns:
        return empty, empty
    applied = (df[df["category_update_step"] == "mechanical"]
               ["category_update_reason"].value_counts())
    flagged = (df[df["review_pending"] & (df["review_source"] == "mechanical")]
               ["review_reason"].value_counts())
    return applied, flagged


def _render_pattern_rules(rules: list[dict], scan: dict, df: pd.DataFrame) -> None:
    st.subheader("🔧 Transformer pattern rules")
    st.caption(
        "First match wins, in table order: POS prefixes → website hints → keywords "
        "(merchant memory runs before all of these — next section). `auto` rules "
        "overwrite the category; `flag` rules only raise a suggestion for review."
    )
    if not rules:
        st.info("No pattern rules configured.")
        return
    by_rule = scan["by_rule"]
    applied, flagged = _provenance_counts(df)
    table = pd.DataFrame([{
        "kind": r["kind"],
        "pattern": r["pattern"],
        "→ category": f"{r['primary']} / {r['detailed']}",
        "trust": r["trust"],
        "origin": r["origin"],
        "matches now": len(by_rule.get(r["rule_id"], [])),
        "applied now": int(applied.get(r["rule_id"], 0)),
        "flags pending": int(flagged.get(r["rule_id"], 0)),
    } for r in rules])
    st.dataframe(
        table, use_container_width=True, hide_index=True,
        column_config={
            "matches now": st.column_config.NumberColumn(
                "matches now", help="Archive rows the mechanical cascade would hit "
                                    "with this rule today (first-match-wins)"),
            "applied now": st.column_config.NumberColumn(
                "applied now", help="Rows whose current category this rule set "
                                    "(provenance step = mechanical)"),
            "flags pending": st.column_config.NumberColumn(
                "flags pending", help="Rows carrying this rule's suggestion, "
                                      "awaiting `categorize.py --review`"),
        })


def _render_rule_inspector(rules: list[dict], scan: dict) -> None:
    by_rule = scan["by_rule"]
    options = [r["rule_id"] for r in rules if by_rule.get(r["rule_id"])]
    options += [k for k in ("memory:entity_id", "memory:name") if by_rule.get(k)]
    if not options:
        return
    pick = st.selectbox(
        "Inspect a rule — the rows it currently catches",
        ["—"] + options,
        format_func=lambda o: o if o == "—" else f"{o}  ({len(by_rule[o])} rows)")
    if pick == "—":
        return
    rows = pd.DataFrame(by_rule[pick])
    total = rows["amount"].sum() if "amount" in rows.columns else 0.0
    st.caption(f"`{pick}` — {len(rows)} rows · net {money(total)}")
    st.dataframe(rows.drop(columns=["transaction_id"], errors="ignore"),
                 use_container_width=True, hide_index=True)


def _render_memory(entries: list[dict], scan: dict) -> None:
    with st.expander(f"🧠 Merchant memory — {len(entries)} learned merchants"):
        st.caption(
            "Once a merchant is resolved, its category is remembered "
            "(`.secrets/merchant_memory.json`): an exact entity-id hit re-applies "
            "automatically (auto); a fuzzy name hit only suggests (flag)."
        )
        if not entries:
            st.info("Memory is empty.")
            return
        by_key = scan["by_memory_key"]
        table = pd.DataFrame([{
            "match": e["match"],
            "merchant": e["merchant"],
            "→ category": f"{e['primary']} / {e['detailed']}",
            "matches now": by_key.get(e["key"], 0),
        } for e in entries])
        st.dataframe(table, use_container_width=True, hide_index=True)


def _render_converter(conv: dict | None, df: pd.DataFrame) -> None:
    st.subheader("🧾 Google-Sheet budget mapping (converter)")
    st.caption(
        "⚠️ A **separate taxonomy** for the monthly Google Sheet only — it maps the "
        "final PFC categories above onto the budget's own category set, and never "
        "touches this app's data or views. Shown here so all rule locations are "
        "visible in one place."
    )
    if conv is None:
        st.info("No converter configured (`$SPEND_VISUALIZER_CONVERTER` or the data "
                "root's `converter_root` pointer) — nothing to show.")
        return
    st.caption(f"Source: `{conv['path']}` (read-only).")

    prim_counts = df["pfc_primary"].value_counts() if "pfc_primary" in df.columns else {}
    det_counts = df["pfc_detailed"].value_counts() if "pfc_detailed" in df.columns else {}

    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Pinned description rules** (fire before the PFC maps; "
                    "first match wins)")
        st.dataframe(pd.DataFrame([
            {"→ budget category": cat, "description tokens": ", ".join(tokens)}
            for cat, tokens in conv["pinned"]],
        ), use_container_width=True, hide_index=True)
        st.markdown("**Dropped rows** (never reach the Sheet)")
        st.dataframe(pd.DataFrame(
            [{"level": "primary", "PFC": p, "rows now": int(prim_counts.get(p, 0))}
             for p in conv["drop_primary"]] +
            [{"level": "detailed", "PFC": d, "rows now": int(det_counts.get(d, 0))}
             for d in conv["drop_detailed"]],
        ), use_container_width=True, hide_index=True)
    with c2:
        st.markdown("**PFC detailed → budget category** (checked before primary)")
        st.dataframe(pd.DataFrame([
            {"PFC detailed": k, "→ budget category": v,
             "rows now": int(det_counts.get(k, 0))}
            for k, v in conv["detailed_map"].items()],
        ), use_container_width=True, hide_index=True)
        st.markdown("**PFC primary → budget category** (the fallback)")
        st.dataframe(pd.DataFrame([
            {"PFC primary": k, "→ budget category": v,
             "rows now": int(prim_counts.get(k, 0))}
            for k, v in conv["primary_map"].items()],
        ), use_container_width=True, hide_index=True)
