"""Tests for Drive head adoption (``adopt_drive_head``).

The categorized store has two legitimate writers (scheduled server run + desktop
LLM runs) serialized through the Drive copy: every Drive-enabled run starts by
adopting the remote head — remote-only rows taken, conflicts take the remote
value, local-only rows kept — and the manual-edits intent log is union-merged the
same way (without that, replay would auto-revert the other machine's
corrections). A pull failure still stops the run; ``--force-push`` skips adoption
and declares the local store authoritative. DriveSync is stubbed; everything
stays offline.
"""
import json

import pytest

import persister
import src.transformer as tr
from src.manual import ManualIndex, apply_manual_edits, load_intents, resolve_intents


class FakeDriveSync:
    """Returns canned per-file remote bytes from pull(); records construction args."""
    remote_by_name = None
    instances = None

    def __init__(self, file_name, folder_name="transactions_archive", secrets_dir=None):
        self.file_name = file_name
        self.folder_name = folder_name
        self.secrets_dir = secrets_dir
        FakeDriveSync.instances.append(self)

    def pull(self):
        return FakeDriveSync.remote_by_name.get(self.file_name)


@pytest.fixture(autouse=True)
def stub_drive(monkeypatch):
    FakeDriveSync.instances = []
    FakeDriveSync.remote_by_name = {}
    monkeypatch.setattr(persister, "DriveSync", FakeDriveSync)


def _jsonl(store: dict) -> bytes:
    return "\n".join(json.dumps(r) for r in store.values()).encode()


def _set_remote_store(store: dict) -> None:
    FakeDriveSync.remote_by_name[tr.DRIVE_JSONL_NAME] = _jsonl(store)


# ── Store adoption ────────────────────────────────────────────────────────────

def given_remote_matches_prior_when_adopt_then_prior_unchanged(store_of):
    prior = store_of({"transaction_id": "t1"}, {"transaction_id": "t2"})
    _set_remote_store(prior)

    assert tr.adopt_drive_head(prior) == prior

    assert FakeDriveSync.instances[0].file_name == tr.DRIVE_JSONL_NAME


def given_no_remote_when_adopt_then_prior_unchanged(store_of):
    # Nothing pushed yet: pull() → None → first push is fine.
    prior = store_of({"transaction_id": "t1"})

    assert tr.adopt_drive_head(prior) == prior


def given_pull_failure_when_adopt_then_stops(store_of, monkeypatch):
    # An unreadable remote cannot be adopted — pushing blind at the end of the run
    # could clobber the other writer. Stop, don't guess.
    prior = store_of({"transaction_id": "t1"})
    monkeypatch.setattr(
        FakeDriveSync, "pull",
        lambda self: (_ for _ in ()).throw(persister.DrivePullError("remote unreadable")))

    with pytest.raises(SystemExit) as exc:
        tr.adopt_drive_head(prior)
    assert "STOP" in str(exc.value)


def given_pull_failure_with_force_push_when_adopt_then_local_authoritative(
        store_of, monkeypatch, capsys):
    # --force-push skips adoption entirely (no pull is even attempted) — the human
    # has declared the local store authoritative, loudly.
    prior = store_of({"transaction_id": "t1"})
    monkeypatch.setattr(
        FakeDriveSync, "pull",
        lambda self: (_ for _ in ()).throw(persister.DrivePullError("remote unreadable")))

    assert tr.adopt_drive_head(prior, force_push=True) == prior

    assert "authoritative" in capsys.readouterr().err


def given_local_ahead_with_new_rows_when_adopt_then_prior_unchanged(store_of):
    # local_only rows (e.g. audited during --no-drive runs) only ADD on push — kept.
    prior = store_of({"transaction_id": "t1"}, {"transaction_id": "t2"})
    _set_remote_store({"t1": prior["t1"]})

    assert tr.adopt_drive_head(prior) == prior


def given_remote_unstamped_when_adopt_then_no_conflict_read(store_of):
    # Local rows carry the collector's txn_owner stamp; the Drive copy predates the
    # field. That is OUR metadata, not audit drift — the local copy stands.
    prior = store_of({"transaction_id": "t1", "txn_owner": "Alice"})
    _set_remote_store({"t1": {k: v for k, v in prior["t1"].items() if k != "txn_owner"}})

    assert tr.adopt_drive_head(prior) == prior


def given_content_conflict_with_no_stamps_when_adopt_then_remote_value_taken(store_of):
    # Pre-stamp rows (no category_audited_at on either side) tie → remote, the
    # original behavior and the safe default for legacy data.
    prior = store_of({"transaction_id": "t1", "amount": 1.0})
    remote = {"t1": dict(prior["t1"], amount=2.0)}
    _set_remote_store(remote)

    adopted = tr.adopt_drive_head(prior)

    assert adopted["t1"]["amount"] == 2.0


# ── Conflict resolution by audit recency (local-ahead must never lose work) ───

def given_local_audit_newer_when_adopt_then_local_work_kept(store_of):
    # THE no-data-loss requirement: a local store ahead of the Drive head (offline
    # review session, crash between save and push, lost push race) keeps its work.
    prior = store_of({"transaction_id": "t1", "amount": 1.0,
                      "category_audited_at": "2026-06-02T10:00:00.000000+00:00"})
    remote = {"t1": dict(prior["t1"], amount=2.0,
                         category_audited_at="2026-06-01T10:00:00.000000+00:00")}
    _set_remote_store(remote)

    adopted = tr.adopt_drive_head(prior)

    assert adopted["t1"]["amount"] == 1.0        # local (newer audit) survives


def given_remote_audit_newer_when_adopt_then_remote_work_taken(store_of):
    prior = store_of({"transaction_id": "t1", "amount": 1.0,
                      "category_audited_at": "2026-06-01T10:00:00.000000+00:00"})
    remote = {"t1": dict(prior["t1"], amount=2.0,
                         category_audited_at="2026-06-02T10:00:00.000000+00:00")}
    _set_remote_store(remote)

    adopted = tr.adopt_drive_head(prior)

    assert adopted["t1"]["amount"] == 2.0


def given_only_local_stamped_when_adopt_then_local_wins(store_of):
    # A stamped row always beats an unstamped one — real work beats legacy data.
    prior = store_of({"transaction_id": "t1", "amount": 1.0,
                      "category_audited_at": "2026-06-01T10:00:00.000000+00:00"})
    remote = {"t1": {k: v for k, v in dict(prior["t1"], amount=2.0).items()
                     if k != "category_audited_at"}}
    _set_remote_store(remote)

    adopted = tr.adopt_drive_head(prior)

    assert adopted["t1"]["amount"] == 1.0


def given_rows_equal_except_stamps_when_adopt_then_in_sync_local_kept(
        store_of, tmp_path):
    # The recency stamp is metadata, not audit content: two machines that audited a
    # row to the SAME result at different times are in sync — no conflict, no log.
    log = tmp_path / "adopt_conflicts.jsonl"
    prior = store_of({"transaction_id": "t1", "amount": 1.0,
                      "category_audited_at": "2026-06-02T10:00:00.000000+00:00"})
    remote = {"t1": dict(prior["t1"],
                         category_audited_at="2026-06-01T10:00:00.000000+00:00")}
    _set_remote_store(remote)

    adopted = tr.adopt_drive_head(prior, conflict_log=str(log))

    assert adopted == prior
    assert not log.exists()


def given_conflicts_when_adopt_then_both_versions_logged(store_of, tmp_path):
    # No version is ever silently discarded: the audit log captures BOTH sides and
    # which one was kept, and lives in the data root so the daily git push keeps it.
    log = tmp_path / "adopt_conflicts.jsonl"
    prior = store_of({"transaction_id": "t1", "amount": 1.0,
                      "category_audited_at": "2026-06-02T10:00:00.000000+00:00"})
    remote = {"t1": dict(prior["t1"], amount=2.0,
                         category_audited_at="2026-06-01T10:00:00.000000+00:00")}
    _set_remote_store(remote)

    tr.adopt_drive_head(prior, conflict_log=str(log))

    entry = json.loads(log.read_text().splitlines()[0])
    assert entry["transaction_id"] == "t1"
    assert entry["kept"] == "local"
    assert entry["local"]["amount"] == 1.0       # full records, both sides
    assert entry["remote"]["amount"] == 2.0


def given_remote_only_rows_when_adopt_then_rows_taken(store_of):
    # Rows the other machine audited — and the lost/reset-local-store disaster case,
    # which is now self-recovering: adoption restores the store from Drive.
    prior = {}
    remote = store_of({"transaction_id": "t1"})
    _set_remote_store(remote)

    adopted = tr.adopt_drive_head(prior)

    assert adopted == remote


def given_mixed_divergence_when_adopt_then_union_preserved(store_of):
    prior = store_of({"transaction_id": "local_only"},
                     {"transaction_id": "both", "amount": 1.0})
    remote = {"both": dict(prior["both"], amount=2.0),
              "remote_only": {"transaction_id": "remote_only"}}
    _set_remote_store(remote)

    adopted = tr.adopt_drive_head(prior)

    assert set(adopted) == {"local_only", "both", "remote_only"}
    assert adopted["both"]["amount"] == 2.0          # conflict → remote
    assert adopted["local_only"] is prior["local_only"]


def given_divergence_with_force_push_when_adopt_then_prior_kept(store_of, capsys):
    prior = store_of({"transaction_id": "t1", "amount": 1.0})
    _set_remote_store({"t1": dict(prior["t1"], amount=2.0)})

    assert tr.adopt_drive_head(prior, force_push=True) == prior

    assert "authoritative" in capsys.readouterr().err


# ── Intent-log adoption (the anti-revert hole) ────────────────────────────────

def _intent(iid: str, tid: str = "t1") -> dict:
    """A minimal valid transaction-scope intent (the shape build_intent emits)."""
    return {"id": iid, "action": "edit", "scope": "transaction",
            "match": {"transaction_id": tid},
            "set": {"primary": "FOOD_AND_DRINK", "detailed": "FOOD_AND_DRINK_COFFEE"}}


def _set_remote_edits(entries: list[dict]) -> None:
    FakeDriveSync.remote_by_name["manual_edits.jsonl"] = (
        "\n".join(json.dumps(e) for e in entries).encode())


def given_remote_intents_when_adopt_then_local_log_becomes_union(tmp_path, store_of):
    edits = tmp_path / "manual_edits.jsonl"
    edits.write_text(json.dumps(_intent("local-1", "t9")) + "\n")
    _set_remote_edits([_intent("remote-1"), _intent("remote-2", "t2")])
    _set_remote_store(store_of({"transaction_id": "t1"}))

    tr.adopt_drive_head(store_of({"transaction_id": "t1"}), edits_path=str(edits))

    ids = [e["id"] for e in load_intents(str(edits))]
    assert ids == ["remote-1", "remote-2", "local-1"]   # remote trunk, local rebased


def given_overlapping_logs_when_adopt_then_deduped_by_id(tmp_path, store_of):
    edits = tmp_path / "manual_edits.jsonl"
    edits.write_text("\n".join(json.dumps(e) for e in
                               [_intent("shared"), _intent("local-1", "t9")]) + "\n")
    _set_remote_edits([_intent("shared")])
    _set_remote_store(store_of({"transaction_id": "t1"}))

    tr.adopt_drive_head(store_of({"transaction_id": "t1"}), edits_path=str(edits))

    ids = [e["id"] for e in load_intents(str(edits))]
    assert ids == ["shared", "local-1"]


def given_no_remote_log_when_adopt_then_local_log_untouched(tmp_path, store_of):
    edits = tmp_path / "manual_edits.jsonl"
    original = json.dumps(_intent("local-1")) + "\n"
    edits.write_text(original)
    _set_remote_store(store_of({"transaction_id": "t1"}))

    tr.adopt_drive_head(store_of({"transaction_id": "t1"}), edits_path=str(edits))

    assert edits.read_text() == original


def given_edits_pull_failure_when_adopt_then_stops(tmp_path, store_of, monkeypatch):
    # A stale local log must never be replayed blind: replay REVERTS corrected rows
    # with no covering local intent, undoing the other machine's corrections.
    edits = tmp_path / "manual_edits.jsonl"
    edits.write_text("")
    _set_remote_store(store_of({"transaction_id": "t1"}))

    def pull(self):
        if self.file_name == "manual_edits.jsonl":
            raise persister.DrivePullError("intent log unreadable")
        return FakeDriveSync.remote_by_name.get(self.file_name)
    monkeypatch.setattr(FakeDriveSync, "pull", pull)

    with pytest.raises(SystemExit) as exc:
        tr.adopt_drive_head(store_of({"transaction_id": "t1"}), edits_path=str(edits))
    assert "intent log" in str(exc.value)


def given_correction_made_on_other_machine_when_adopt_and_replay_then_not_reverted(
        tmp_path, store_of, make_record):
    # THE regression this design exists for: machine A corrected a row (intent in A's
    # log, manual provenance in the pushed store); machine B has neither. B must adopt
    # both the row and the intent — replaying B's stale log alone would revert A's
    # correction and push the regression.
    corrected = make_record()
    corrected.update({"transaction_id": "t1", "category_update_step": "manual"})
    _set_remote_store({"t1": corrected})
    _set_remote_edits([_intent("a-1", "t1")])
    edits = tmp_path / "manual_edits.jsonl"      # machine B's log: empty (stale)
    edits.write_text("")

    adopted = tr.adopt_drive_head({}, edits_path=str(edits))
    index = ManualIndex(resolve_intents(load_intents(str(edits))))
    summary = apply_manual_edits(adopted, index)

    assert summary["reverted"] == []             # A's correction survives B's replay
    assert adopted["t1"]["category_update_step"] == "manual"


# ── review_run adopts, then keeps local and Drive in lock-step ────────────────

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


def given_remote_ahead_when_review_then_head_adopted_and_persisted(review_world):
    # The other machine pushed a row this machine hasn't seen: the review session
    # adopts it (works the freshest store) and the re-persist keeps it.
    _set_remote_store({"ghost": {"transaction_id": "ghost"}})

    tr.review_run(**review_world, do_drive=True)

    saved = persister.load_jsonl(review_world["out_jsonl"])
    assert "ghost" in saved
    assert tr.DRIVE_JSONL_NAME in FakePushDriveSync.pushes
