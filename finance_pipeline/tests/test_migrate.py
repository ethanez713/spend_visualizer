"""Tests for tools/migrate_multiuser.py — the one-off ownership back-fill.

A synthetic monorepo tree is built in tmp_path (tiny xz + jsonl stores, fake token
values only); the script must stamp owners everywhere, be idempotent, abort whole on
any pre-existing different owner, preserve file permissions, and write nothing on
--dry-run. Everything offline.
"""
import json
import lzma
import os

import pytest

from tools.migrate_multiuser import main


def _rec(tid, **over):
    rec = {"transaction_id": tid, "account_id": "acc_1", "date": "2026-01-15",
           "amount": 5.0, "pending": False}
    rec.update(over)
    return rec


def _jsonl(*recs):
    return "".join(json.dumps(r) + "\n" for r in recs)


@pytest.fixture
def world(tmp_path):
    """A pre-migration monorepo tree with fake (non-secret) token values."""
    tsec = tmp_path / "transactions" / ".secrets"
    tdata = tmp_path / "transactions" / "data"
    cdata = tmp_path / "plaid_category_transformer" / "data"
    for d in (tsec, tdata, cdata):
        d.mkdir(parents=True)

    tokens = tsec / "tokens.json"
    tokens.write_text(json.dumps([
        {"access_token": "fake-tok-1", "item_id": "it1", "institution": "Chase"},
        {"access_token": "fake-tok-2", "item_id": "it2", "institution": "Ally"},
    ]))
    os.chmod(tokens, 0o600)

    raw = tdata / "transactions_raw.jsonl.xz"
    with lzma.open(raw, "wt", encoding="utf-8") as f:
        f.write(_jsonl(_rec("t1"), _rec("t2")))
    os.chmod(raw, 0o600)

    durable = tdata / "transactions.jsonl"
    # One record already stamped: a partially-applied earlier run must not abort.
    durable.write_text(_jsonl(_rec("t1"), _rec("t2", txn_owner="Alice")))
    os.chmod(durable, 0o600)

    categorized = cdata / "transactions_categorized.jsonl"
    categorized.write_text(_jsonl(
        _rec("t1", category_update_step="", source_content_hash="abc123")))

    return tmp_path


def _run(world, *extra):
    return main(["--root", str(world), "--owner", "Alice", *extra])


def _read_all(world):
    tokens = json.loads(
        (world / "transactions" / ".secrets" / "tokens.json").read_text())
    with lzma.open(world / "transactions" / "data" / "transactions_raw.jsonl.xz",
                   "rt", encoding="utf-8") as f:
        raw = [json.loads(l) for l in f if l.strip()]
    durable = [json.loads(l) for l in
               (world / "transactions" / "data" / "transactions.jsonl")
               .read_text().splitlines() if l.strip()]
    categorized = [json.loads(l) for l in
                   (world / "plaid_category_transformer" / "data" /
                    "transactions_categorized.jsonl").read_text().splitlines()
                   if l.strip()]
    return tokens, raw, durable, categorized


def given_premigration_tree_when_migrated_then_everything_stamped(world):
    assert _run(world, "--yes") == 0
    tokens, raw, durable, categorized = _read_all(world)

    assert [t["owner"] for t in tokens] == ["Alice", "Alice"]
    assert all(r["txn_owner"] == "Alice" for r in raw)
    assert all(r["txn_owner"] == "Alice" for r in durable)
    assert all(r["txn_owner"] == "Alice" for r in categorized)
    # Existing non-owner content is untouched (incl. the transformer's bookkeeping).
    assert categorized[0]["source_content_hash"] == "abc123"
    assert tokens[0]["access_token"] == "fake-tok-1"


def given_migrated_tree_when_rerun_then_noop(world):
    _run(world, "--yes")
    before = _read_all(world)
    assert _run(world, "--yes") == 0  # idempotent: nothing left to stamp
    assert _read_all(world) == before


def given_secret_files_when_migrated_then_perms_preserved(world):
    _run(world, "--yes")
    for rel in (("transactions", ".secrets", "tokens.json"),
                ("transactions", "data", "transactions_raw.jsonl.xz"),
                ("transactions", "data", "transactions.jsonl")):
        path = world.joinpath(*rel)
        assert oct(path.stat().st_mode & 0o777) == "0o600", path


def given_record_owned_by_someone_else_when_migrated_then_aborts_untouched(world):
    durable = world / "transactions" / "data" / "transactions.jsonl"
    durable.write_text(_jsonl(_rec("t1", txn_owner="someone_else")))
    before = _read_all(world)

    with pytest.raises(SystemExit) as exc:
        _run(world, "--yes")

    assert "someone_else" in str(exc.value)
    assert _read_all(world) == before  # scan-phase abort: NOTHING was written


def given_token_owned_by_someone_else_when_migrated_then_aborts(world):
    tokens = world / "transactions" / ".secrets" / "tokens.json"
    entries = json.loads(tokens.read_text())
    entries[0]["owner"] = "someone_else"
    tokens.write_text(json.dumps(entries))
    before = _read_all(world)

    with pytest.raises(SystemExit):
        _run(world, "--yes")
    assert _read_all(world) == before


def given_dry_run_when_migrated_then_nothing_written(world):
    before = _read_all(world)
    assert _run(world, "--dry-run") == 0
    assert _read_all(world) == before


def given_missing_files_when_migrated_then_skipped_not_crashed(world, capsys):
    # A fresh checkout may lack e.g. the categorized store — skip it, do the rest.
    (world / "plaid_category_transformer" / "data" /
     "transactions_categorized.jsonl").unlink()

    assert _run(world, "--yes") == 0

    tokens, raw, durable, _ = (json.loads(
        (world / "transactions" / ".secrets" / "tokens.json").read_text()), None, None, None)
    assert all(t["owner"] == "Alice" for t in tokens)
    assert "skip (missing)" in capsys.readouterr().out


def given_bad_owner_name_when_migrated_then_rejected(world):
    with pytest.raises(SystemExit):
        main(["--root", str(world), "--owner", "../evil", "--yes"])
