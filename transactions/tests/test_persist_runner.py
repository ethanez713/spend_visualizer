"""Tests for run_persist: the orchestration that drives the persister library.

DriveSync, fetch_window, the Plaid client, and account-meta lookup are all stubbed; the
durable store is written into a tmp data_dir and the reconcile log is redirected to tmp,
so nothing touches the network or real data/output paths. The tests assert the pipeline
sequence: reconcile → (conflict ⇒ window + refetch + merge_golden) → dedupe → save_jsonl →
derive_csv → Drive push, and that no Drive calls happen when do_drive=False.
"""
import json
import os

import pytest

import persister
import src.persist_runner as pr


class FakeDriveSync:
    """Records pull/push and returns canned remote bytes; appends pushes to a shared order list."""
    remote_bytes = None          # JSONL bytes that pull() returns (the remote store)
    order = None                 # shared list capturing call order across instances
    instances = None             # all constructed instances

    def __init__(self, file_name, folder_name="transactions_archive", secrets_dir=None):
        self.file_name = file_name
        self.folder_name = folder_name
        self.secrets_dir = secrets_dir
        self.pulled = False
        self.pushes = []
        FakeDriveSync.instances.append(self)

    def pull(self):
        self.pulled = True
        return FakeDriveSync.remote_bytes

    def push(self, local_path, mime="application/x-ndjson"):
        self.pushes.append((local_path, mime))
        FakeDriveSync.order.append(f"push:{self.file_name}")
        return f"https://drive.example/{self.file_name}"


def _rec(tid, *, amount=1.0, pending=False, pending_txn_id=None, date="2026-01-10"):
    r = {"transaction_id": tid, "date": date, "pending": pending, "amount": amount,
         "account_id": "acc_1", "personal_finance_category": {"primary": "FOOD_AND_DRINK",
         "detailed": "FOOD_AND_DRINK_GROCERIES", "confidence_level": "HIGH"}}
    if pending_txn_id:
        r["pending_transaction_id"] = pending_txn_id
    return r


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """Stub every external dependency of run_persist; isolate all I/O to tmp."""
    order = []
    FakeDriveSync.instances = []
    FakeDriveSync.order = order
    FakeDriveSync.remote_bytes = None

    monkeypatch.setattr(persister, "DriveSync", FakeDriveSync)
    monkeypatch.setattr(pr, "get_client", lambda: object())
    monkeypatch.setattr(pr, "load_tokens", lambda: [{"access_token": "tok", "item_id": "it1",
                                                     "institution": "Chase", "owner": "u1"}])
    monkeypatch.setattr(pr, "get_account_meta", lambda client, tokens: {})
    # Redirect the audit log off the real data/ dir.
    monkeypatch.setattr(pr, "_RECONCILE_LOG", str(tmp_path / "reconcile_log.jsonl"))

    refetch_calls = []

    def fake_fetch_window(client, tokens, start, end):
        FakeDriveSync.order.append("refetch")
        refetch_calls.append((start, end))
        return fake_fetch_window.fresh

    fake_fetch_window.fresh = []
    monkeypatch.setattr(pr, "fetch_window", fake_fetch_window)

    return tmp_path, order, refetch_calls


def _set_local(monkeypatch, store):
    monkeypatch.setattr(pr, "load_raw_store", lambda: dict(store))


def _read_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return {json.loads(l)["transaction_id"]: json.loads(l) for l in f if l.strip()}


def given_conflict_when_run_persist_then_refetch_merge_and_push(harness, monkeypatch):
    tmp_path, order, refetch_calls = harness
    # Local says amount=1.0; remote says amount=2.0 → a content conflict on "t1".
    _set_local(monkeypatch, {"t1": _rec("t1", amount=1.0), "t2": _rec("t2")})
    remote = {"t1": _rec("t1", amount=2.0)}
    FakeDriveSync.remote_bytes = "\n".join(json.dumps(r) for r in remote.values()).encode()
    # Plaid golden re-fetch returns the corrected t1 (amount=9.0).
    pr.fetch_window.fresh = [_rec("t1", amount=9.0)]

    pr.run_persist(do_drive=True, allow_refetch=True, data_dir=str(tmp_path))

    # Sequence: conflict refetch happened before both Drive pushes.
    assert order == ["refetch", "push:transactions.jsonl", "push:transactions.csv"]
    # The refetch window covers the conflict (a bounded ISO window was passed).
    assert len(refetch_calls) == 1
    # merge_golden let Plaid overwrite the conflicting record in the durable store.
    store = _read_jsonl(tmp_path / "transactions.jsonl")
    assert store["t1"]["amount"] == 9.0
    assert set(store) == {"t1", "t2"}
    # Derived CSV written; reconcile log appended.
    assert (tmp_path / "transactions.csv").exists()
    assert os.path.exists(pr._RECONCILE_LOG)


def given_no_drive_when_run_persist_then_no_pull_no_push_no_refetch(harness, monkeypatch):
    tmp_path, order, refetch_calls = harness
    _set_local(monkeypatch, {"t1": _rec("t1"), "t2": _rec("t2")})

    pr.run_persist(do_drive=False, allow_refetch=True, data_dir=str(tmp_path))

    # Exactly one DriveSync was constructed (the canonical one), never pulled or pushed.
    assert len(FakeDriveSync.instances) == 1
    assert FakeDriveSync.instances[0].pulled is False
    assert order == []                # no refetch (no remote ⇒ no conflict), no pushes
    assert refetch_calls == []
    # Local-only data still persisted offline.
    store = _read_jsonl(tmp_path / "transactions.jsonl")
    assert set(store) == {"t1", "t2"}


def given_conflict_but_no_refetch_when_run_persist_then_stops_before_persisting(
        harness, monkeypatch):
    tmp_path, order, refetch_calls = harness
    _set_local(monkeypatch, {"t1": _rec("t1", amount=1.0)})
    remote = {"t1": _rec("t1", amount=2.0)}
    FakeDriveSync.remote_bytes = "\n".join(json.dumps(r) for r in remote.values()).encode()

    # With the repair fetch disabled the conflict cannot be resolved → stop, don't persist.
    with pytest.raises(SystemExit):
        pr.run_persist(do_drive=True, allow_refetch=False, data_dir=str(tmp_path))

    assert refetch_calls == []
    assert "refetch" not in order
    assert not any(o.startswith("push:") for o in order)   # nothing pushed to Drive
    assert not (tmp_path / "transactions.jsonl").exists()  # durable store untouched
    assert os.path.exists(pr._RECONCILE_LOG)               # conflict still audited


def given_refetch_misses_conflict_when_run_persist_then_stops_before_persisting(
        harness, monkeypatch):
    tmp_path, order, refetch_calls = harness
    _set_local(monkeypatch, {"t1": _rec("t1", amount=1.0)})
    remote = {"t1": _rec("t1", amount=2.0)}
    FakeDriveSync.remote_bytes = "\n".join(json.dumps(r) for r in remote.values()).encode()
    # Plaid's golden re-fetch does NOT return t1 (aged out / item error) → unresolved.
    pr.fetch_window.fresh = []

    with pytest.raises(SystemExit) as exc:
        pr.run_persist(do_drive=True, allow_refetch=True, data_dir=str(tmp_path))

    assert "t1" in str(exc.value)                          # names the unresolved id
    assert len(refetch_calls) == 1                         # the repair fetch did run
    assert "refetch" in order
    assert not any(o.startswith("push:") for o in order)   # nothing pushed to Drive
    assert not (tmp_path / "transactions.jsonl").exists()  # durable store untouched


def given_remote_unstamped_when_run_persist_then_no_conflict_and_stamp_survives(
        harness, monkeypatch):
    # The post-migration scenario: the local store carries txn_owner stamps but the
    # Drive remote predates the field. That difference is OUR metadata, not Plaid
    # content — it must NOT register as a store-wide conflict (which would trigger a
    # giant repair fetch), and the stamped local copy must win in the durable store.
    tmp_path, order, refetch_calls = harness
    _set_local(monkeypatch, {"t1": {**_rec("t1"), "txn_owner": "u1"}})
    remote = {"t1": _rec("t1")}  # identical Plaid content, no stamp
    FakeDriveSync.remote_bytes = "\n".join(json.dumps(r) for r in remote.values()).encode()

    pr.run_persist(do_drive=True, allow_refetch=True, data_dir=str(tmp_path))

    assert refetch_calls == []  # no spurious conflict repair
    store = _read_jsonl(tmp_path / "transactions.jsonl")
    assert store["t1"]["txn_owner"] == "u1"  # local (stamped) copy persisted + pushed


def given_settled_pending_when_run_persist_then_deduped(harness, monkeypatch):
    tmp_path, _order, _ = harness
    # A pending row superseded by a posted row that references it → dropped by dedupe.
    _set_local(monkeypatch, {
        "pend1": _rec("pend1", pending=True),
        "post1": _rec("post1", pending=False, pending_txn_id="pend1"),
    })

    pr.run_persist(do_drive=False, allow_refetch=False, data_dir=str(tmp_path))

    store = _read_jsonl(tmp_path / "transactions.jsonl")
    assert set(store) == {"post1"}  # settled pending dropped
