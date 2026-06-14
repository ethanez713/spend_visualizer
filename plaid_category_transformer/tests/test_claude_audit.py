"""The Claude audit ritual: export un-reviewed rows, apply verdicts as review flags.

Claude is a REVIEWER like the (now-default-off) local LLM — it only raises
``category_review_*`` flags (source="claude") for the human to adjudicate via --review; it
never overwrites a category. Reviewed rows are stamped ``claude_audited_at`` so the next
ritual skips them. All offline — no Drive, no network.
"""
import json

import persister

from src.claude_audit import (
    CLAUDE_SOURCE,
    apply_verdicts,
    audit_scan,
    export_bundle,
    export_queue,
    load_verdicts,
    rows_for_claude,
)
from src.incremental import source_hash
from src.rules import MerchantMemory
from src.schema import COLUMNS, row_fn
from src.transformer import claude_apply_run, claude_export_run


# ── selection ─────────────────────────────────────────────────────────────────

def given_mixed_store_when_rows_for_claude_then_only_unreviewed_posted(store_of):
    store = store_of(
        {"transaction_id": "fresh"},
        {"transaction_id": "seen", "claude_audited_at": "2026-06-01T00:00:00+00:00"},
        {"transaction_id": "pend", "pending": True},
    )
    ids = {tid for tid, _ in rows_for_claude(store)}
    assert ids == {"fresh"}                       # seen=stamped, pend=pending → both skipped


def given_full_when_rows_for_claude_then_all_posted_even_if_seen(store_of):
    store = store_of(
        {"transaction_id": "fresh"},
        {"transaction_id": "seen", "claude_audited_at": "2026-06-01T00:00:00+00:00"},
        {"transaction_id": "pend", "pending": True},
    )
    ids = {tid for tid, _ in rows_for_claude(store, full=True)}
    assert ids == {"fresh", "seen"}               # full re-includes seen; pending still out


def given_claude_audited_at_when_hashed_then_source_hash_unchanged(make_record):
    # The new column is bookkeeping: it must NOT change the incremental source hash,
    # or stamping it would re-audit every row.
    rec = make_record()
    before = source_hash(rec)
    rec["claude_audited_at"] = "2026-06-01T00:00:00+00:00"
    assert source_hash(rec) == before


# ── export ────────────────────────────────────────────────────────────────────

def given_store_when_exported_then_queue_has_unreviewed_rows(tmp_path, store_of):
    store = store_of(
        {"transaction_id": "a", "merchant_name": "Chipotle", "amount": 14.2},
        {"transaction_id": "b", "claude_audited_at": "2026-06-01T00:00:00+00:00"},
    )
    queue = tmp_path / "queue.jsonl"
    n = export_queue(store, str(queue))

    assert n == 1
    rows = [json.loads(line) for line in queue.read_text().splitlines()]
    assert rows[0]["transaction_id"] == "a"
    assert rows[0]["merchant_name"] == "Chipotle"
    assert "current_primary" in rows[0] and "current_detailed" in rows[0]


# ── verdict application ───────────────────────────────────────────────────────

def given_flag_verdict_when_applied_then_review_flag_set_and_stamped(store_of):
    store = store_of({"transaction_id": "a", "personal_finance_category": {
        "primary": "GENERAL_MERCHANDISE",
        "detailed": "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE", "confidence_level": "LOW"}})
    summary = apply_verdicts(store, [{"transaction_id": "a", "verdict": "flag",
                                      "primary": "FOOD_AND_DRINK",
                                      "detailed": "FOOD_AND_DRINK_RESTAURANT",
                                      "reason": "chipotle is a restaurant"}])
    rec = store["a"]
    assert summary["flagged"] == 1
    assert rec["category_review_flag"] == "1"
    assert rec["category_review_primary"] == "FOOD_AND_DRINK"
    assert rec["category_review_source"] == CLAUDE_SOURCE
    assert rec["claude_audited_at"]                                  # stamped
    assert rec["personal_finance_category"]["primary"] == "GENERAL_MERCHANDISE"  # untouched


def given_ok_verdict_when_applied_then_only_stamped_no_flag(store_of):
    store = store_of({"transaction_id": "a"})
    summary = apply_verdicts(store, [{"transaction_id": "a", "verdict": "ok"}])
    rec = store["a"]
    assert summary == {"flagged": 0, "ok": 1, "invalid": [], "unknown": [],
                       "log": [{"transaction_id": "a", "verdict": "ok"}]}
    assert rec["claude_audited_at"] and rec["category_review_flag"] == ""


def given_flag_equal_to_current_when_applied_then_counts_as_ok(store_of):
    store = store_of({"transaction_id": "a", "personal_finance_category": {
        "primary": "FOOD_AND_DRINK", "detailed": "FOOD_AND_DRINK_COFFEE",
        "confidence_level": "LOW"}})
    summary = apply_verdicts(store, [{"transaction_id": "a", "verdict": "flag",
                                      "primary": "FOOD_AND_DRINK",
                                      "detailed": "FOOD_AND_DRINK_COFFEE"}])
    assert summary["flagged"] == 0 and summary["ok"] == 1
    assert store["a"]["category_review_flag"] == "" and store["a"]["claude_audited_at"]


def given_invalid_detailed_when_applied_then_snapped_to_other(store_of):
    store = store_of({"transaction_id": "a", "personal_finance_category": {
        "primary": "GENERAL_MERCHANDISE", "detailed": "GENERAL_MERCHANDISE_ELECTRONICS",
        "confidence_level": "LOW"}})
    summary = apply_verdicts(store, [{"transaction_id": "a", "verdict": "flag",
                                      "primary": "FOOD_AND_DRINK",
                                      "detailed": "NOT_A_REAL_DETAILED"}])
    assert summary["flagged"] == 1
    assert store["a"]["category_review_detailed"] == "FOOD_AND_DRINK_OTHER_FOOD_AND_DRINK"


def given_unknown_primary_when_applied_then_invalid_and_unstamped(store_of):
    store = store_of({"transaction_id": "a"})
    summary = apply_verdicts(store, [{"transaction_id": "a", "verdict": "flag",
                                      "primary": "NONSENSE", "detailed": "NONSENSE_X"}])
    assert summary["invalid"] == ["a"] and summary["flagged"] == 0
    assert store["a"].get("claude_audited_at") in (None, "")        # left for retry


def given_unknown_tid_when_applied_then_reported_unknown(store_of):
    store = store_of({"transaction_id": "a"})
    summary = apply_verdicts(store, [{"transaction_id": "ghost", "verdict": "ok"}])
    assert summary["unknown"] == ["ghost"] and summary["ok"] == 0


def given_verdicts_file_when_loaded_then_blank_lines_tolerated(tmp_path):
    p = tmp_path / "v.jsonl"
    p.write_text('{"transaction_id": "a", "verdict": "ok"}\n\n'
                 '{"transaction_id": "b", "verdict": "flag"}\n')
    assert [v["transaction_id"] for v in load_verdicts(str(p))] == ["a", "b"]


# ── CLI wrappers (offline: do_drive=False) ────────────────────────────────────

def given_store_and_verdicts_when_apply_run_then_persisted_with_flag(tmp_path, store_of):
    store = store_of({"transaction_id": "a", "personal_finance_category": {
        "primary": "GENERAL_MERCHANDISE",
        "detailed": "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE", "confidence_level": "LOW"}})
    out_jsonl = tmp_path / "cat.jsonl"
    persister.save_jsonl(str(out_jsonl), store)
    verdicts = tmp_path / "v.jsonl"
    verdicts.write_text(json.dumps({"transaction_id": "a", "verdict": "flag",
                                    "primary": "FOOD_AND_DRINK",
                                    "detailed": "FOOD_AND_DRINK_RESTAURANT",
                                    "reason": "restaurant"}) + "\n")

    claude_apply_run(out_jsonl=str(out_jsonl), out_csv=str(tmp_path / "cat.csv"),
                     flags_csv=str(tmp_path / "flags.csv"), verdicts_path=str(verdicts),
                     edits_path=None, do_drive=False)

    reloaded = persister.load_jsonl(str(out_jsonl))
    assert reloaded["a"]["category_review_source"] == CLAUDE_SOURCE
    assert reloaded["a"]["claude_audited_at"]
    # the new column rides the CSV schema too
    header = (tmp_path / "cat.csv").read_text().splitlines()[0].split(",")
    assert header == COLUMNS and "claude_audited_at" in header


def given_store_when_export_run_then_queue_and_scan_written(tmp_path, store_of):
    store = store_of({"transaction_id": "a"}, {"transaction_id": "b"})
    out_jsonl = tmp_path / "cat.jsonl"
    persister.save_jsonl(str(out_jsonl), store)
    queue, scan = tmp_path / "queue.jsonl", tmp_path / "scan.json"

    claude_export_run(out_jsonl=str(out_jsonl), queue_path=str(queue), scan_path=str(scan))

    assert len({json.loads(l)["transaction_id"] for l in queue.read_text().splitlines()}) == 2
    bundle = json.loads(scan.read_text())
    assert "findings" in bundle and bundle["store_rows"] == 2


# ── deterministic pre-scan (the extra sweeps, one pass) ───────────────────────

def given_store_when_scanned_then_taxonomy_sign_and_uncategorized_found(store_of):
    store = store_of(
        {"transaction_id": "legacy", "amount": -100.0, "personal_finance_category": {
            "primary": "INCOME", "detailed": "INCOME_SALARY", "confidence_level": "LOW"}},
        {"transaction_id": "badsign", "amount": 250.0, "personal_finance_category": {
            "primary": "INCOME", "detailed": "INCOME_WAGES", "confidence_level": "LOW"}},
        {"transaction_id": "uncat", "personal_finance_category": {}},
    )
    f = audit_scan(store)
    assert [r["transaction_id"] for r in f["taxonomy_invalid"]] == ["legacy"]
    assert [r["transaction_id"] for r in f["sign_violations"]] == ["badsign"]
    assert f["uncategorized"] == ["uncat"]


def given_same_entity_two_categories_when_scanned_then_inconsistent(store_of):
    store = store_of(
        {"transaction_id": "x1", "merchant_entity_id": "ent_x",
         "personal_finance_category": {"primary": "FOOD_AND_DRINK",
                                       "detailed": "FOOD_AND_DRINK_RESTAURANT"}},
        {"transaction_id": "x2", "merchant_entity_id": "ent_x",
         "personal_finance_category": {"primary": "GENERAL_MERCHANDISE",
                                       "detailed": "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE"}},
    )
    f = audit_scan(store)
    assert len(f["entity_inconsistent"]) == 1
    assert f["entity_inconsistent"][0]["merchant_entity_id"] == "ent_x"


def given_memory_disagrees_with_store_when_scanned_then_conflict(store_of):
    store = store_of({"transaction_id": "m", "merchant_entity_id": "ent_m",
                      "personal_finance_category": {
                          "primary": "GENERAL_MERCHANDISE",
                          "detailed": "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE"}})
    mem = MerchantMemory(path=None)
    mem.remember(store["m"], "FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE")  # memory says coffee
    f = audit_scan(store, mem)
    assert len(f["memory_conflicts"]) == 1
    assert f["memory_conflicts"][0]["memory_says"] == "FOOD_AND_DRINK/FOOD_AND_DRINK_COFFEE"


def given_old_pending_and_big_amounts_when_scanned_then_flagged(store_of):
    store = store_of(
        {"transaction_id": "stale", "pending": True, "date": "2020-01-01", "amount": 5.0},
        {"transaction_id": "huge", "pending": False, "date": "2026-06-01", "amount": 9999.0},
        {"transaction_id": "small", "pending": False, "date": "2026-06-01", "amount": 3.0},
    )
    f = audit_scan(store, outlier_n=1)
    assert f["stale_pending"] == ["stale"]
    assert f["amount_outliers"][0]["transaction_id"] == "huge"


def given_export_bundle_when_written_then_both_artifacts_present(tmp_path, store_of):
    store = store_of(
        {"transaction_id": "ok"},
        {"transaction_id": "legacy", "personal_finance_category": {
            "primary": "INCOME", "detailed": "INCOME_SALARY", "confidence_level": "LOW"}},
    )
    queue, scan = tmp_path / "q.jsonl", tmp_path / "s.json"
    n, findings = export_bundle(store, str(queue), str(scan))
    assert n == 2                                              # both posted, unreviewed
    assert [r["transaction_id"] for r in findings["taxonomy_invalid"]] == ["legacy"]
    assert json.loads(scan.read_text())["findings"]["taxonomy_invalid"]
