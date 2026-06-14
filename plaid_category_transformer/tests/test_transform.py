"""End-to-end engine: selection + stages + provenance + schema-compatible output."""
import json
import lzma

import persister

from src.rules import MerchantMemory
from src.schema import BASE_COLUMNS, COLUMNS, NEW_COLUMNS, row_fn
from src.transformer import load_input, transform, write_category_log


def given_trusted_row_when_llm_disagrees_then_flagged_never_auto_changed(store_of, FakeLLM_cls):
    # A trusted Plaid label is AUDITED but never auto-overwritten by the LLM — only flagged,
    # even when the LLM is HIGH-confidence and authority would otherwise allow an apply.
    store = store_of({"personal_finance_category": {
        "primary": "FOOD_AND_DRINK", "detailed": "FOOD_AND_DRINK_GROCERIES",
        "confidence_level": "VERY_HIGH"}})
    llm = FakeLLM_cls({0: ("ENTERTAINMENT", "ENTERTAINMENT_VIDEO_GAMES", "actually games")},
                      confidence="HIGH")

    _, changes, flags = transform(store, memory=None, llm=llm, authority="apply_when_high")

    assert changes == []
    assert len(flags) == 1
    rec = store["txn_1"]
    assert rec["personal_finance_category"]["primary"] == "FOOD_AND_DRINK"   # untouched
    assert rec["personal_finance_category"]["confidence_level"] == "VERY_HIGH"
    assert rec["category_review_flag"] == "1"
    assert rec["category_review_detailed"] == "ENTERTAINMENT_VIDEO_GAMES"
    for col in NEW_COLUMNS:
        assert col in rec


def given_trusted_row_when_llm_concurs_then_no_flag(store_of, FakeLLM_cls):
    store = store_of({"personal_finance_category": {
        "primary": "FOOD_AND_DRINK", "detailed": "FOOD_AND_DRINK_GROCERIES",
        "confidence_level": "VERY_HIGH"}})
    llm = FakeLLM_cls({0: ("FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES", "still groceries")})

    _, changes, flags = transform(store, memory=None, llm=llm)

    assert changes == [] and flags == []
    rec = store["txn_1"]
    assert rec["personal_finance_category"]["confidence_level"] == "VERY_HIGH"
    assert rec["category_update_step"] == "" and rec["category_review_flag"] == ""


def given_low_confidence_row_when_llm_changes_it_then_flagged_not_applied(store_of, FakeLLM_cls):
    # Default authority "flag": the LLM never overwrites; it raises a review flag instead.
    store = store_of({"personal_finance_category": {
        "primary": "GENERAL_MERCHANDISE",
        "detailed": "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE",
        "confidence_level": "LOW"}})
    llm = FakeLLM_cls({0: ("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE", "coffee shop")})

    _, changes, flags = transform(store, memory=None, llm=llm)

    assert changes == []
    assert len(flags) == 1
    rec = store["txn_1"]
    # category UNCHANGED; suggestion recorded in the review columns.
    assert rec["personal_finance_category"]["detailed"] == "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE"
    assert rec["category_review_flag"] == "1"
    assert rec["category_review_detailed"] == "FOOD_AND_DRINK_COFFEE"
    assert rec["category_review_source"] == "llm"
    assert rec["category_update_step"] == ""


def given_low_confidence_row_when_apply_when_high_then_llm_applied(store_of, FakeLLM_cls):
    store = store_of({"personal_finance_category": {
        "primary": "GENERAL_MERCHANDISE",
        "detailed": "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE",
        "confidence_level": "LOW"}})
    llm = FakeLLM_cls({0: ("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE", "coffee shop")},
                      confidence="HIGH")

    _, changes, flags = transform(store, memory=None, llm=llm, authority="apply_when_high")

    assert len(changes) == 1 and flags == []
    rec = store["txn_1"]
    assert rec["personal_finance_category"]["detailed"] == "FOOD_AND_DRINK_COFFEE"
    assert rec["original_pf_category_detailed"] == "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE"
    assert rec["category_update_step"] == "llm"


def given_an_applied_change_when_transformed_then_memory_learns_it(store_of, FakeLLM_cls):
    # A COT*FLT row is a mechanical 'auto' rule → applied → taught to memory.
    store = store_of({"merchant_entity_id": "ent_z", "name": "COT*FLT",
                      "original_description": "COT*FLT", "merchant_name": "COT", "website": None,
                      "personal_finance_category": {
                          "primary": "GENERAL_MERCHANDISE",
                          "detailed": "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE",
                          "confidence_level": "LOW"}})
    mem = MerchantMemory(path=None)

    _, changes, _ = transform(store, memory=mem, llm=None)

    assert len(changes) == 1
    hit = mem.lookup(store["txn_1"])
    assert hit is not None
    assert (hit.primary, hit.detailed) == ("TRAVEL", "TRAVEL_FLIGHTS")


def given_mechanical_suggestion_when_llm_sees_batch_then_suggestion_is_passed(store_of, FakeLLM_cls):
    # Blue Bottle record carries a coffee-ish website; memory primes an entity hit so
    # the LLM batch item gets a suggested_* pair.
    store = store_of({})  # default record: LOW confidence, ent_bluebottle
    mem = MerchantMemory(path=None)
    mem.remember(store["txn_1"], "FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE")
    llm = FakeLLM_cls({0: ("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE", "coffee")})

    transform(store, memory=mem, llm=llm)

    item = llm.seen_items[0]
    assert item["suggested_primary"] == "FOOD_AND_DRINK"
    assert item["suggested_detailed"] == "FOOD_AND_DRINK_COFFEE"
    # The entity-id memory hit is a trusted 'auto' rule → applied in place as mechanical.
    assert store["txn_1"]["category_update_step"] == "mechanical"


def given_auto_mechanical_rule_when_no_llm_then_applied(store_of):
    # COT*FLT is a trusted 'auto' prefix → applies with no LLM needed.
    store = store_of({"name": "COT*FLT", "original_description": "COT*FLT",
                      "merchant_name": "COT", "merchant_entity_id": None, "website": None,
                      "personal_finance_category": {
                          "primary": "GENERAL_MERCHANDISE",
                          "detailed": "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE",
                          "confidence_level": "LOW"}})

    _, changes, flags = transform(store, memory=None, llm=None)

    assert len(changes) == 1 and flags == []
    assert store["txn_1"]["personal_finance_category"]["detailed"] == "TRAVEL_FLIGHTS"
    assert store["txn_1"]["category_update_step"] == "mechanical"


def given_flag_mechanical_rule_when_no_llm_then_flagged_not_applied(store_of):
    # TST* is a loose 'flag' prefix → only a suggestion, never an auto-overwrite.
    store = store_of({"name": "TST* TAQUERIA", "original_description": "TST* TAQUERIA",
                      "merchant_name": None, "merchant_entity_id": None, "website": None,
                      "personal_finance_category": {
                          "primary": "GENERAL_MERCHANDISE",
                          "detailed": "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE",
                          "confidence_level": "MEDIUM"}})

    _, changes, flags = transform(store, memory=None, llm=None)

    assert changes == [] and len(flags) == 1
    rec = store["txn_1"]
    assert rec["personal_finance_category"]["detailed"] == "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE"
    assert rec["category_review_detailed"] == "FOOD_AND_DRINK_RESTAURANT"
    assert rec["category_review_source"] == "mechanical"


# ── schema compatibility ───────────────────────────────────────────────────────

def given_transformed_record_when_inspected_then_all_original_keys_kept(store_of, FakeLLM_cls):
    store = store_of({})
    original_keys = set(store["txn_1"].keys())
    llm = FakeLLM_cls({0: ("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE", "coffee")})

    transform(store, memory=None, llm=llm)

    keys = set(store["txn_1"].keys())
    assert original_keys <= keys              # nothing dropped
    assert set(NEW_COLUMNS) <= keys           # provenance + review columns added


def given_row_fn_when_projected_then_yields_all_columns(store_of, FakeLLM_cls):
    store = store_of({})
    llm = FakeLLM_cls({0: ("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE", "coffee")})
    transform(store, memory=None, llm=llm, authority="apply_when_high")

    row = row_fn(store["txn_1"])
    assert set(BASE_COLUMNS) <= set(row.keys())
    assert set(NEW_COLUMNS) <= set(row.keys())
    # corrected category is reflected in the flat columns
    assert row["pf_category_detailed"] == "FOOD_AND_DRINK_COFFEE"
    assert row["pf_category_confidence"] == "CORRECTED"
    assert row["original_pf_category_detailed"] == "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE"


def given_full_run_when_persisted_then_jsonl_and_csv_round_trip(tmp_path, store_of, FakeLLM_cls):
    store = store_of({})
    llm = FakeLLM_cls({0: ("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE", "coffee")})
    transform(store, memory=None, llm=llm, authority="apply_when_high")

    jsonl = tmp_path / "out.jsonl"
    csv = tmp_path / "out.csv"
    persister.save_jsonl(str(jsonl), store)
    persister.derive_csv(store, str(csv), COLUMNS, row_fn=row_fn)

    reloaded = persister.load_jsonl(str(jsonl))
    assert reloaded["txn_1"]["personal_finance_category"]["detailed"] == "FOOD_AND_DRINK_COFFEE"
    header = csv.read_text().splitlines()[0].split(",")
    assert header == COLUMNS


# ── logging + input loading ─────────────────────────────────────────────────────

def given_changes_when_logged_then_jsonl_appended_owner_only(tmp_path):
    import os

    path = tmp_path / ".secrets" / "category_log.jsonl"
    write_category_log(str(path), [{"transaction_id": "t1", "step": "llm"}])
    write_category_log(str(path), [{"transaction_id": "t2", "step": "mechanical"}])

    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["transaction_id"] == "t1"
    assert oct(os.stat(path).st_mode)[-3:] == "600"


# ── LLM on/off resolution (the 7B is OFF by default; --llm opts in) ────────────

def given_no_llm_flags_when_resolved_then_off_by_config_default():
    from src.transformer import _resolve_llm_mode
    # default_on=False (the shipped config) → a bare run skips the LLM, stamps final.
    assert _resolve_llm_mode(llm=False, no_llm=False, llm_defer=False, default_on=False) \
        == (True, False)
    # If the config flag is ever flipped back on, a bare run enables the LLM again.
    assert _resolve_llm_mode(llm=False, no_llm=False, llm_defer=False, default_on=True) \
        == (False, False)


def given_explicit_flags_when_resolved_then_flag_wins_over_default():
    from src.transformer import _resolve_llm_mode
    assert _resolve_llm_mode(llm=True, no_llm=False, llm_defer=False, default_on=False) \
        == (False, False)                                   # --llm forces ON
    assert _resolve_llm_mode(llm=False, no_llm=True, llm_defer=False, default_on=True) \
        == (True, False)                                    # --no-llm forces OFF
    assert _resolve_llm_mode(llm=False, no_llm=False, llm_defer=True, default_on=True) \
        == (False, True)                                    # --llm-defer → pending


def given_xz_raw_store_when_loaded_then_parsed_by_transaction_id(tmp_path, make_record):
    path = tmp_path / "raw.jsonl.xz"
    recs = [make_record(transaction_id="a"), make_record(transaction_id="b")]
    with lzma.open(path, "wt", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r) + "\n")

    store = load_input(str(path))
    assert set(store.keys()) == {"a", "b"}
