"""Tests for the Drive divergence gate (``check_drive_divergence``).

The gate must stop a run (before any audit/write) when the Drive copy of the
categorized store holds anything the local prior store doesn't — content conflicts
or remote-only rows — because pushing would clobber remote audit history that has
no golden source to repair from. DriveSync is stubbed; everything stays offline.
"""
import json

import pytest

import persister
import src.transformer as tr


class FakeDriveSync:
    """Returns canned remote JSONL bytes from pull(); records construction args."""
    remote_bytes = None
    instances = None

    def __init__(self, file_name, folder_name="transactions_archive", secrets_dir=None):
        self.file_name = file_name
        self.folder_name = folder_name
        self.secrets_dir = secrets_dir
        FakeDriveSync.instances.append(self)

    def pull(self):
        return FakeDriveSync.remote_bytes


@pytest.fixture(autouse=True)
def stub_drive(monkeypatch):
    FakeDriveSync.instances = []
    FakeDriveSync.remote_bytes = None
    monkeypatch.setattr(persister, "DriveSync", FakeDriveSync)


def _jsonl(store: dict) -> bytes:
    return "\n".join(json.dumps(r) for r in store.values()).encode()


def given_remote_matches_prior_when_gate_then_passes(store_of):
    prior = store_of({"transaction_id": "t1"}, {"transaction_id": "t2"})
    FakeDriveSync.remote_bytes = _jsonl(prior)

    tr.check_drive_divergence(prior)  # no exit

    assert FakeDriveSync.instances[0].file_name == tr.DRIVE_JSONL_NAME


def given_no_remote_when_gate_then_passes(store_of):
    # Nothing pushed yet (or Drive unreachable): pull() → None → first push is fine.
    prior = store_of({"transaction_id": "t1"})
    FakeDriveSync.remote_bytes = None

    tr.check_drive_divergence(prior)  # no exit


def given_local_ahead_with_new_rows_when_gate_then_passes(store_of):
    # local_only rows (e.g. audited during --no-drive runs) only ADD on push — safe.
    prior = store_of({"transaction_id": "t1"}, {"transaction_id": "t2"})
    remote = {"t1": prior["t1"]}
    FakeDriveSync.remote_bytes = _jsonl(remote)

    tr.check_drive_divergence(prior)  # no exit


def given_remote_unstamped_when_gate_then_passes(store_of):
    # Post-migration: local rows carry the collector's txn_owner stamp; the Drive copy
    # predates the field. That is OUR metadata, not audit drift — the gate must pass
    # (no --force-push needed after the multi-user migration).
    prior = store_of({"transaction_id": "t1", "txn_owner": "Alice"})
    remote = {"t1": {k: v for k, v in prior["t1"].items() if k != "txn_owner"}}
    FakeDriveSync.remote_bytes = _jsonl(remote)

    tr.check_drive_divergence(prior)  # no exit


def given_content_conflict_when_gate_then_stops(store_of):
    prior = store_of({"transaction_id": "t1", "amount": 1.0})
    remote = {"t1": dict(prior["t1"], amount=2.0)}
    FakeDriveSync.remote_bytes = _jsonl(remote)

    with pytest.raises(SystemExit) as exc:
        tr.check_drive_divergence(prior)
    assert "force-push" in str(exc.value)


def given_remote_only_rows_when_gate_then_stops(store_of):
    # The disaster case: local store lost/reset while Drive still holds history.
    prior = {}
    remote = store_of({"transaction_id": "t1"})
    FakeDriveSync.remote_bytes = _jsonl(remote)

    with pytest.raises(SystemExit):
        tr.check_drive_divergence(prior)


def given_divergence_with_force_push_when_gate_then_passes(store_of, capsys):
    prior = store_of({"transaction_id": "t1", "amount": 1.0})
    remote = {"t1": dict(prior["t1"], amount=2.0)}
    FakeDriveSync.remote_bytes = _jsonl(remote)

    tr.check_drive_divergence(prior, force_push=True)  # no exit

    assert "authoritative" in capsys.readouterr().err


# ── review_run keeps local and Drive in lock-step ─────────────────────────────

class FakePushDriveSync(FakeDriveSync):
    """FakeDriveSync that also records push() calls across instances."""
    pushes = None

    def push(self, local_path, mime="application/x-ndjson"):
        FakePushDriveSync.pushes.append(self.file_name)
        return f"https://drive.example/{self.file_name}"


@pytest.fixture
def review_world(tmp_path, monkeypatch, store_of):
    """A categorized store on disk + stubbed Drive + stubbed interactive review."""
    FakePushDriveSync.pushes = []
    monkeypatch.setattr(persister, "DriveSync", FakePushDriveSync)

    store = store_of({"transaction_id": "t1", "category_review_flag": "1"})
    out_jsonl = tmp_path / "categorized.jsonl"
    persister.save_jsonl(str(out_jsonl), store)

    # The interactive session is stubbed: it "accepts" one row (mutating nothing we
    # assert on) so review_run takes its re-persist path.
    import src.review as review_mod
    monkeypatch.setattr(review_mod, "run_review",
                        lambda store, memory: {"accepted": 1, "rejected": 0,
                                               "repicked": 0, "skipped": 0, "log": []})
    monkeypatch.setattr(review_mod, "write_review_log", lambda path, entries: None)

    paths = dict(out_jsonl=str(out_jsonl), out_csv=str(tmp_path / "c.csv"),
                 flags_csv=str(tmp_path / "f.csv"), memory_path=None)
    return paths


def given_accepted_review_when_drive_on_then_outputs_pushed(review_world):
    tr.review_run(**review_world, do_drive=True)

    assert FakePushDriveSync.pushes == [tr.DRIVE_JSONL_NAME,
                                        "transactions_categorized.csv",
                                        "flagged_for_review.csv"]


def given_accepted_review_when_no_drive_then_nothing_pushed(review_world):
    tr.review_run(**review_world, do_drive=False)

    assert FakePushDriveSync.pushes == []


def given_diverged_remote_when_review_then_gate_stops_before_session(review_world):
    # Remote holds a row the local store doesn't → the gate must stop the review
    # before any human effort is spent (and before anything could be pushed).
    # (pull() reads the base-class attribute, so set it there.)
    FakeDriveSync.remote_bytes = _jsonl({"ghost": {"transaction_id": "ghost"}})

    with pytest.raises(SystemExit):
        tr.review_run(**review_world, do_drive=True)

    assert FakePushDriveSync.pushes == []
