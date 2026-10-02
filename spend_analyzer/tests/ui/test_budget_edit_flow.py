"""Setting a budget category from the Transactions tab: edits land in the (isolated) intent
log with the right scope, and the transfers filter finds what nobody has classified."""
import json

import pytest

from tests.ui._harness import widget


def _budget_edits(path):
    return [json.loads(line) for line in path.read_text().splitlines()
            if json.loads(line).get("action") == "budget_edit"] if path.exists() else []


def _few_row_merchant(cube_df, *, with_entity_id=False):
    spend = cube_df[(cube_df["flow"] == "spend") & cube_df["merchant"].notna()]
    if with_entity_id:
        spend = spend[spend["merchant_id"].str.len().gt(20) & ~spend["merchant_id"].str.contains(" ")]
    counts = spend.groupby("merchant").size()
    few = counts[counts.between(1, 3)]
    if few.empty:
        pytest.skip("no merchant with a handful of rows in the archive")
    return few.index[0], int(few.iloc[0])


def _search(boot_app, term):
    at = boot_app("Transactions")
    widget(at.text_input, key="txn_search").set_value(term)
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    return at


def test_given_a_search_and_a_category_when_applied_to_the_rows_then_one_edit_per_row(
        boot_app, isolate_writes, cube_df):
    merchant, n = _few_row_merchant(cube_df)
    at = _search(boot_app, merchant)

    widget(at.selectbox, key="txn_budget_category").set_value("Travel")
    widget(at.text_input, key="txn_budget_note").set_value("trip")
    widget(at.button, key="txn_budget_apply").click()
    at.run()

    edits = _budget_edits(isolate_writes)
    assert not at.exception and len(edits) >= 1
    assert {e["set"]["budget_category"] for e in edits} == {"Travel"}
    assert {e["scope"] for e in edits} == {"transaction"} and {e["note"] for e in edits} == {"trip"}


def test_given_a_single_merchant_when_applied_to_every_transaction_then_one_merchant_edit(
        boot_app, isolate_writes, cube_df):
    merchant, _ = _few_row_merchant(cube_df, with_entity_id=True)
    at = _search(boot_app, merchant)

    widget(at.selectbox, key="txn_budget_category").set_value("Gifts/Charity")
    scopes = widget(at.radio, key="txn_budget_scope")
    scopes.set_value("merchant")
    widget(at.button, key="txn_budget_apply").click()
    at.run()

    (edit,) = _budget_edits(isolate_writes)
    assert edit["scope"] == "merchant" and edit["set"] == {"budget_category": "Gifts/Charity"}


def test_given_not_budget_spending_when_applied_then_the_edit_excludes(
        boot_app, isolate_writes, cube_df):
    merchant, _ = _few_row_merchant(cube_df)
    at = _search(boot_app, merchant)

    widget(at.selectbox, key="txn_budget_category").set_value("Not budget spending")
    widget(at.button, key="txn_budget_apply").click()
    at.run()

    assert {tuple(e["set"].items()) for e in _budget_edits(isolate_writes)} == {(("exclude", True),)}


def test_given_only_a_note_when_applied_then_the_edit_carries_no_category(
        boot_app, isolate_writes, cube_df):
    merchant, _ = _few_row_merchant(cube_df)
    at = _search(boot_app, merchant)

    widget(at.text_input, key="txn_budget_note").set_value("Europe")
    widget(at.button, key="txn_budget_apply").click()
    at.run()

    edits = _budget_edits(isolate_writes)
    assert edits and {e["set"] == {} and e["note"] == "Europe" for e in edits} == {True}


def test_given_the_transfers_toggle_when_on_then_the_view_renders_without_error(boot_app):
    at = boot_app("Transactions")

    widget(at.toggle, key="txn_transfers").set_value(True)
    at.run()

    assert not at.exception, [str(e.value) for e in at.exception]


def _seed_edit(isolate_writes, cube_df, **kw):
    import budget_edits
    import manual_edits
    tid = cube_df[cube_df["flow"] == "spend"]["transaction_id"].iloc[0]
    edit = budget_edits.build_edit(manual_edits.raw_index()[tid], scope="transaction", **kw)
    budget_edits.append_edits([edit])
    return edit


def test_given_a_budget_edit_when_overrides_opens_then_it_is_listed_and_can_be_revoked(
        boot_app, isolate_writes, cube_df):
    edit = _seed_edit(isolate_writes, cube_df, category="Travel", note="trip")

    at = boot_app("Overrides")
    assert "Budget edits" in [h.value for h in at.subheader]
    widget(at.button, key=f"revoke_budget_{edit['id']}").click()
    at.run()

    entries = [json.loads(line) for line in isolate_writes.read_text().splitlines()]
    assert any(e.get("action") == "revoke" and e.get("revokes") == edit["id"] for e in entries)
    assert not at.exception, [str(e.value) for e in at.exception]


def test_given_a_seeded_edit_when_budget_edit_applied_then_the_category_shows_in_the_cube(
        isolate_writes, cube_df):
    from data import build_cube
    edit = _seed_edit(isolate_writes, cube_df, category="Wedding")
    tid = edit["match"]["transaction_id"]

    df = build_cube().df.set_index("transaction_id")

    assert df.loc[tid, "budget_cat"] == "Wedding" and df.loc[tid, "budget_edit_id"] == edit["id"]
