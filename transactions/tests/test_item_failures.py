"""Tests for the end-of-run failed-Item report (main()'s exit contract).

A per-Item Plaid failure used to be one stderr line inside an otherwise-green run —
an expired bank consent looked exactly like "no new spending", and the pipeline
categorized and pushed the incomplete picture. main() now collects the Items that
contributed nothing and exits non-zero AFTER the healthy data is persisted.

Fully offline: the Plaid client, token list, and account metadata are faked, and the
`state` fixture keeps every path in tmp.
"""
import json

import pytest
from plaid.exceptions import ApiException

import src.fetch_transactions as ft
from src.fetch_transactions import api_error_code, report_failed_items


class FakeTxn(dict):
    def to_dict(self):
        return dict(self)


class PerTokenClient:
    """transactions_sync keyed by access_token; a token may map to an Exception."""

    def __init__(self, per_token):
        self.per_token = per_token

    def transactions_sync(self, request):
        data = self.per_token[request.access_token]
        if isinstance(data, Exception):
            raise data
        return data


def _page(added=(), next_cursor="c1"):
    return {"added": list(added), "modified": [], "removed": [],
            "has_more": False, "next_cursor": next_cursor}


def _token(access_token, institution, item_id, owner="u1"):
    return {"access_token": access_token, "item_id": item_id,
            "institution": institution, "owner": owner}


def _api_error(code, status=400):
    e = ApiException(status=status, reason=code)
    e.body = json.dumps({"error_code": code, "error_type": "ITEM_ERROR"})
    return e


def _drive_main(monkeypatch, client, tokens, argv):
    """Run main() with Plaid, account metadata, and the overfetch stubbed out."""
    monkeypatch.setattr(ft, "get_client", lambda: client)
    monkeypatch.setattr(ft, "load_tokens", lambda: tokens)
    monkeypatch.setattr(ft, "get_account_meta", lambda c, t: {})
    monkeypatch.setattr(ft, "overfetch_due", lambda force: False)
    return ft.main(argv)


# --- the exit contract ---------------------------------------------------------------

def given_one_item_fails_when_fetch_then_exits_nonzero(state, monkeypatch):
    client = PerTokenClient({
        "ok": _page([FakeTxn(transaction_id="a1", date="2026-01-02")]),
        "boom": _api_error("ITEM_LOGIN_REQUIRED"),
    })
    tokens = [_token("ok", "Chase", "it1"), _token("boom", "Amex", "it2")]

    with pytest.raises(SystemExit) as exc:
        _drive_main(monkeypatch, client, tokens, ["--no-persist"])

    assert exc.value.code == 1


def given_one_item_fails_when_fetch_then_healthy_data_still_saved(state, monkeypatch):
    """The non-zero exit must stop the PIPELINE, not discard this run's work."""
    client = PerTokenClient({
        "ok": _page([FakeTxn(transaction_id="a1", date="2026-01-02")]),
        "boom": _api_error("ITEM_LOGIN_REQUIRED"),
    })
    tokens = [_token("ok", "Chase", "it1"), _token("boom", "Amex", "it2")]

    with pytest.raises(SystemExit):
        _drive_main(monkeypatch, client, tokens, ["--no-persist"])

    from src.plaid_client import load_raw_store
    assert "a1" in load_raw_store()      # healthy item's rows are durable
    assert state.csv.exists()            # and the CSV was projected


def given_all_items_healthy_when_fetch_then_exits_cleanly(state, monkeypatch):
    client = PerTokenClient({"ok": _page([FakeTxn(transaction_id="a1", date="2026-01-02")])})

    _drive_main(monkeypatch, client, [_token("ok", "Chase", "it1")], ["--no-persist"])

    from src.plaid_client import load_raw_store
    assert "a1" in load_raw_store()


def given_allow_item_errors_when_item_fails_then_exits_cleanly(state, monkeypatch):
    client = PerTokenClient({
        "ok": _page([FakeTxn(transaction_id="a1", date="2026-01-02")]),
        "boom": _api_error("ITEM_LOGIN_REQUIRED"),
    })
    tokens = [_token("ok", "Chase", "it1"), _token("boom", "Amex", "it2")]

    _drive_main(monkeypatch, client, tokens, ["--no-persist", "--allow-item-errors"])

    from src.plaid_client import load_raw_store
    assert "a1" in load_raw_store()


def given_history_never_ready_when_fetch_then_counted_as_failure(state, monkeypatch):
    """Retry exhaustion is the same signal: the Item produced no data."""
    monkeypatch.setattr(ft, "FIRST_RUN_RETRIES", 1)
    monkeypatch.setattr(ft, "FIRST_RUN_DELAY_S", 0)
    client = PerTokenClient({"new": _page(next_cursor="")})  # empty + no cursor

    with pytest.raises(SystemExit) as exc:
        _drive_main(monkeypatch, client, [_token("new", "Chase", "it1")], ["--no-persist"])

    assert exc.value.code == 1


# --- report / error-code helpers -----------------------------------------------------

def given_failures_when_reported_then_message_names_the_banks(state, capsys):
    with pytest.raises(SystemExit):
        report_failed_items(["Amex (ITEM_LOGIN_REQUIRED)"], 2, allow=False)

    err = capsys.readouterr().err
    assert "Amex (ITEM_LOGIN_REQUIRED)" in err
    assert "1 of 2" in err
    assert "app.py --user" in err          # actionable next step


def given_no_failures_when_reported_then_silent_and_no_exit(state, capsys):
    report_failed_items([], 3, allow=False)
    assert capsys.readouterr().err == ""


def given_plaid_body_when_error_code_read_then_code_extracted(state):
    assert api_error_code(_api_error("ITEM_LOGIN_REQUIRED")) == "ITEM_LOGIN_REQUIRED"


def given_unparseable_body_when_error_code_read_then_falls_back_to_status(state):
    e = ApiException(status=500, reason="boom")
    e.body = "<html>gateway error</html>"
    assert api_error_code(e) == "HTTP 500"
