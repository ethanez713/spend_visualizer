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
OFFERS_KEY = "so_offers"
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
    current = budget_edits.resolve(entries)
    categories = dict(zip(df["transaction_id"], df["budget_cat"]))
    _offers(df, current, categories)
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
        _batch_rules(todo, edits_by_id, current, categories, options)
    _suggestions(df, current, categories)
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
    st.session_state[OFFERS_KEY] = [*st.session_state.get(OFFERS_KEY, []),
                                    *signoff.merchant_offers(grid, edited, raw_record)]
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


def _preview(df: pd.DataFrame, ids: list[str]) -> None:
    rows = df[df["transaction_id"].isin(ids)].sort_values("date_resolved", ascending=False)
    st.dataframe(pd.DataFrame({
        "Date": rows["date_resolved"], "Description": rows["merchant"].fillna(rows["name"]),
        "Amount": rows["display_amount"].map(money_cents),
        "Budget category now": rows["budget_cat"].map(signoff.category_label)}),
        hide_index=True, width="stretch", height=table_height(len(rows), 10))


def _write(intents: list[dict], done: str, *, revoke: str | None = None) -> None:
    try:
        signoff.append(intents)
        if revoke:
            manual_edits.revoke(revoke, note="replaced from Review")
    except (ValueError, OSError) as e:
        st.error(f"Could not save: {e}")
        return
    st.toast(done)
    st.rerun()


def _offers(df: pd.DataFrame, current: list[dict], categories: dict) -> None:
    """After a fix, offer the merchant-wide rule it suggests, with the rows it would move."""
    offers = st.session_state.get(OFFERS_KEY) or []
    for offer in list(offers):
        record = raw_record(offer["transaction_id"])
        if record is None:
            offers.remove(offer)
            continue
        rule = signoff.build_rule(record, offer["category"])
        moves = signoff.rule_preview(rule, current, all_raw_records(), categories)
        key = f"so_offer_{offer['transaction_id']}"
        with st.container(border=True):
            st.markdown(f"Make **{md_text(offer['category'])}** the default for "
                        f"**{md_text(offer['merchant'])}**?")
            st.caption(f"It would move {len(moves)} other transaction"
                       f"{'s' if len(moves) != 1 else ''} from this merchant now, and every "
                       "future one. Rows you set by hand keep their own category.")
            if moves:
                with st.expander("Rows it would move"):
                    _preview(df, moves)
            c = st.columns([1, 1, 6])
            if c[0].button("Yes", key=f"{key}_yes", type="primary", width="stretch"):
                offers.remove(offer)
                _write([rule], f"{offer['merchant']} now defaults to {offer['category']}.")
            if c[1].button("No", key=f"{key}_no", width="stretch"):
                offers.remove(offer)
                st.rerun()


def _rule_label(rule: dict) -> str:
    match = rule.get("match") or {}
    if rule.get("scope") == "description":
        return f"description has “{match.get('description_contains', '')}”"
    return match.get("merchant_name_normalized") or match.get("merchant_entity_id", "")


def _batch_rules(todo: pd.DataFrame, edits_by_id: dict, current: list[dict],
                 categories: dict, options: list[str]) -> None:
    """The rules that chose this batch's categories. The ones written here can be pointed
    at another category or deleted; the others are overridden with a merchant rule."""
    mine = todo.loc[todo["budget_source"] == "edit", "budget_edit_id"].map(edits_by_id.get).dropna()
    mine = [r for r in mine if r.get("scope") in ("merchant", "description")]
    pinned = todo.loc[todo["budget_source"].str.startswith("pinned:"), "budget_source"]
    transformer = todo.loc[(todo["budget_source"] == "fold")
                           & (todo["category_update_step"] == "mechanical"), "category_update_reason"]
    if not (mine or len(pinned) or len(transformer)):
        return
    with st.expander("Rules behind this batch"):
        for rule_id in dict.fromkeys(r["id"] for r in mine):
            _own_rule(edits_by_id[rule_id], sum(r["id"] == rule_id for r in mine), current,
                      categories, options, todo)
        for source, n in pinned.value_counts().items():
            st.caption(f"Pinned rule “{md_text(source.removeprefix('pinned:'))}” · {n} rows here. "
                       "It lives in budget.yaml and the converter's PINNED_RULES (change both); "
                       "a merchant rule made here overrides it.")
        for reason, n in transformer.value_counts().items():
            st.caption(f"Transformer rule {md_text(reason)} · {n} rows here. See the Rules tab; "
                       "a merchant rule made here overrides it for the budget.")


def _own_rule(rule: dict, here: int, current: list[dict], categories: dict,
              options: list[str], todo: pd.DataFrame) -> None:
    now = signoff.rule_category(rule)
    key = f"so_rule_{rule['id']}"
    with st.container(border=True):
        st.markdown(f"Your rule: **{md_text(_rule_label(rule))}** → {md_text(now)} · {here} rows here")
        c = st.columns([3, 1, 1])
        choice = c[0].selectbox("Category", options, index=options.index(now) if now in options else 0,
                                key=f"{key}_cat", label_visibility="collapsed")
        if choice != now:
            moves = signoff.rule_preview(signoff.retarget(rule, choice), current,
                                         all_raw_records(), categories, retire=rule["id"])
            st.caption(f"Pointing it at {md_text(choice)} moves {len(moves)} rows.")
        if c[1].button("Change", key=f"{key}_change", disabled=choice == now, width="stretch"):
            _write([signoff.retarget(rule, choice)], f"Rule now sets {choice}.", revoke=rule["id"])
        if c[2].button("Delete", key=f"{key}_delete", width="stretch"):
            try:
                manual_edits.revoke(rule["id"], note="deleted from Review")
            except (ValueError, OSError) as e:
                st.error(f"Could not delete: {e}")
                return
            st.toast("Rule deleted; its rows fall back to the budget model.")
            st.rerun()


def _suggestions(df: pd.DataFrame, current: list[dict], categories: dict) -> None:
    found = signoff.rule_suggestions(current, raw_record,
                                     dict(zip(df["transaction_id"], df["merchant"].fillna(""))))
    if not found:
        return
    with st.expander(f"Rules you could make · {len(found)}"):
        st.caption("Merchants you have moved by hand more than once, always to the same "
                   "category. A rule files their future transactions for you.")
        for s in found:
            record = raw_record(s["transaction_id"])
            if record is None:
                continue
            rule = signoff.build_rule(record, s["category"])
            moves = signoff.rule_preview(rule, current, all_raw_records(), categories)
            c = st.columns([6, 2])
            c[0].markdown(f"**{md_text(s['merchant'])}** → {md_text(s['category'])} · moved by "
                          f"hand {s['edits']} times · the rule would move {len(moves)} more rows")
            if c[1].button("Make rule", key=f"so_suggest_{s['transaction_id']}", width="stretch"):
                _write([rule], f"{s['merchant']} now defaults to {s['category']}.")
