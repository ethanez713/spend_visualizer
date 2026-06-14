"""End-to-end two-writer scenarios over a shared (in-memory) Drive.

Two "machines" — a scheduled server (rules-only, ``--llm-defer``) and a desktop
LLM box — each with their own store, worklist, and intent log, share one fake
Drive head. Every scenario drives the REAL ``run()`` path on both sides
(adopt → audit → replay → push) and asserts the no-data-loss properties the
adopt design exists for: local-ahead work survives, deferred rows get their LLM
pass exactly once, and human corrections cross machines without being reverted.
Everything stays offline; "Drive" is a dict.
"""
import json
from pathlib import Path

import pytest

import persister
import src.transformer as tr
from src.incremental import HASH_PENDING_LLM, SOURCE_HASH_FIELD


class StubLLM:
    """CategoryLLM stand-in: runs 'successfully' and flags nothing."""

    def __init__(self, ran_ok: bool = True):
        self._outcome = ran_ok
        self.ran_ok = False

    def categorize(self, items):
        self.ran_ok = self._outcome
        return {}


class SharedDrive:
    files: dict = {}


class FakeDriveSync:
    """DriveSync stand-in backed by the shared dict — one 'Drive' for all machines."""

    def __init__(self, file_name, folder_name="transactions_archive", secrets_dir=None):
        self.file_name = file_name

    def pull(self):
        return SharedDrive.files.get(self.file_name)

    def push(self, local_path, mime="application/x-ndjson"):
        SharedDrive.files[self.file_name] = Path(local_path).read_bytes()
        return f"https://drive.example/{self.file_name}"


@pytest.fixture(autouse=True)
def shared_drive(monkeypatch):
    SharedDrive.files = {}
    monkeypatch.setattr(persister, "DriveSync", FakeDriveSync)


class Machine:
    """One writer: its own data dir (store, worklist, intent log) + the shared Drive."""

    def __init__(self, root: Path, name: str):
        d = root / name
        d.mkdir()
        self.input = str(d / "input.jsonl")
        self.out_jsonl = str(d / "categorized.jsonl")
        self.out_csv = str(d / "categorized.csv")
        self.flags_csv = str(d / "flags.csv")
        self.log = str(d / "log.jsonl")
        self.edits = str(d / "manual_edits.jsonl")
        self.conflicts = str(d / "adopt_conflicts.jsonl")

    def write_input(self, store: dict) -> None:
        Path(self.input).write_text(
            "\n".join(json.dumps(r) for r in store.values()) + "\n")

    def run(self, monkeypatch, *, llm=None, defer=False, drive=True) -> dict:
        monkeypatch.setattr(tr, "CategoryLLM", lambda debug=False: llm)
        tr.run(input_path=self.input, out_jsonl=self.out_jsonl, out_csv=self.out_csv,
               flags_csv=self.flags_csv, log_path=self.log,
               levels={"LOW", "MEDIUM", "HIGH", "VERY_HIGH", "UNKNOWN"},
               memory_path=None, do_drive=drive,
               no_llm=(llm is None and not defer), defer_llm=defer,
               debug=False, edits_path=self.edits)
        return persister.load_jsonl(self.out_jsonl)


def _head() -> dict:
    return persister.load_jsonl_bytes(SharedDrive.files.get(tr.DRIVE_JSONL_NAME))


@pytest.fixture
def machines(tmp_path, make_record):
    server, desktop = Machine(tmp_path, "server"), Machine(tmp_path, "desktop")
    raw = {"txn_1": make_record()}          # both fetched the same raw store
    server.write_input(raw)
    desktop.write_input(raw)
    return server, desktop


def given_server_deferred_rows_when_desktop_audits_then_server_adopts(
        machines, monkeypatch):
    # The steady-state split: server defers the LLM nightly; the desktop run picks
    # the pending rows up exactly once; the server's next night adopts the result
    # instead of clobbering it back to pending.
    server, desktop = machines

    server.run(monkeypatch, defer=True)
    assert _head()["txn_1"][SOURCE_HASH_FIELD] == HASH_PENDING_LLM

    desktop.run(monkeypatch, llm=StubLLM(ran_ok=True))
    server_store = server.run(monkeypatch, defer=True)

    assert server_store["txn_1"][SOURCE_HASH_FIELD] != HASH_PENDING_LLM
    assert _head()["txn_1"][SOURCE_HASH_FIELD] != HASH_PENDING_LLM


def given_local_store_ahead_of_head_when_drive_run_then_local_work_survives(
        machines, monkeypatch):
    # THE no-data-loss case: the desktop audited offline (or its push failed), so
    # its local store is AHEAD of the Drive head. The next Drive run must keep the
    # newer local work — and log both versions of the conflict it resolved.
    server, desktop = machines

    server.run(monkeypatch, defer=True)                          # head: pending
    desktop.run(monkeypatch, llm=StubLLM(ran_ok=True), drive=False)  # newer, unpushed
    desktop_store = desktop.run(monkeypatch, llm=StubLLM(ran_ok=True))

    assert desktop_store["txn_1"][SOURCE_HASH_FIELD] != HASH_PENDING_LLM
    assert _head()["txn_1"][SOURCE_HASH_FIELD] != HASH_PENDING_LLM

    entry = json.loads(Path(desktop.conflicts).read_text().splitlines()[0])
    assert entry["kept"] == "local"                  # the resolver chose our work
    assert entry["remote"][SOURCE_HASH_FIELD] == HASH_PENDING_LLM  # loser kept too


def given_stale_machine_when_drive_run_then_head_cannot_shrink(
        machines, monkeypatch, make_record):
    # "Always appending", end to end: a machine whose raw input is missing POSTED
    # rows (stale fetch, truncated store) must stop rather than push a shrunken
    # head. Settled pendings remain the only legitimate shrinkage (prune gate).
    server, desktop = machines
    raw = {"txn_1": make_record(),
           "txn_2": make_record(transaction_id="txn_2")}
    server.write_input(raw)
    server.run(monkeypatch, defer=True)               # head: 2 posted rows
    desktop.write_input({"txn_1": make_record()})     # stale: missing txn_2

    with pytest.raises(SystemExit) as exc:
        desktop.run(monkeypatch, llm=StubLLM(ran_ok=True))

    assert "prune gate" in str(exc.value)
    assert set(_head()) == {"txn_1", "txn_2"}         # head untouched


def given_server_correction_and_desktop_audit_then_both_survive_the_cycle(
        machines, monkeypatch):
    # A human correction captured on the server (its intent log only) and a
    # desktop LLM pass must BOTH survive a full server → desktop → server cycle:
    # the intent log union-merge prevents the stale desktop log from reverting
    # the correction, and the LLM result isn't clobbered back to pending.
    server, desktop = machines
    target = ("ENTERTAINMENT", "ENTERTAINMENT_MUSIC_AND_AUDIO")
    Path(server.edits).write_text(json.dumps({
        "id": "ui-1", "action": "edit", "scope": "transaction",
        "match": {"transaction_id": "txn_1"},
        "set": {"primary": target[0], "detailed": target[1]}}) + "\n")

    server.run(monkeypatch, defer=True)                    # correction + log pushed
    desktop_store = desktop.run(monkeypatch, llm=StubLLM(ran_ok=True))
    server_store = server.run(monkeypatch, defer=True)

    for store in (desktop_store, server_store, _head()):
        pfc = store["txn_1"]["personal_finance_category"]
        assert (pfc["primary"], pfc["detailed"]) == target
        assert store["txn_1"]["category_update_step"] == "manual"
    assert server_store["txn_1"][SOURCE_HASH_FIELD] != HASH_PENDING_LLM
    # The desktop's log adopted the server's intent — the anti-revert guarantee.
    assert json.loads(Path(desktop.edits).read_text().splitlines()[0])["id"] == "ui-1"
