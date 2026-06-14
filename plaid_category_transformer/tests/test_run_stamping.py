"""LLM-outage stamping: rows from a run whose LLM stage silently skipped must be
re-audited next run, never marked as fully audited.

An explicit --no-llm run (llm=None) stamps real hashes — rules-only was deliberate.
"""
import json

import persister
import src.transformer as tr
from src.incremental import HASH_PENDING_LLM, SOURCE_HASH_FIELD, classify, source_hash


class StubLLM:
    """CategoryLLM stand-in with a controllable ran_ok outcome."""

    def __init__(self, ran_ok: bool):
        self._outcome = ran_ok
        self.ran_ok = False

    def categorize(self, items):
        self.ran_ok = self._outcome
        return {}


def _write_input(tmp_path, store):
    p = tmp_path / "input.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in store.values()) + "\n")
    return str(p)


def _run(tmp_path, monkeypatch, *, llm_stub=None, no_llm=False, defer_llm=False,
         make_record=None):
    monkeypatch.setattr(tr, "CategoryLLM", lambda debug=False: llm_stub)
    out_jsonl = str(tmp_path / "out.jsonl")
    tr.run(
        input_path=_write_input(tmp_path, {"txn_1": make_record()}),
        out_jsonl=out_jsonl,
        out_csv=str(tmp_path / "out.csv"),
        flags_csv=str(tmp_path / "flags.csv"),
        log_path=str(tmp_path / "log.jsonl"),
        levels={"LOW", "MEDIUM", "HIGH", "VERY_HIGH", "UNKNOWN"},
        memory_path=None,
        do_drive=False,
        no_llm=no_llm,
        defer_llm=defer_llm,
        debug=False,
    )
    return persister.load_jsonl(out_jsonl)


def given_llm_stage_skipped_when_run_then_rows_stamped_pending(tmp_path, monkeypatch,
                                                               make_record):
    store = _run(tmp_path, monkeypatch, llm_stub=StubLLM(ran_ok=False),
                 make_record=make_record)
    assert store["txn_1"][SOURCE_HASH_FIELD] == HASH_PENDING_LLM


def given_llm_stage_completed_when_run_then_rows_stamped_with_real_hash(
        tmp_path, monkeypatch, make_record):
    store = _run(tmp_path, monkeypatch, llm_stub=StubLLM(ran_ok=True),
                 make_record=make_record)
    assert store["txn_1"][SOURCE_HASH_FIELD] == source_hash(make_record())


def given_explicit_no_llm_when_run_then_rows_stamped_with_real_hash(
        tmp_path, monkeypatch, make_record):
    store = _run(tmp_path, monkeypatch, llm_stub=None, no_llm=True,
                 make_record=make_record)
    assert store["txn_1"][SOURCE_HASH_FIELD] == source_hash(make_record())


def given_llm_defer_when_run_then_rows_stamped_pending(tmp_path, monkeypatch,
                                                       make_record):
    # The scheduled-server mode: rules apply now, but the rows stay pending so the
    # next LLM-enabled run (e.g. the desktop) audits them — unlike --no-llm, which
    # stamps rules-only as deliberate and final.
    store = _run(tmp_path, monkeypatch, llm_stub=None, defer_llm=True,
                 make_record=make_record)
    assert store["txn_1"][SOURCE_HASH_FIELD] == HASH_PENDING_LLM


def given_pending_stamp_when_next_run_classifies_then_row_reaudited(make_record):
    rec = make_record()
    prior = {"txn_1": dict(rec, **{SOURCE_HASH_FIELD: HASH_PENDING_LLM})}

    delta = classify({"txn_1": rec}, prior)

    assert "txn_1" in delta.to_process     # sentinel never matches a real hash
    assert delta.changed == ["txn_1"]


# ── Audit-recency stamping (the adopt-time conflict resolver's signal) ────────
# Invariant under test: category_audited_at moves ONLY when audit content
# actually changes. A no-op re-run must not refresh it, or a stale machine
# could outrank the other machine's real work at adopt time.

def given_new_row_when_processed_then_audit_stamp_written(tmp_path, monkeypatch,
                                                          make_record):
    store = _run(tmp_path, monkeypatch, llm_stub=None, no_llm=True,
                 make_record=make_record)
    assert store["txn_1"]["category_audited_at"]


def given_pending_row_rerun_unchanged_then_stamp_not_refreshed(tmp_path, monkeypatch,
                                                               make_record):
    # Two consecutive --llm-defer runs over the same source: the second run
    # re-processes the pending row (rules re-apply, same result) — no real change,
    # so the stamp must hold still.
    first = _run(tmp_path, monkeypatch, llm_stub=None, defer_llm=True,
                 make_record=make_record)
    second = _run(tmp_path, monkeypatch, llm_stub=None, defer_llm=True,
                  make_record=make_record)
    assert (second["txn_1"]["category_audited_at"]
            == first["txn_1"]["category_audited_at"])


def given_pending_row_when_llm_completes_then_stamp_refreshed(tmp_path, monkeypatch,
                                                              make_record):
    first = _run(tmp_path, monkeypatch, llm_stub=None, defer_llm=True,
                 make_record=make_record)
    second = _run(tmp_path, monkeypatch, llm_stub=StubLLM(ran_ok=True),
                  make_record=make_record)
    assert (second["txn_1"]["category_audited_at"]
            > first["txn_1"]["category_audited_at"])   # ISO strings order correctly


def given_audit_stamp_differs_then_source_hash_unchanged(make_record):
    # The stamp is provenance, not source content — it must never trigger a re-audit.
    rec = make_record()
    stamped = dict(rec, category_audited_at="2026-06-01T00:00:00.000000+00:00")
    assert source_hash(rec) == source_hash(stamped)


def given_correction_applied_then_stamp_written(make_record):
    from src.schema import set_provenance
    rec = make_record()
    set_provenance(rec, "FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE", "review", "r", "HIGH")
    assert rec["category_audited_at"]


def given_flag_raised_then_stamp_written(make_record):
    from src.schema import set_review_flag
    rec = make_record()
    set_review_flag(rec, "FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE", "r", "HIGH", "llm")
    assert rec["category_audited_at"]


def given_identical_flag_reraised_then_stamp_not_refreshed(make_record):
    from src.schema import set_review_flag
    rec = make_record()
    set_review_flag(rec, "FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE", "r", "HIGH", "llm")
    first = rec["category_audited_at"]
    set_review_flag(rec, "FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE", "r", "HIGH", "llm")
    assert rec["category_audited_at"] == first


def given_flag_cleared_then_stamp_refreshed(make_record):
    from src.schema import clear_review_flag, set_review_flag
    rec = make_record()
    set_review_flag(rec, "FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE", "r", "HIGH", "llm")
    first = rec["category_audited_at"]
    clear_review_flag(rec)                 # an adjudication (reject) is real work
    assert rec["category_audited_at"] > first


def given_unflagged_row_when_clear_then_no_stamp(make_record):
    from src.schema import clear_review_flag
    rec = make_record()
    clear_review_flag(rec)                 # pure no-op — nothing was adjudicated
    assert rec["category_audited_at"] == ""
