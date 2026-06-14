"""The prune-legitimacy gate: only settled pendings may vanish from the input.

In the durable input store, posted rows are never deleted upstream (stale ones
are only flagged) — so a posted row missing from the input means a stale or
truncated raw store, and pruning it would shrink the shared store. The run must
stop before any audit, write, or push; pending rows keep pruning normally.
"""
import json

import pytest

import persister
import src.transformer as tr


def _write_input(tmp_path, store):
    p = tmp_path / "input.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in store.values()) + "\n")
    return str(p)


def _run(tmp_path, store):
    out_jsonl = str(tmp_path / "out.jsonl")
    tr.run(input_path=_write_input(tmp_path, store), out_jsonl=out_jsonl,
           out_csv=str(tmp_path / "out.csv"), flags_csv=str(tmp_path / "flags.csv"),
           log_path=str(tmp_path / "log.jsonl"),
           levels={"LOW", "MEDIUM", "HIGH", "VERY_HIGH", "UNKNOWN"},
           memory_path=None, do_drive=False, no_llm=True, debug=False)
    return persister.load_jsonl(out_jsonl)


def given_settled_pending_gone_from_input_when_run_then_pruned_normally(
        tmp_path, make_record):
    _run(tmp_path, {"posted": make_record(transaction_id="posted"),
                    "pend": make_record(transaction_id="pend", pending=True)})

    store = _run(tmp_path, {"posted": make_record(transaction_id="posted")})

    assert "pend" not in store          # settlement is the legitimate shrinkage
    assert "posted" in store


def given_posted_row_gone_from_input_when_run_then_stops_before_write(
        tmp_path, make_record):
    first = _run(tmp_path, {"t1": make_record(transaction_id="t1"),
                            "t2": make_record(transaction_id="t2")})

    with pytest.raises(SystemExit) as exc:
        _run(tmp_path, {"t1": make_record(transaction_id="t1")})

    assert "prune gate" in str(exc.value)
    assert "t2" in str(exc.value)       # names the rows so the user can verify
    assert persister.load_jsonl(str(tmp_path / "out.jsonl")) == first  # untouched
