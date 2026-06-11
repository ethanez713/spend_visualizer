"""Tests for store.py — JSONL round-trip, atomic/sorted writes, derive_csv, dedupe."""
import json

from persister.store import (
    dedupe_supersede,
    derive_csv,
    load_jsonl,
    load_jsonl_bytes,
    save_jsonl,
)


def given_store_when_save_then_load_roundtrips(tmp_path, make_record):
    path = str(tmp_path / "s.jsonl")
    store = {"txn_1": make_record(), "txn_2": make_record(transaction_id="txn_2")}
    save_jsonl(path, store)
    assert load_jsonl(path) == store


def given_missing_file_when_load_then_empty_dict(tmp_path):
    assert load_jsonl(str(tmp_path / "nope.jsonl")) == {}


def given_records_when_save_then_sorted_by_date_then_key(tmp_path, make_record):
    path = str(tmp_path / "s.jsonl")
    store = {
        "b": make_record(transaction_id="b", date="2026-03-01"),
        "a": make_record(transaction_id="a", date="2026-01-01"),
        "c": make_record(transaction_id="c", date="2026-01-01"),  # ties → key order
    }
    save_jsonl(path, store)
    ids = [json.loads(line)["transaction_id"] for line in
           (tmp_path / "s.jsonl").read_text().splitlines()]
    assert ids == ["a", "c", "b"]


def given_save_when_inspecting_then_no_leftover_tmp(tmp_path, make_record):
    save_jsonl(str(tmp_path / "s.jsonl"), {"txn_1": make_record()})
    assert [p.name for p in tmp_path.iterdir()] == ["s.jsonl"]  # tmp replaced, none left


def given_save_when_parent_missing_then_created(tmp_path, make_record):
    nested = str(tmp_path / "deep" / "dir" / "s.jsonl")
    save_jsonl(nested, {"txn_1": make_record()})
    assert load_jsonl(nested) == {"txn_1": make_record()}


def given_bad_line_when_load_then_skipped_not_crash(tmp_path, make_record):
    path = tmp_path / "s.jsonl"
    good = json.dumps(make_record())
    path.write_text(good + "\n{ this is not json }\n\n")
    loaded = load_jsonl(str(path))
    assert set(loaded) == {"txn_1"}  # bad + blank lines skipped, good kept


def given_line_missing_key_when_load_then_skipped(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(json.dumps({"no_key": 1}) + "\n")
    assert load_jsonl(str(path)) == {}


def given_bytes_when_load_jsonl_bytes_then_parsed(make_record):
    blob = (json.dumps(make_record()) + "\n").encode("utf-8")
    assert load_jsonl_bytes(blob) == {"txn_1": make_record()}


def given_none_when_load_jsonl_bytes_then_empty(make_record):
    assert load_jsonl_bytes(None) == {}
    assert load_jsonl_bytes(b"") == {}


def given_settled_pending_when_dedupe_then_pending_dropped(make_record):
    store = {
        "pend_1": make_record(transaction_id="pend_1", pending=True),
        "post_1": make_record(transaction_id="post_1", pending=False,
                              pending_transaction_id="pend_1"),
        "keep_pend": make_record(transaction_id="keep_pend", pending=True),  # no posted
    }
    out = dedupe_supersede(store)
    assert set(out) == {"post_1", "keep_pend"}  # superseded pending dropped, rest kept


def given_derive_csv_when_written_then_columns_and_injection_guard(tmp_path, make_record):
    path = str(tmp_path / "out.csv")
    store = {
        "txn_1": make_record(name="=cmd|' /C calc'!A0", amount=-5.0),
    }
    cols = ["transaction_id", "name", "amount"]
    derive_csv(store, path, cols,
               row_fn=lambda r: {"transaction_id": r["transaction_id"],
                                 "name": r["name"], "amount": r["amount"]})
    lines = (tmp_path / "out.csv").read_text().splitlines()
    assert lines[0] == "transaction_id,name,amount"  # column order honoured
    # Leading '=' neutralised with a quote; negative amount stays numeric (not quoted).
    assert lines[1].startswith("txn_1,'=cmd")
    assert lines[1].endswith(",-5.0")
