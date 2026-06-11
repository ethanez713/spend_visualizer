"""Manual edit intents: construction, the append-only log, matching, replay, stickiness.

The contract under test (src/manual.py + the transformer wiring): an intent appended to
the log is re-applied on EVERY run — it survives a --full re-audit, catches a merchant's
future transactions, skips the (expensive) LLM stage on covered rows, and a revoke hands
the row back to the pipeline. All offline; the LLM is a recording stub.
"""
import json

import pytest

import persister
import src.transformer as tr
from src.incremental import HASH_PENDING_REVOKED, SOURCE_HASH_FIELD
from src.manual import (
    ManualIndex,
    append_intent,
    apply_manual_edits,
    build_intent,
    build_revoke,
    load_intents,
    resolve_intents,
    run_edit_session,
    search_rows,
)
from src.pfc_taxonomy import DETAILED, PRIMARY
from src.schema import PROCESS_CONFIDENCE, set_provenance, set_review_flag
from src.transformer import transform

COFFEE = ("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE")
FLIGHTS = ("TRAVEL", "TRAVEL_FLIGHTS")
PLAID_ORIGINAL = ("GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE")


def _intent(record, scope="transaction", cat=COFFEE, **kw):
    return build_intent(scope=scope, primary=cat[0], detailed=cat[1], record=record, **kw)


# ── Intent construction ───────────────────────────────────────────────────────

def given_a_record_when_txn_intent_built_then_match_and_snapshot_captured(make_record):
    it = _intent(make_record())
    assert it["match"] == {"transaction_id": "txn_1"}
    assert it["set"] == {"primary": COFFEE[0], "detailed": COFFEE[1]}
    assert it["snapshot"]["merchant_name"] == "Blue Bottle Coffee"
    assert it["snapshot"]["before_primary"] == "GENERAL_MERCHANDISE"


def given_a_corrected_record_when_intent_built_then_snapshot_keeps_plaid_original(
        make_record):
    rec = make_record()
    set_provenance(rec, *FLIGHTS, "mechanical", "pos:cot*flt", "HIGH")
    snap = _intent(rec)["snapshot"]
    assert snap["before_primary"] == "TRAVEL"              # the value being overridden
    assert snap["before_step"] == "mechanical"             # ...and which stage wrote it
    assert snap["plaid_primary"] == "GENERAL_MERCHANDISE"  # Plaid's true original


def given_a_record_when_merchant_intent_built_then_both_merchant_keys_captured(
        make_record):
    it = _intent(make_record(), scope="merchant")
    assert it["match"] == {"merchant_entity_id": "ent_bluebottle",
                           "merchant_name_normalized": "blue bottle coffee"}


def given_an_invalid_category_when_intent_built_then_raises(make_record):
    with pytest.raises(ValueError):
        build_intent(scope="transaction", primary="TRAVEL", detailed="NOT_A_REAL_LEAF",
                     record=make_record())


def given_no_merchant_identity_when_merchant_intent_built_then_raises(make_record):
    rec = make_record(merchant_entity_id=None, merchant_name=None)
    with pytest.raises(ValueError):
        _intent(rec, scope="merchant")


# ── The append-only log ───────────────────────────────────────────────────────

def given_intents_appended_when_loaded_and_resolved_then_order_preserved(
        tmp_path, make_record):
    path = str(tmp_path / "edits.jsonl")
    a = append_intent(path, _intent(make_record()))
    b = append_intent(path, _intent(make_record(transaction_id="txn_2"), cat=FLIGHTS))
    assert [it["id"] for it in resolve_intents(load_intents(path))] == [a["id"], b["id"]]


def given_a_corrupt_line_when_loaded_then_skipped_loudly_and_rest_survive(
        tmp_path, make_record, capsys):
    path = str(tmp_path / "edits.jsonl")
    append_intent(path, _intent(make_record()))
    with open(path, "a", encoding="utf-8") as f:
        f.write("{not json\n")
    append_intent(path, _intent(make_record(transaction_id="txn_2")))
    assert len(load_intents(path)) == 2
    assert "corrupt line" in capsys.readouterr().err


def given_a_revoke_when_resolved_then_intent_and_tombstone_dropped(make_record):
    it = _intent(make_record())
    keep = _intent(make_record(transaction_id="txn_2"))
    resolved = resolve_intents([it, keep, build_revoke(it["id"])])
    assert [x["id"] for x in resolved] == [keep["id"]]


def given_unknown_match_fields_when_resolved_then_intent_skipped(make_record, capsys):
    it = _intent(make_record())
    it["match"]["description_contains"] = "AMZN"   # a future predicate
    assert resolve_intents([it]) == []
    assert "unrecognized match field" in capsys.readouterr().err


def given_an_invalid_category_in_the_log_when_resolved_then_intent_skipped(
        make_record, capsys):
    it = _intent(make_record())
    it["set"]["detailed"] = "HAND_EDITED_TYPO"      # e.g. a hand-edited log line
    assert resolve_intents([it]) == []
    assert "not a valid PFC pair" in capsys.readouterr().err


# ── Matching (specificity, recency, the entity-id veto) ───────────────────────

def given_txn_and_newer_merchant_intents_when_matched_then_transaction_scope_wins(
        make_record):
    rec = make_record()
    txn_it = _intent(rec)                                   # older but specific
    merch_it = _intent(rec, scope="merchant", cat=FLIGHTS)  # newer but broad
    assert ManualIndex([txn_it, merch_it]).match(rec)["id"] == txn_it["id"]


def given_two_merchant_intents_when_matched_then_latest_wins(make_record):
    rec = make_record()
    old = _intent(rec, scope="merchant")
    new = _intent(rec, scope="merchant", cat=FLIGHTS)
    assert ManualIndex([old, new]).match(rec)["id"] == new["id"]


def given_same_name_but_different_entity_ids_when_matched_then_name_match_vetoed(
        make_record):
    it = _intent(make_record(), scope="merchant")           # ent_bluebottle + name
    impostor = make_record(transaction_id="txn_9", merchant_entity_id="ent_other")
    assert ManualIndex([it]).match(impostor) is None


def given_a_row_without_entity_id_when_matched_then_name_fallback_applies(make_record):
    it = _intent(make_record(), scope="merchant")
    row = make_record(transaction_id="txn_9", merchant_entity_id=None)
    assert ManualIndex([it]).match(row)["id"] == it["id"]


# ── Replay (apply_manual_edits) ───────────────────────────────────────────────

def given_a_covered_flagged_row_when_applied_then_manual_provenance_and_flag_cleared(
        make_record):
    rec = make_record()
    set_review_flag(rec, *FLIGHTS, "llm disagreed", "HIGH", "llm")
    summary = apply_manual_edits({"txn_1": rec}, ManualIndex([_intent(rec)]))
    pfc = rec["personal_finance_category"]
    assert (pfc["primary"], pfc["detailed"]) == COFFEE
    assert pfc["confidence_level"] == "CORRECTED"
    assert rec["category_update_step"] == "manual"
    assert rec["original_pf_category_primary"] == "GENERAL_MERCHANDISE"
    assert rec["category_review_flag"] == ""                # human outranks the flag
    assert len(summary["applied"]) == 1


def given_an_applied_intent_when_replayed_then_idempotent_no_churn(make_record):
    rec = make_record()
    idx = ManualIndex([_intent(rec)])
    apply_manual_edits({"txn_1": rec}, idx)
    second = apply_manual_edits({"txn_1": rec}, idx)
    assert second["applied"] == []
    assert second["already"] == 1


def given_a_revoked_intent_when_replayed_then_row_reverted_and_marked_for_reaudit(
        make_record):
    rec = make_record()
    it = _intent(rec)
    apply_manual_edits({"txn_1": rec}, ManualIndex([it]))
    summary = apply_manual_edits(
        {"txn_1": rec}, ManualIndex(resolve_intents([it, build_revoke(it["id"])])))
    pfc = rec["personal_finance_category"]
    assert (pfc["primary"], pfc["detailed"]) == PLAID_ORIGINAL
    assert pfc["confidence_level"] == "LOW"                 # Plaid's original restored
    assert rec["category_update_step"] == ""
    assert rec[SOURCE_HASH_FIELD] == HASH_PENDING_REVOKED   # full re-audit next run
    assert summary["reverted"] == ["txn_1"]


def given_an_intent_for_a_pruned_row_when_replayed_then_orphan_reported_store_untouched(
        make_record):
    gone = _intent(make_record(transaction_id="txn_gone"))
    rec = make_record()
    summary = apply_manual_edits({"txn_1": rec}, ManualIndex([gone]))
    assert summary["orphans"] == [gone["id"]]
    assert rec["personal_finance_category"]["primary"] == "GENERAL_MERCHANDISE"


# ── Pipeline integration ──────────────────────────────────────────────────────

def given_a_covered_row_when_transformed_then_llm_never_sees_it(
        make_record, store_of, FakeLLM_cls):
    covered = make_record()
    other = make_record(transaction_id="txn_2", merchant_name="Delta",
                        merchant_entity_id="ent_delta", name="DELTA AIR",
                        original_description="DELTA AIR LINES", website=None)
    llm = FakeLLM_cls()
    transform(store_of(covered, other), llm=llm, manual=ManualIndex([_intent(covered)]))
    assert {i["merchant_name"] for i in llm.seen_items} == {"Delta"}


class _RecordingLLM:
    """CategoryLLM stand-in: records every batch item, changes nothing, completes OK."""

    def __init__(self):
        self.ran_ok = False
        self.seen: list[dict] = []

    def categorize(self, items):
        self.seen.extend(items)
        self.ran_ok = True
        return {}


def _run(tmp_path, monkeypatch, records, *, edits_path, full=False, llm=None):
    """Drive the real ``run()`` offline (no Drive, stubbed LLM, default selection)."""
    monkeypatch.setattr(tr, "CategoryLLM",
                        lambda debug=False: llm if llm is not None else _RecordingLLM())
    inp = tmp_path / "input.jsonl"
    inp.write_text("".join(json.dumps(r) + "\n" for r in records))
    out_jsonl = str(tmp_path / "out.jsonl")
    tr.run(input_path=str(inp), out_jsonl=out_jsonl, out_csv=str(tmp_path / "out.csv"),
           flags_csv=str(tmp_path / "flags.csv"), log_path=str(tmp_path / "log.jsonl"),
           levels=set(PROCESS_CONFIDENCE), memory_path=None, do_drive=False,
           no_llm=False, debug=False, full=full, edits_path=edits_path)
    return persister.load_jsonl(out_jsonl)


def given_a_full_reaudit_when_intent_exists_then_manual_edit_survives(
        tmp_path, monkeypatch, make_record):
    edits = str(tmp_path / "edits.jsonl")
    append_intent(edits, _intent(make_record()))
    store = _run(tmp_path, monkeypatch, [make_record()], edits_path=edits)
    assert store["txn_1"]["category_update_step"] == "manual"
    assert store["txn_1"]["personal_finance_category"]["detailed"] == COFFEE[1]
    # --full re-derives every row from pristine input — the replay must re-assert it.
    store = _run(tmp_path, monkeypatch, [make_record()], edits_path=edits, full=True)
    assert store["txn_1"]["category_update_step"] == "manual"
    assert store["txn_1"]["personal_finance_category"]["detailed"] == COFFEE[1]


def given_a_second_incremental_run_when_nothing_changed_then_no_new_log_entries(
        tmp_path, monkeypatch, make_record):
    edits = str(tmp_path / "edits.jsonl")
    append_intent(edits, _intent(make_record()))
    _run(tmp_path, monkeypatch, [make_record()], edits_path=edits)
    log = (tmp_path / "log.jsonl").read_text().splitlines()
    assert len(log) == 1                                    # exactly the manual apply
    assert json.loads(log[0])["step"] == "manual"
    _run(tmp_path, monkeypatch, [make_record()], edits_path=edits)
    assert (tmp_path / "log.jsonl").read_text().splitlines() == log   # no churn


def given_a_merchant_intent_when_new_txn_arrives_later_then_categorized_without_llm(
        tmp_path, monkeypatch, make_record):
    edits = str(tmp_path / "edits.jsonl")
    append_intent(edits, _intent(make_record(), scope="merchant"))
    _run(tmp_path, monkeypatch, [make_record()], edits_path=edits)
    llm = _RecordingLLM()
    newcomer = make_record(transaction_id="txn_2", name="SQ *BLUE BOTTLE 99")
    store = _run(tmp_path, monkeypatch, [make_record(), newcomer],
                 edits_path=edits, llm=llm)
    assert store["txn_2"]["category_update_step"] == "manual"
    assert store["txn_2"]["personal_finance_category"]["detailed"] == COFFEE[1]
    assert llm.seen == []          # covered → the expensive stage skipped entirely


def given_no_edits_file_when_run_then_pipeline_unaffected(
        tmp_path, monkeypatch, make_record):
    store = _run(tmp_path, monkeypatch, [make_record()],
                 edits_path=str(tmp_path / "missing.jsonl"))
    assert store["txn_1"]["category_update_step"] == ""


# ── The interactive --edit session ────────────────────────────────────────────

def given_a_store_when_searched_then_text_and_id_lookups_work(make_record, store_of):
    store = store_of(make_record(), make_record(transaction_id="txn_2",
                                                merchant_name="Delta",
                                                name="DELTA AIR",
                                                original_description="DELTA AIR LINES"))
    assert [tid for tid, _ in search_rows(store, "blue bottle")] == ["txn_1"]
    assert [tid for tid, _ in search_rows(store, "id:txn_2")] == ["txn_2"]
    assert search_rows(store, "no such merchant") == []


def _driver(answers):
    answers = list(answers)
    return lambda _prompt="": answers.pop(0)


@pytest.fixture
def _tty(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)


def given_a_scripted_session_when_row_edited_then_intent_appended(
        tmp_path, make_record, _tty):
    edits = str(tmp_path / "edits.jsonl")
    store = {"txn_1": make_record()}
    answers = ["blue bottle", "0",
               str(PRIMARY.index(COFFEE[0])),
               str(DETAILED[COFFEE[0]].index(COFFEE[1])),
               "t", "it is a cafe", "q"]
    n = run_edit_session(store, edits, input_fn=_driver(answers), out=lambda *a: None)
    assert n == 1
    (it,) = resolve_intents(load_intents(edits))
    assert it["scope"] == "transaction"
    assert it["match"] == {"transaction_id": "txn_1"}
    assert it["set"] == {"primary": COFFEE[0], "detailed": COFFEE[1]}
    assert it["note"] == "it is a cafe"
    assert it["source"] == "cli"


def given_a_scripted_session_when_revoke_issued_then_intent_retired(
        tmp_path, make_record, _tty):
    edits = str(tmp_path / "edits.jsonl")
    it = append_intent(edits, _intent(make_record()))
    n = run_edit_session({}, edits, input_fn=_driver([f"revoke {it['id']}", "q"]),
                         out=lambda *a: None)
    assert n == 1
    assert resolve_intents(load_intents(edits)) == []
