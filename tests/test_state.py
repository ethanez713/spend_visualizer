"""Tests for plaid_client local-state helpers and client construction."""
import datetime

import pytest
from plaid.api import plaid_api

import src.plaid_client as plaid_client
from src.plaid_client import (
    add_token,
    get_client,
    load_cursors,
    load_raw_store,
    load_tokens,
    save_cursor,
    save_raw_store,
)


# --- _save_json: atomic write + owner-only perms (security regression) -------

def given_secret_written_when_save_json_then_mode_is_0600(state):
    add_token("access-secret", "item_1", "Chase")
    mode = state.tokens.stat().st_mode & 0o777
    assert oct(mode) == "0o600"


def given_missing_file_when_load_then_default_returned(state):
    assert load_tokens() == []      # default []
    assert load_cursors() == {}     # default {}


def given_data_when_saved_then_round_trips(state):
    save_cursor("item_1", "cursor-abc")
    save_cursor("item_2", "cursor-def")
    assert load_cursors() == {"item_1": "cursor-abc", "item_2": "cursor-def"}


# --- add_token: append-or-update-by-item_id ----------------------------------

def given_new_items_when_add_token_then_appended(state):
    add_token("tokA", "item_A", "Chase")
    add_token("tokB", "item_B", "Wells Fargo")
    tokens = load_tokens()
    assert [t["item_id"] for t in tokens] == ["item_A", "item_B"]


def given_existing_item_when_add_token_then_updated_in_place(state):
    add_token("old-token", "item_A", "Chase")
    add_token("new-token", "item_A", "Chase (re-linked)")
    tokens = load_tokens()
    assert len(tokens) == 1
    assert tokens[0]["access_token"] == "new-token"
    assert tokens[0]["institution"] == "Chase (re-linked)"


# --- raw store: xz JSONL round-trip ------------------------------------------

def given_store_when_saved_then_loads_identically(state):
    store = {
        "t2": {"transaction_id": "t2", "date": "2026-01-02", "amount": 5.0},
        "t1": {"transaction_id": "t1", "date": "2026-01-01", "amount": 9.0},
    }
    save_raw_store(store)
    loaded = load_raw_store()
    assert loaded == store


def given_no_archive_when_load_raw_store_then_empty(state):
    assert load_raw_store() == {}


def given_mixed_date_types_when_saved_then_sorted_and_iso_serialized(state):
    # Regression: a delta run used to mix archive records (ISO strings) with fresh
    # Plaid records (datetime objects) in one store — sorting must not TypeError,
    # and date/datetime values must land as ISO ('T'-separated) strings.
    store = {
        "t_old": {"transaction_id": "t_old", "date": "2026-01-02", "amount": 5.0},
        "t_new": {"transaction_id": "t_new", "date": datetime.date(2026, 1, 1),
                  "datetime": datetime.datetime(2026, 1, 1, 12, 30), "amount": 9.0},
    }
    save_raw_store(store)
    loaded = load_raw_store()
    assert loaded["t_new"]["date"] == "2026-01-01"
    assert loaded["t_new"]["datetime"] == "2026-01-01T12:30:00"  # isoformat, not str()
    assert loaded["t_old"] == store["t_old"]


# --- get_client: env validation ----------------------------------------------

def given_missing_credentials_when_get_client_then_systemexit(monkeypatch):
    monkeypatch.delenv("PLAID_CLIENT_ID", raising=False)
    monkeypatch.setenv("PLAID_SECRET", "secret")
    with pytest.raises(SystemExit):
        get_client()


def given_invalid_env_when_get_client_then_systemexit(monkeypatch):
    monkeypatch.setenv("PLAID_CLIENT_ID", "id")
    monkeypatch.setenv("PLAID_SECRET", "secret")
    monkeypatch.setenv("PLAID_ENV", "staging")  # not production/sandbox
    with pytest.raises(SystemExit):
        get_client()


def given_valid_env_when_get_client_then_returns_plaid_api(monkeypatch):
    monkeypatch.setenv("PLAID_CLIENT_ID", "id")
    monkeypatch.setenv("PLAID_SECRET", "secret")
    monkeypatch.setenv("PLAID_ENV", "sandbox")
    client = get_client()
    assert isinstance(client, plaid_api.PlaidApi)
