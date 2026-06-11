"""Tests for drive_sync.py — the Drive service is STUBBED; no real network.

A fake service object records calls and returns canned results, so we exercise the
update-vs-create branching, file_id persistence, byte download, and error→None
behaviour without any credentials or network.
"""
import json

import pytest

from persister.drive_sync import AppendOnlyError, DriveSync, _GuardedService


class _Exec:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result


class _Files:
    def __init__(self, service):
        self.s = service

    def get_media(self, fileId=None):
        self.s.calls.append(("get_media", fileId))
        return _Exec(self.s.content)

    def list(self, q=None, fields=None, pageSize=None):
        self.s.calls.append(("list", q))
        return _Exec({"files": self.s.folders})

    def create(self, body=None, media_body=None, fields=None):
        self.s.calls.append(("create", body))
        # Folder creates ask only for id; file creates ask for id+webViewLink.
        return _Exec({"id": self.s.new_id, "webViewLink": "http://drive/created"})

    def update(self, fileId=None, media_body=None, body=None, fields=None):
        self.s.calls.append(("update", fileId))
        return _Exec({"id": fileId, "webViewLink": "http://drive/updated"})

    def delete(self, fileId=None):
        # Present so the guard has a real method to block; should never be reached.
        self.s.calls.append(("delete", fileId))
        return _Exec({})


class _Revisions:
    def __init__(self, service):
        self.s = service

    def list(self, fileId=None, fields=None, pageSize=None, pageToken=None):
        self.s.calls.append(("rev_list", fileId))
        return _Exec({"revisions": self.s.revisions_meta})

    def get_media(self, fileId=None, revisionId=None):
        self.s.calls.append(("rev_get_media", revisionId))
        return _Exec(self.s.revision_content.get(revisionId, b""))

    def delete(self, fileId=None, revisionId=None):
        self.s.calls.append(("rev_delete", revisionId))
        return _Exec({})


class FakeService:
    """Stand-in for the googleapiclient Drive v3 service."""
    def __init__(self, content=b"", new_id="newfile123", folders=None,
                 revisions_meta=None, revision_content=None):
        self.content = content
        self.new_id = new_id
        self.folders = folders if folders is not None else [{"id": "folder1"}]
        self.revisions_meta = revisions_meta if revisions_meta is not None else []
        self.revision_content = revision_content or {}
        self.calls = []

    def files(self):
        return _Files(self)

    def revisions(self):
        return _Revisions(self)


class BrokenService:
    def files(self):
        raise RuntimeError("network down")


def _make(tmp_path, fake, name="transactions.jsonl"):
    ds = DriveSync(name, secrets_dir=str(tmp_path))
    ds._service = fake  # inject the stub
    return ds


def _local_file(tmp_path):
    p = tmp_path / "store.jsonl"
    p.write_text('{"transaction_id":"a"}\n')
    return str(p)


def given_no_file_id_when_push_then_creates_and_remembers_id(tmp_path):
    fake = FakeService()
    ds = _make(tmp_path, fake)
    link = ds.push(_local_file(tmp_path))

    assert link == "http://drive/created"
    # file_id persisted to .secrets/drive_state.json under the logical name.
    state = json.loads((tmp_path / "drive_state.json").read_text())
    assert state == {"transactions.jsonl": "newfile123"}
    methods = [c[0] for c in fake.calls]
    assert "create" in methods and "update" not in methods


def given_known_file_id_when_push_then_updates_in_place(tmp_path):
    (tmp_path / "drive_state.json").write_text(json.dumps({"transactions.jsonl": "existing"}))
    fake = FakeService()
    ds = _make(tmp_path, fake)
    link = ds.push(_local_file(tmp_path))

    assert link == "http://drive/updated"
    assert ("update", "existing") in fake.calls
    assert "create" not in [c[0] for c in fake.calls]  # no duplicate file created


def given_remembered_id_when_pull_then_returns_bytes(tmp_path):
    (tmp_path / "drive_state.json").write_text(json.dumps({"transactions.jsonl": "fid"}))
    fake = FakeService(content=b'{"transaction_id":"a"}\n')
    ds = _make(tmp_path, fake)
    assert ds.pull() == b'{"transaction_id":"a"}\n'
    assert ("get_media", "fid") in fake.calls


def given_str_content_when_pull_then_encoded_to_bytes(tmp_path):
    (tmp_path / "drive_state.json").write_text(json.dumps({"transactions.jsonl": "fid"}))
    ds = _make(tmp_path, FakeService(content='{"transaction_id":"a"}\n'))
    assert ds.pull() == b'{"transaction_id":"a"}\n'


def given_no_file_id_when_pull_then_none_without_calling_service(tmp_path):
    fake = FakeService()
    ds = _make(tmp_path, fake)
    assert ds.pull() is None
    assert fake.calls == []  # short-circuits before touching the service


def given_service_error_when_pull_then_none_not_raise(tmp_path):
    (tmp_path / "drive_state.json").write_text(json.dumps({"transactions.jsonl": "fid"}))
    ds = _make(tmp_path, BrokenService())
    assert ds.pull() is None


def given_service_error_when_push_then_none_not_raise(tmp_path):
    ds = _make(tmp_path, BrokenService())
    assert ds.push(_local_file(tmp_path)) is None


# --- revision audit / rollback ----------------------------------------------------

def given_pushed_file_when_list_revisions_then_returns_metadata(tmp_path):
    (tmp_path / "drive_state.json").write_text(json.dumps({"transactions.jsonl": "fid"}))
    revs = [{"id": "r1", "modifiedTime": "2026-01-01T00:00:00Z", "size": "100"},
            {"id": "r2", "modifiedTime": "2026-01-02T00:00:00Z", "size": "200"}]
    ds = _make(tmp_path, FakeService(revisions_meta=revs))
    assert ds.list_revisions() == revs


def given_no_file_id_when_list_revisions_then_empty(tmp_path):
    fake = FakeService(revisions_meta=[{"id": "r1"}])
    ds = _make(tmp_path, fake)
    assert ds.list_revisions() == []
    assert fake.calls == []  # short-circuits before touching the service


def given_revision_id_when_pull_revision_then_returns_bytes(tmp_path):
    (tmp_path / "drive_state.json").write_text(json.dumps({"transactions.jsonl": "fid"}))
    fake = FakeService(revision_content={"r1": b'{"transaction_id":"old"}\n'})
    ds = _make(tmp_path, fake)
    assert ds.pull_revision("r1") == b'{"transaction_id":"old"}\n'
    assert ("rev_get_media", "r1") in fake.calls


def given_no_file_id_when_pull_revision_then_none(tmp_path):
    ds = _make(tmp_path, FakeService())
    assert ds.pull_revision("r1") is None


def given_service_error_when_list_or_pull_revision_then_safe(tmp_path):
    (tmp_path / "drive_state.json").write_text(json.dumps({"transactions.jsonl": "fid"}))
    ds = _make(tmp_path, BrokenService())
    assert ds.list_revisions() == []       # never raises
    assert ds.pull_revision("r1") is None   # never raises


def given_old_revision_when_restore_then_repushes_as_new_revision(tmp_path):
    # Rollback = pull the old revision's bytes, then push them as a NEW head revision
    # (file_id known → update path). History is preserved; nothing deleted.
    (tmp_path / "drive_state.json").write_text(json.dumps({"transactions.jsonl": "fid"}))
    fake = FakeService(revision_content={"r1": b'{"transaction_id":"old"}\n'})
    ds = _make(tmp_path, fake)
    link = ds.restore_revision("r1")
    assert link == "http://drive/updated"
    methods = [c[0] for c in fake.calls]
    assert "rev_get_media" in methods and "update" in methods
    assert "delete" not in methods and "rev_delete" not in methods


# --- append-only guard: the library can NEVER delete or trash --------------------

def given_guarded_service_when_files_delete_then_raises():
    guarded = _GuardedService(FakeService())
    with pytest.raises(AppendOnlyError):
        guarded.files().delete  # accessing the destructive method is blocked


def given_guarded_service_when_revisions_delete_then_raises():
    guarded = _GuardedService(FakeService())
    with pytest.raises(AppendOnlyError):
        guarded.revisions().delete


def given_guarded_service_when_update_sets_trashed_then_raises():
    guarded = _GuardedService(FakeService())
    with pytest.raises(AppendOnlyError):
        guarded.files().update(fileId="x", body={"trashed": True})


def given_guarded_service_when_normal_ops_then_pass_through():
    fake = FakeService(content=b"hi")
    guarded = _GuardedService(fake)
    # get_media / list / update-without-trash all work unchanged.
    assert guarded.files().get_media(fileId="x").execute() == b"hi"
    assert guarded.files().update(fileId="x", body={"name": "ok"}).execute()["id"] == "x"
    assert ("update", "x") in fake.calls


def given_drivesync_when_inspected_then_exposes_no_delete_api():
    # Structural guarantee: no destructive method on the public class.
    for name in ("delete", "delete_file", "trash", "remove", "destroy"):
        assert not hasattr(DriveSync, name)
