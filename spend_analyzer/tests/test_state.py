"""Hidden-rule persistence (state.py): survives across sessions, fails soft.

These exercise the disk layer (_load/_save) directly — no Streamlit session — so
they stay fast unit tests. The in-session behavior is covered by tests/ui/test_hidden.
"""
import json

import pytest

import state


@pytest.fixture(autouse=True)
def _tmp_store(monkeypatch, tmp_path):
    """Redirect the durable store into tmp so no test touches live hide rules."""
    monkeypatch.setattr(state, "DATA_DIR", tmp_path)
    monkeypatch.setattr(state, "STORE", tmp_path / "hidden_rules.json")


def test_given_saved_rules_when_reloaded_then_round_trips():
    rules = [{"dim": "tier1", "value": "Mortgage", "label": "Mortgage"}]
    state._save(rules)
    assert state._load() == rules


def test_given_no_file_when_load_then_empty():
    assert state._load() == []


def test_given_malformed_json_when_load_then_empty_not_crash(tmp_path):
    state.STORE.write_text("{ not json", encoding="utf-8")
    assert state._load() == []


def test_given_non_list_json_when_load_then_empty():
    state.STORE.write_text(json.dumps({"dim": "x"}), encoding="utf-8")
    assert state._load() == []


def test_given_partly_malformed_rules_when_load_then_only_wellformed_kept():
    state.STORE.write_text(json.dumps([
        {"dim": "tier1", "value": "House", "label": "House"},
        {"dim": "tier1"},                 # missing "value" → dropped
        "not a dict",                     # → dropped
    ]), encoding="utf-8")
    assert state._load() == [{"dim": "tier1", "value": "House", "label": "House"}]


def test_given_saved_file_when_written_then_owner_only_perms():
    import stat
    state._save([{"dim": "tier1", "value": "X", "label": "X"}])
    assert stat.S_IMODE(state.STORE.stat().st_mode) == 0o600
