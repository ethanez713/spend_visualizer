"""Tests for the CLI — offline only (--no-drive); no network."""
import json

from persister.cli import main


def _write_store(tmp_path, records):
    p = tmp_path / "store.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in records))
    return str(p)


def given_window_cmd_when_run_then_prints_window(tmp_path, make_record, capsys):
    store = _write_store(tmp_path, [
        make_record(transaction_id="a", date="2026-05-20", pending=False),
        make_record(transaction_id="p", date="2026-05-19", pending=True),
    ])
    rc = main(["window", "--store", store])
    out = capsys.readouterr().out
    assert rc == 0
    assert "start_date :" in out and "end_date   :" in out
    assert "pending    : 1 id(s)" in out


def given_reconcile_no_drive_when_run_then_local_only_counts(tmp_path, make_record,
                                                             capsys, monkeypatch):
    # Redirect the audit log so the test never writes the repo's real var/.
    import persister.cli as cli
    monkeypatch.setattr(cli, "log_reconcile", lambda *a, **k: None)
    store = _write_store(tmp_path, [make_record(transaction_id="a")])
    rc = main(["reconcile", "--store", store, "--no-drive"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "local_only  : 1" in out
    assert "remote_only : 0" in out


def given_push_no_drive_when_run_then_noop(tmp_path, make_record, capsys):
    store = _write_store(tmp_path, [make_record()])
    rc = main(["push", "--store", store, "--no-drive"])
    assert rc == 0
    assert "nothing to push" in capsys.readouterr().out
