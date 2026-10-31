"""Suggested merchant merges: conservative candidates, chain and cycle resolution."""
import pandas as pd

import merchant_merge
from enrich import _apply_merchant_aliases
from merchant_merge import candidates, rejected_pairs, resolve


def _df(*merchants):
    """merchants: (id, name, category, visits)"""
    return pd.DataFrame([{"merchant_id": mid, "merchant": name, "tier1": category, "flow": "spend",
                          "refund": False, "spend": 10.0}
                         for mid, name, category, visits in merchants for _ in range(visits)])


def _pairs(*merchants, decided=frozenset()):
    return [(p["kept"]["name"], p["absorbed"]["name"]) for p in candidates(_df(*merchants), set(decided))]


def _merge(eid, absorbed, into, name=""):
    return {"id": eid, "action": merchant_merge.ACTION, "match": {"merchant_id": absorbed},
            "set": {"into": into, "name": name or into}}


def test_given_a_name_and_its_longer_form_when_compared_then_a_candidate_keeping_the_busier():
    assert _pairs(("h", "Tidy Home", "House", 1), ("hs", "Tidy Home Services", "House", 3)) == [
        ("Tidy Home Services", "Tidy Home")]


def test_given_a_star_suffixed_name_when_compared_then_it_matches_its_stem():
    assert _pairs(("a", "ACM", "Travel", 2), ("b", "ACM*FLT", "Travel", 1)) == [("ACM", "ACM*FLT")]


def test_given_unrelated_or_generic_names_when_compared_then_no_candidate():
    assert _pairs(("a", "Hotel Aster", "Travel", 2), ("b", "Hotel Restaurante", "Travel", 1)) == []
    assert _pairs(("a", "Amazon", "Shops", 2), ("b", "Amazonia", "Shops", 1)) == []
    assert _pairs(("a", "Hotel", "Travel", 2), ("b", "Hotel Aster", "Travel", 1)) == []
    assert _pairs(("a", "The Cafe", "Food", 2), ("b", "The Cafe Roma", "Food", 1)) == []
    assert _pairs(("a", "Ab", "Food", 2), ("b", "Ab Roma", "Food", 1)) == []


def test_given_different_categories_when_compared_then_no_candidate():
    assert _pairs(("a", "Tidy Home", "House", 2), ("b", "Tidy Home Services", "Other", 1)) == []


def test_given_a_rejected_pair_when_compared_then_no_candidate():
    entries = [{"id": "x", "action": merchant_merge.ACTION, "match": {"merchant_id": "a"},
                "set": {"distinct": "b"}}]
    merchants = (("a", "Tidy Home", "House", 2), ("b", "Tidy Home Services", "House", 1))

    assert _pairs(*merchants, decided=rejected_pairs(entries)) == []
    assert rejected_pairs([*entries, {"id": "r", "action": "revoke", "revokes": "x"}]) == set()


def test_given_a_chain_when_resolved_then_every_merchant_reaches_the_end():
    assert resolve([_merge("1", "a", "b"), _merge("2", "b", "c", "Kept")]) == {
        "a": ("c", "Kept"), "b": ("c", "Kept")}


def test_given_a_cycle_when_resolved_then_it_is_ignored_with_a_warning(caplog):
    assert resolve([_merge("1", "a", "b"), _merge("2", "b", "a"), _merge("3", "x", "y")]) == {"x": ("y", "y")}
    assert "cycle" in caplog.text


def test_given_a_revoked_merge_when_resolved_then_it_no_longer_applies():
    assert resolve([_merge("1", "a", "b"), {"id": "r", "action": "revoke", "revokes": "1"}]) == {}


def test_given_aliases_when_applied_then_rows_group_under_the_kept_merchant():
    df = pd.DataFrame({"merchant_id": ["a", "b"], "merchant": ["Tidy", "Tidy Home"],
                       "category_path": [["House", "Tidy"], ["House", "Tidy Home"]], "spend": [5.0, 7.0]})

    _apply_merchant_aliases(df, {"a": ("b", "Tidy Home")})

    assert df.groupby("merchant")["spend"].sum().to_dict() == {"Tidy Home": 12.0}
    assert df["merchant_id"].tolist() == ["b", "b"]
    assert df["category_path"].iloc[0] == ["House", "Tidy Home"]
