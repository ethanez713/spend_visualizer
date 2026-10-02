"""Reusable UI fragments: recategorize/correction forms, transaction table, hide button."""
from __future__ import annotations

import pandas as pd
import streamlit as st

import corrections as corr
import manual_edits
import rules_bridge
import state
from viz import (PROVENANCE_BADGES, blank_if_missing, humanize_atom, md_text, money, money_cents, money_md,
                 provenance_why, table_height)

# Columns shown in a raw transaction-detail table.
_TXN_COLS = ["date_resolved", "display_amount", "merchant", "name", "tier1", "tier2",
             "atom_display", "person", "account_name", "confidence", "why"]


def transaction_detail(rows: pd.DataFrame, title: str = "Transactions",
                       key: str = "txn", *, sort: str = "abs_amount",
                       ascending: bool = False, max_rows: int = 15) -> None:
    """Render the transactions behind a slice/merchant with a flag column.

    Checking on a row opens a transaction-scoped correction form (prefilled
    from that row). Flagging never edits records — it feeds the report.
    """
    if rows.empty:
        st.caption("No transactions.")
        return
    ordered = rows.sort_values(sort, ascending=ascending, kind="stable").reset_index(drop=True)
    view = ordered.copy()
    view["atom_display"] = view["atom"].map(humanize_atom)
    if "category_update_step" in view.columns:
        pend = (view["review_pending"] if "review_pending" in view.columns
                else [False] * len(view))
        view["why"] = [
            provenance_why(s, r, bool(p)) for s, r, p in zip(
                view["category_update_step"], view["category_update_reason"], pend)]
    cols = [c for c in _TXN_COLS if c in view.columns]
    disp = view[cols].copy()
    disp.insert(0, "Flag", False)

    st.caption(f"{md_text(title)} — {len(disp)} txns · {money_md(_net(rows))}  "
               "·  tick Flag on a row to fix its category")
    edited = st.data_editor(
        disp.style.format({"display_amount": money_cents}), width="stretch", hide_index=True, key=f"txned_{key}",
        height=table_height(len(disp), max_rows),
        disabled=[c for c in disp.columns if c != "Flag"],
        column_config={
            "Flag": st.column_config.CheckboxColumn("Flag", width="small",
                                                  help="Flag this transaction for correction"),
            "date_resolved": st.column_config.TextColumn("Date"),
            "name": st.column_config.TextColumn("Description", width="medium"),
            "merchant": st.column_config.TextColumn("Merchant", width="medium"),
            "tier1": "Category",
            "tier2": "Subcategory",
            "atom_display": "Atom",
            "display_amount": st.column_config.NumberColumn(
                "Amount", help="A negative amount is a refund."),
            "person": "Person",
            "account_name": "Account",
            "why": st.column_config.TextColumn(
                "Why", help="Who set this category: rule · LLM · "
                            "review · manual override · blank = Plaid default. "
                            "A pending suggestion awaits review."),
        },
    )
    flagged = [i for i, f in enumerate(edited["Flag"].tolist()) if f]
    for i in flagged[:6]:
        r = ordered.iloc[i]
        label = f"{r.get('merchant')} · {money(r.get('display_amount', 0))} · {r.get('date_resolved')}"
        fix_categorization(row=r, label=label, key=f"txn_{key}_{i}", expanded=True)


def _net(rows: pd.DataFrame) -> float:
    return float(rows["display_amount"].sum())


def hide_button(dim: str, value, label: str, *, key: str) -> None:
    """An 'eye' toggle that hides/shows a category across all views."""
    hidden = state.is_hidden(dim, value)
    icon = "Show" if hidden else "Hide"
    if st.button(f"{icon} {label}", key=key, width="stretch"):
        state.toggle_hidden(dim, value, label)
        st.rerun()


# ── Recategorize (PFC) → the transformer's manual-edit intent log ─────────────

@st.cache_data(show_spinner=False)
def _raw_index(sig: tuple) -> dict[str, dict]:
    """Raw archive records by transaction_id (sig = archive mtimes, busts the cache)."""
    return manual_edits.raw_index()


@st.cache_data(show_spinner=False)
def _rule_details(sig: tuple) -> dict[str, dict]:
    """Rule tables keyed by rule_id (sig = config/memory/archive stats)."""
    try:
        return rules_bridge.rule_details()
    except Exception:  # noqa: BLE001 — no transformer repo ⇒ no rule lookups
        return {}


def raw_record(transaction_id: str) -> dict | None:
    """The full archive record behind a cube row (what an intent is built from)."""
    return _raw_index(manual_edits.archive_sig()).get(str(transaction_id))


def _txt(v) -> str:
    """A row field as display text ('' for None/NaN)."""
    v = blank_if_missing(v)
    return "" if v is None else str(v)


def provenance_explain(row) -> None:
    """The decision chain for one row: Plaid original → who changed it → final.

    Includes the matched rule's definition inline when a mechanical rule (or a
    rule-sourced pending suggestion) is involved — the row→rule cross-reference.
    """
    step = _txt(row.get("category_update_step"))
    reason = _txt(row.get("category_update_reason"))
    cur = f"{_txt(row.get('pfc_primary'))} / {_txt(row.get('pfc_detailed'))}"
    details = _rule_details(rules_bridge.tables_sig())

    if step:
        badge = PROVENANCE_BADGES.get(step, step)
        orig = (f"{_txt(row.get('original_pf_primary'))} / "
                f"{_txt(row.get('original_pf_detailed'))}")
        conf = _txt(row.get("category_update_confidence"))
        line = (f"**Why this category:** {badge} `{reason}` changed Plaid's "
                f"`{orig}` to `{cur}`")
        if conf:
            line += f" (confidence {conf})"
        if step == "manual":
            line += " — revoke it from the Overrides tab."
        st.markdown(line)
        _rule_hint(details.get(reason))
    else:
        st.markdown(f"**Why this category:** Plaid default `{cur}` — "
                    "never changed by the pipeline.")

    if bool(row.get("review_pending")):
        sugg = (f"{_txt(row.get('review_primary'))} / "
                f"{_txt(row.get('review_detailed'))}")
        src = _txt(row.get("review_source"))
        why = _txt(row.get("review_reason"))
        st.markdown(f"**Pending suggestion** ({src}): `{sugg}` — {why}. "
                    "Decide it in Review.")
        _rule_hint(details.get(why))


def _rule_hint(rule: dict | None) -> None:
    if not rule:
        return
    st.caption(f"↳ rule `{rule['rule_id']}` ({rule['kind']}, {rule['origin']}, "
               f"trust={rule['trust']}): pattern `{rule['pattern']}` → "
               f"`{rule['primary']} / {rule['detailed']}` — full tables in the "
               "Rules tab.")


def fix_categorization(*, row, label: str, key: str,
                       default_scope: str = "transaction", expanded: bool = False) -> None:
    """Expander: provenance explainer + the two fix paths.

    The PRIMARY action is the sticky override intent; the report queue is a
    deliberately-secondary "note to self" that never changes records.
    """
    with st.expander(f"Fix categorization — {label}", expanded=expanded):
        provenance_explain(row)
        st.divider()
        t_re, t_rep = st.tabs(["Override category",
                               "Note for triage"])
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
        st.caption(f"Recategorize unavailable: {err}")
        return
    raw = raw_record(row.get("transaction_id"))
    if raw is None:
        st.caption("Raw record not found in the archive — reload data and retry.")
        return
    pfc = raw.get("personal_finance_category") or {}
    cur_p, cur_d = pfc.get("primary"), pfc.get("detailed")
    primaries, detailed_map = manual_edits.taxonomy()
    st.caption(f"Current: `{cur_p} / {cur_d}`. An override shows here at once, is written "
               "into the store by the next categorize run, and holds until you revoke it.")
    c1, c2 = st.columns(2)
    p = c1.selectbox("Primary", primaries,
                     index=primaries.index(cur_p) if cur_p in primaries else 0,
                     format_func=humanize_atom, key=f"rc_p_{key}")
    details = detailed_map[p]
    d = c2.selectbox("Detailed", details,
                     index=details.index(cur_d) if cur_d in details else 0,
                     format_func=lambda d: humanize_atom(d.removeprefix(p + "_")) or humanize_atom(d),
                     key=f"rc_d_{key}")
    merchant = raw.get("merchant_name") or row.get("merchant") or "this merchant"
    has_merchant = bool(raw.get("merchant_entity_id") or raw.get("merchant_name"))
    # Plaid names no merchant on these rows, so only the description can generalize.
    scopes = ["transaction", "merchant", "description"] if has_merchant \
        else ["transaction", "description"]
    labels = {"transaction": "just this transaction",
              "merchant": f"every transaction from “{merchant}”",
              "description": "every transaction whose description contains…"}
    scope = st.radio(
        "Apply to", scopes, horizontal=True, format_func=labels.get, key=f"rc_s_{key}",
        index=scopes.index(default_scope) if default_scope in scopes else 0)
    phrase, would_change = None, 0
    if scope == "description":
        phrase, would_change = _description_rule(raw, d, key)
    note = st.text_input("Note (why?)", key=f"rc_n_{key}")
    if st.button("Save edit", key=f"rc_b_{key}", type="primary"):
        if (p, d) == (cur_p, cur_d) and not would_change:
            st.warning("That is already the current category — nothing to save.")
            return
        try:
            manual_edits.add_edit(raw, scope=scope, primary=p, detailed=d, note=note,
                                  phrase=phrase)
        except (ValueError, OSError) as e:
            st.error(f"Could not save the edit: {e}")
            return
        st.toast(f"Override saved: {humanize_atom(d)}. Revoke it from Overrides.")
        st.rerun()


def _description_rule(raw: dict, detailed: str, key: str) -> tuple[str, int]:
    """The phrase input and a live preview of what a rule on it would cover.

    Returns (phrase, rows whose category the rule would change). The preview is the
    point of the control: a phrase one token too short sweeps in other payees, and
    that is visible here before anything is saved.
    """
    default = manual_edits.normalize_phrase(raw.get("name") or raw.get("merchant_name") or "")
    phrase = st.text_input(
        "Description contains", value=default, key=f"rc_ph_{key}",
        help="Matched as whole words, ignoring case, digits and punctuation. "
             "Applies to past and future transactions.")
    hits = manual_edits.description_matches(
        list(_raw_index(manual_edits.archive_sig()).values()), phrase)
    if not hits:
        st.caption("No transaction matches. Use at least four letters from the description.")
        return phrase, 0
    cat = lambda r: (r.get("personal_finance_category") or {}).get("detailed")  # noqa: E731
    changing = [r for r in hits if cat(r) != detailed]
    payees = {manual_edits.normalize_phrase(r.get("merchant_name") or r.get("name") or "")
              for r in hits}
    st.caption(f"{len(hits)} transactions match across {len(payees)} "
               f"{'payee' if len(payees) == 1 else 'payees'}; "
               f"{len(changing)} would change category.")
    preview = pd.DataFrame([{
        "Date": r.get("authorized_date") or r.get("date"),
        "Amount": r.get("amount"),
        "Description": r.get("name"),
        "Merchant": r.get("merchant_name") or "",
        "Current category": humanize_atom(cat(r)).capitalize(),
        "Changes": cat(r) != detailed,
    } for r in hits]).sort_values("Date", ascending=False)
    st.dataframe(preview.style.format({"Amount": money_cents}), hide_index=True,
                 width="stretch", height=table_height(len(preview), 8))
    return phrase, len(changing)


def correction_form(*, scope: str, original: dict, target: dict, key: str,
                    wrap: bool = True) -> None:
    """Form to flag a miscategorization → appends to the corrections queue.

    Never edits records: it captures original + suggested diff for triage. PFC category
    fixes are better made via ``recategorize_form`` (a sticky, replayed intent); this
    report path remains for merchant-name and tier/taxonomy fixes. ``wrap=False`` skips
    the expander (for callers already inside one — Streamlit can't nest them).
    """
    if wrap:
        with st.expander(f"Flag a categorization problem — {target.get('label', scope)}"):
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
                st.success("Noted for triage (Overrides tab → Triage notes).")
