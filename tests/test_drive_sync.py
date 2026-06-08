"""Tests for drive_sync.py — the Drive service is STUBBED; no real network.

A fake service object records calls and returns canned results, so we exercise the
update-vs-create branching, file_id persistence, byte download, and error→None
behaviour without any credentials or network.
"""
import json

from persister.drive_sync import DriveSync


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

    def update(self, fileId=None, media_body=None, fields=None):
        self.s.calls.append(("update", fileId))
        return _Exec({"id": fileId, "webViewLink": "http://drive/updated"})


class FakeService:
    """Stand-in for the googleapiclient Drive v3 service."""
    def __init__(self, content=b"", new_id="newfile123", folders=None):
        self.content = content
        self.new_id = new_id
        self.folders = folders if folders is not None else [{"id": "folder1"}]
        self.calls = []

    def files(self):
        return _Files(self)


class BrokenService:
    def files(self):
        raise RuntimeError("network down")


def _make(tmp_path, fake, name="transactions.jsonl"):
    ds = DriveSync(name, var_dir=str(tmp_path))
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
    # file_id persisted to var/drive_state.json under the logical name.
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
