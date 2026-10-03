"""Review sign-offs: which rows still need review, how a changed category resurfaces, the
Sheet baseline, merchant history, and the edits a review grid produces."""
import json
from collections import Counter

import pandas as pd
import pytest

import signoff


def _decide(atom):
    return atom.endswith("_FROM_APPS")


def _df(*rows):
    cols = ["transaction_id", "budget_cat", "atom", "budget_edit_id", "date_resolved",
            "merchant_id", "pending"]
    defaults = {"atom": "FOOD_AND_DRINK_RESTAURANT", "budget_edit_id": "",
                "date_resolved": "2026-09-10", "merchant_id": "m1", "pending": False}
    return pd.DataFrame([{**defaults, **r} for r in rows], columns=cols)


def _signed(reviewed, at="2026-09-20T10:00:00", sid="s1"):
    return {"id": sid, "created_at": at, "action": "budget_review", "set": {"reviewed": reviewed}}


def _state(df, entries, parents=None):
    got = signoff.states(df, entries, parents or {}, _decide)
    return dict(zip(got["transaction_id"], got["review_state"]))


def test_given_no_signoff_when_states_computed_then_budget_and_decision_rows_are_new():
    df = _df({"transaction_id": "a", "budget_cat": "Dining Out"},
             {"transaction_id": "venmo", "budget_cat": None, "atom": "TRANSFER_OUT_TRANSFER_OUT_FROM_APPS"},
             {"transaction_id": "card", "budget_cat": None, "atom": "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"},
             {"transaction_id": "own", "budget_cat": None, "atom": "TRANSFER_OUT_ACCOUNT_TRANSFER"})

    assert _state(df, []) == {"a": "new", "venmo": "new"}


def test_given_signoff_with_the_same_category_when_states_computed_then_reviewed():
    df = _df({"transaction_id": "a", "budget_cat": "Dining Out"},
             {"transaction_id": "v", "budget_cat": None, "atom": "TRANSFER_IN_TRANSFER_IN_FROM_APPS"})

    assert _state(df, [_signed({"a": "Dining Out", "v": None})]) == {"a": "reviewed", "v": "reviewed"}


def test_given_category_moved_by_a_rule_after_signoff_when_states_computed_then_changed():
    df = _df({"transaction_id": "a", "budget_cat": "Merchandise"})

    got = signoff.states(df, [_signed({"a": "Dining Out"})], {}, _decide)

    assert got["review_state"].tolist() == ["changed"]
    assert got["reviewed_as"].tolist() == ["Dining Out"]


def test_given_signed_row_moved_out_of_the_budget_when_states_computed_then_changed_not_lost():
    df = _df({"transaction_id": "a", "budget_cat": None, "atom": "TRANSFER_OUT_ACCOUNT_TRANSFER"})

    assert _state(df, [_signed({"a": "Dining Out"})]) == {"a": "changed"}


def test_given_own_budget_edit_newer_than_signoff_when_states_computed_then_reviewed():
    edit = {"id": "e1", "action": "budget_edit", "created_at": "2026-09-21T09:00:00"}
    df = _df({"transaction_id": "a", "budget_cat": "Travel", "budget_edit_id": "e1"})

    assert _state(df, [_signed({"a": "Dining Out"}), edit]) == {"a": "reviewed"}


def test_given_own_budget_edit_older_than_signoff_when_category_differs_then_changed():
    edit = {"id": "e1", "action": "budget_edit", "created_at": "2026-09-01T09:00:00"}
    df = _df({"transaction_id": "a", "budget_cat": "Travel", "budget_edit_id": "e1"})

    assert _state(df, [_signed({"a": "Dining Out"}), edit]) == {"a": "changed"}


def test_given_pending_row_signed_off_when_it_posts_then_the_posted_row_inherits_it():
    df = _df({"transaction_id": "posted", "budget_cat": "Dining Out"})

    assert _state(df, [_signed({"pend": "Dining Out"})], {"posted": "pend"}) == {"posted": "reviewed"}


def test_given_revoked_signoff_when_states_computed_then_rows_are_new_again():
    df = _df({"transaction_id": "a", "budget_cat": "Dining Out"})
    entries = [_signed({"a": "Dining Out"}), {"id": "r", "action": "revoke", "revokes": "s1"}]

    assert _state(df, entries) == {"a": "new"}


def test_given_two_signoffs_of_a_row_when_live_then_the_latest_wins():
    entries = [_signed({"a": "Dining Out"}, sid="s1"), _signed({"a": "Travel"}, sid="s2")]

    assert signoff.live(entries)["a"][0] == "Travel"


def test_given_empty_selection_when_signoff_built_then_refused():
    with pytest.raises(ValueError):
        signoff.build_signoff({})


def _manifest(tmp_path, *records):
    path = tmp_path / "manifest.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records) + "not json\n")
    return path


def test_given_manifest_when_baseline_built_then_exported_and_pre_floor_rows_only(tmp_path):
    path = _manifest(tmp_path, {
        "since": "2026-09-01", "until": "2026-09-29",
        "rows": [{"transaction_id": "exported"}, {"transaction_id": None}],
        "silent": [{"transaction_id": "settled"}]})
    df = _df({"transaction_id": "old", "budget_cat": "Groceries", "date_resolved": "2026-08-31"},
             {"transaction_id": "exported", "budget_cat": "Dining Out"},
             {"transaction_id": "settled", "budget_cat": "Travel"},
             {"transaction_id": "fresh", "budget_cat": "Dining Out", "date_resolved": "2026-09-30"},
             {"transaction_id": "card", "budget_cat": None, "date_resolved": "2026-08-01",
              "atom": "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"})

    assert signoff.baseline(df, path, _decide) == {
        "old": "Groceries", "exported": "Dining Out", "settled": "Travel"}


def test_given_no_manifest_when_baseline_built_then_empty(tmp_path):
    df = _df({"transaction_id": "a", "budget_cat": "Dining Out"})

    assert signoff.baseline(df, tmp_path / "absent.jsonl", _decide) == {}


def test_given_merchant_rows_when_history_built_then_batch_and_pending_rows_left_out():
    df = _df({"transaction_id": "a", "budget_cat": "Utilities"},
             {"transaction_id": "b", "budget_cat": "Utilities"},
             {"transaction_id": "c", "budget_cat": "Other"},
             {"transaction_id": "p", "budget_cat": "Other", "pending": True},
             {"transaction_id": "batch", "budget_cat": "Other"})

    history = signoff.merchant_history(df, exclude={"batch"})

    assert history["m1"] == Counter({"Utilities": 2, "Other": 1})
    assert signoff.history_label(history["m1"]) == "Utilities 2 · Other 1"
    assert signoff.differs_from_history("Other", history["m1"])
    assert not signoff.differs_from_history("Utilities", history["m1"])


def test_given_a_single_past_row_when_compared_then_no_disagreement_is_claimed():
    assert not signoff.differs_from_history("Other", Counter({"Utilities": 1}))


RAW = {tid: {"transaction_id": tid, "merchant_entity_id": "e", "merchant_name": "Cafe",
             "name": "Cafe", "original_description": ""} for tid in ("a", "b", "c")}


def _grid(*rows):
    return pd.DataFrame(rows, columns=["transaction_id", "category", "note"])


@pytest.fixture
def bridge():
    import manual_edits
    if manual_edits.status() is not None:
        pytest.skip("transformer bridge unavailable")


def test_given_grid_changes_when_edits_built_then_one_transaction_edit_per_changed_row(bridge):
    before = _grid(("a", "Dining Out", ""), ("b", "Dining Out", ""), ("c", "Dining Out", "x"))
    after = _grid(("a", "Travel", ""), ("b", signoff.NOT_IN_BUDGET, "split"), ("c", "Dining Out", "x"))

    edits = signoff.edits_from_grid(before, after, RAW.get)

    assert [(e["match"], e["set"], e["note"]) for e in edits] == [
        ({"transaction_id": "a"}, {"budget_category": "Travel"}, ""),
        ({"transaction_id": "b"}, {"exclude": True}, "split")]
    assert {e["scope"] for e in edits} == {"transaction"}


def test_given_only_a_note_changed_when_edits_built_then_a_note_only_edit(bridge):
    edits = signoff.edits_from_grid(_grid(("a", "Dining Out", "")), _grid(("a", "Dining Out", "trip")),
                                    RAW.get)

    assert [(e["set"], e["note"]) for e in edits] == [({}, "trip")]


def test_given_a_row_missing_from_the_archive_when_edits_built_then_refused(bridge):
    with pytest.raises(ValueError):
        signoff.edits_from_grid(_grid(("zz", "Dining Out", "")), _grid(("zz", "Travel", "")), RAW.get)


def _rule(record, category, scope="merchant"):
    return signoff.build_rule(record, category, scope=scope)


def test_given_a_merchant_rule_when_previewed_then_rows_set_by_hand_keep_their_category(bridge):
    hand = {**_rule(RAW["a"], "Dining Out", scope="transaction"), "id": "hand"}
    rule = _rule(RAW["b"], "Travel")
    categories = {"a": "Dining Out", "b": "Dining Out", "c": "Travel"}

    assert signoff.rule_preview(rule, [hand], list(RAW.values()), categories) == ["b"]


def test_given_an_existing_rule_retargeted_when_previewed_then_the_old_rule_is_ignored(bridge):
    old = {**_rule(RAW["a"], "Dining Out"), "id": "old"}
    new = signoff.retarget(old, signoff.NOT_IN_BUDGET)
    categories = {"a": "Dining Out", "b": "Dining Out", "c": None}

    assert signoff.rule_preview(new, [old], list(RAW.values()), categories, retire="old") == ["a", "b"]
    assert new["match"] == old["match"] and new["set"] == {"exclude": True} and new["id"] != "old"


def _moves(*rows):
    return pd.DataFrame(rows, columns=["transaction_id", "description", "category"])


def test_given_rows_moved_in_the_grid_when_offers_built_then_one_per_named_merchant():
    raw = {**RAW, "x": {"transaction_id": "x", "merchant_entity_id": None, "merchant_name": None}}
    before = _moves(("a", "Cafe", "Other"), ("b", "Cafe", "Other"), ("x", "ACH 123", "Other"))
    after = _moves(("a", "Cafe", "Dining Out"), ("b", "Cafe", "Dining Out"), ("x", "ACH 123", "Travel"))

    assert signoff.merchant_offers(before, after, raw.get) == [
        {"transaction_id": "a", "merchant": "Cafe", "category": "Dining Out"}]


def test_given_one_merchant_moved_to_two_categories_when_offers_built_then_none():
    before = _moves(("a", "Cafe", "Other"), ("b", "Cafe", "Other"))
    after = _moves(("a", "Cafe", "Dining Out"), ("b", "Cafe", "Travel"))

    assert signoff.merchant_offers(before, after, RAW.get) == []


def _hand(tid, category):
    return {**_rule(RAW[tid], category, scope="transaction"), "id": f"h{tid}"}


def test_given_repeated_hand_moves_to_one_category_when_suggested_then_a_merchant_rule(bridge):
    current = [_hand("a", "Groceries"), _hand("b", "Groceries")]

    got = signoff.rule_suggestions(current, RAW.get, {"a": "Cafe", "b": "Cafe"})

    assert [(s["merchant"], s["category"], s["edits"]) for s in got] == [("Cafe", "Groceries", 2)]


def test_given_hand_moves_that_disagree_or_a_rule_exists_when_suggested_then_none(bridge):
    merchants = {"a": "Cafe", "b": "Cafe", "c": "Cafe"}
    mixed = [_hand("a", "Groceries"), _hand("b", "Dining Out")]
    covered = [_hand("a", "Groceries"), _hand("b", "Groceries"), _rule(RAW["c"], "Groceries")]

    assert signoff.rule_suggestions(mixed, RAW.get, merchants) == []
    assert signoff.rule_suggestions(covered, RAW.get, merchants) == []
