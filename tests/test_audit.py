"""Tests for audit.py — append-only reconcile log."""
import json
import os
import stat

from persister.audit import log_reconcile
from persister.reconcile import reconcile


def given_report_when_logged_then_line_appended_with_counts(tmp_path, make_record):
    path = str(tmp_path / "var" / "reconcile_log.jsonl")
    local = {"a": make_record(transaction_id="a"),
             "c": make_record(transaction_id="c", amount=1.0)}
    remote = {"b": make_record(transaction_id="b"),
              "c": make_record(transaction_id="c", amount=2.0)}
    rep = reconcile(local, remote)

    log_reconcile(path, rep, source="test")
    log_reconcile(path, rep, source="test")  # appends, doesn't overwrite

    lines = [json.loads(l) for l in open(path).read().splitlines()]
    assert len(lines) == 2
    e = lines[0]
    assert e["source"] == "test"
    assert e["local_only"] == 1 and e["remote_only"] == 1 and e["conflicts"] == 1
    assert e["conflict_keys"] == ["c"]
    assert "timestamp" in e


def given_log_when_written_then_owner_only_perms(tmp_path, make_record):
    path = str(tmp_path / "reconcile_log.jsonl")
    rep = reconcile({}, {})
    log_reconcile(path, rep, source="test")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600
