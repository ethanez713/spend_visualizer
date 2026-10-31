"""Merchant merges: grouping decisions the household confirms, never made automatically.

A merge or a rejection is a ``merchant_alias`` intent in the transformer's shared log. A
merge only groups rows in the analyzer: raw records and category overrides are untouched.
"""
from __future__ import annotations

import logging
import re
from itertools import combinations

import pandas as pd

import manual_edits

ACTION = "merchant_alias"
GENERIC_WORDS = frozenset({"the", "cafe", "coffee", "restaurant", "bar", "grill", "hotel", "market",
                           "shop", "store", "inc", "llc", "co", "services"})
MIN_NAME_LETTERS = 3

log = logging.getLogger(__name__)


def name_tokens(name: str) -> list[str]:
    before_star = str(name).casefold().split("*", 1)[0]
    return re.sub(r"[^\w\s]", " ", before_star).split()


def _could_be_same(a: list[str], b: list[str]) -> bool:
    shorter, longer = sorted((a, b), key=len)
    if not shorter or longer[:len(shorter)] != shorter:
        return False
    return (not set(shorter) <= GENERIC_WORDS
            and sum(len(token) for token in shorter) >= MIN_NAME_LETTERS)


def merchants(df: pd.DataFrame) -> pd.DataFrame:
    rows = df[(df["flow"] == "spend") & ~df["merchant_id"].isin(["", "unknown"])]
    return rows.groupby("merchant_id").agg(
        name=("merchant", "first"), category=("tier1", lambda s: s.mode().iat[0]),
        visits=("refund", lambda s: int((~s).sum())), total=("spend", "sum")).reset_index()


def kept_first(a: dict, b: dict) -> tuple[dict, dict]:
    return tuple(sorted((a, b), key=lambda m: (-m["visits"], m["merchant_id"])))


def candidates(df: pd.DataFrame, decided: set[frozenset[str]]) -> list[dict]:
    """Likely duplicates, biggest first: same category and one name the whole-word start of
    the other. No edit distance, because a false merge is worse than a missed one. The
    merchant with more transactions is kept."""
    pairs = []
    for _, same_category in merchants(df).groupby("category"):
        for a, b in combinations(same_category.to_dict("records"), 2):
            if frozenset((a["merchant_id"], b["merchant_id"])) in decided:
                continue
            if _could_be_same(name_tokens(a["name"]), name_tokens(b["name"])):
                kept, absorbed = kept_first(a, b)
                pairs.append({"kept": kept, "absorbed": absorbed})
    return sorted(pairs, key=lambda p: -(p["kept"]["total"] + p["absorbed"]["total"]))


def live(entries: list[dict]) -> list[dict]:
    revoked = {e.get("revokes") for e in entries if e.get("action") == "revoke"}
    return [e for e in entries if e.get("action") == ACTION and e.get("id") not in revoked]


def rejected_pairs(entries: list[dict]) -> set[frozenset[str]]:
    return {frozenset((e["match"]["merchant_id"], e["set"]["distinct"]))
            for e in live(entries) if "distinct" in e["set"]}


def resolve(entries: list[dict]) -> dict[str, tuple[str, str]]:
    """``{absorbed id: (kept id, kept name)}`` with chains followed to their end. The latest
    merge of a merchant wins; merchants in a cycle stay unmerged, with a warning."""
    direct = {e["match"]["merchant_id"]: (e["set"]["into"], e["set"].get("name", ""))
              for e in live(entries) if "into" in e["set"]}
    resolved = {}
    for start in direct:
        seen, current, name = {start}, start, ""
        while current in direct:
            current, name = direct[current]
            if current in seen:
                log.warning("merchant merge cycle through %s ignored", start)
                break
            seen.add(current)
        else:
            resolved[start] = (current, name)
    return resolved


def load_entries() -> list[dict]:
    manual, _ = manual_edits._import()
    return manual.load_intents(manual_edits.edits_path())
