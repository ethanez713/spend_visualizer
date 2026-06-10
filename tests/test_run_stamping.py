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


def _run(tmp_path, monkeypatch, *, llm_stub=None, no_llm=False, make_record=None):
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


def given_pending_stamp_when_next_run_classifies_then_row_reaudited(make_record):
    rec = make_record()
    prior = {"txn_1": dict(rec, **{SOURCE_HASH_FIELD: HASH_PENDING_LLM})}

    delta = classify({"txn_1": rec}, prior)

    assert "txn_1" in delta.to_process     # sentinel never matches a real hash
    assert delta.changed == ["txn_1"]
