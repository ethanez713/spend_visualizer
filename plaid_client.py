"""Shared Plaid client setup and small local-state helpers."""
import json
import lzma
import os
from pathlib import Path

import plaid
from dotenv import load_dotenv
from plaid.api import plaid_api

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
TOKENS_FILE = BASE_DIR / "tokens.json"
CURSORS_FILE = BASE_DIR / "sync_cursors.json"
CSV_FILE = BASE_DIR / "transactions.csv"
# Single source of truth: the full raw Plaid object per transaction, keyed by
# transaction_id, persisted as xz-compressed JSONL (one JSON object per line).
# The CSV is a derived projection of this. Kept for audit / QC.
RAW_FILE = BASE_DIR / "transactions_raw.jsonl.xz"

_ENV_HOSTS = {
    "production": plaid.Environment.Production,
    "sandbox": plaid.Environment.Sandbox,
}


def get_client() -> plaid_api.PlaidApi:
    """Build a PlaidApi client from .env (client_id + secret in request body)."""
    client_id = os.getenv("PLAID_CLIENT_ID")
    secret = os.getenv("PLAID_SECRET")
    env = (os.getenv("PLAID_ENV") or "production").lower()

    if not client_id or not secret:
        raise SystemExit("Missing PLAID_CLIENT_ID / PLAID_SECRET in .env")
    if env not in _ENV_HOSTS:
        raise SystemExit(f"PLAID_ENV must be one of {list(_ENV_HOSTS)} (got {env!r})")

    configuration = plaid.Configuration(
        host=_ENV_HOSTS[env],
        api_key={"clientId": client_id, "secret": secret},
    )
    return plaid_api.PlaidApi(plaid.ApiClient(configuration))


def _load_json(path: Path, default):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return default


def _save_json(path: Path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    tmp.replace(path)  # atomic — don't lose access tokens on a crash mid-write


# --- tokens.json: list of {access_token, item_id, institution} ---------------

def load_tokens() -> list:
    return _load_json(TOKENS_FILE, [])


def add_token(access_token: str, item_id: str, institution: str):
    tokens = load_tokens()
    for entry in tokens:
        if entry.get("item_id") == item_id:
            entry.update(access_token=access_token, institution=institution)
            break
    else:
        tokens.append(
            {"access_token": access_token, "item_id": item_id, "institution": institution}
        )
    _save_json(TOKENS_FILE, tokens)
    return tokens


# --- sync_cursors.json: {item_id: next_cursor} -------------------------------

def load_cursors() -> dict:
    return _load_json(CURSORS_FILE, {})


def save_cursor(item_id: str, cursor: str):
    cursors = load_cursors()
    cursors[item_id] = cursor
    _save_json(CURSORS_FILE, cursors)


# --- transactions_raw.jsonl.xz: {transaction_id: full raw Plaid object} -------

def _json_default(o):
    # Keep dates/datetimes as ISO strings so the CSV projection is stable across
    # runs (datetime.isoformat() uses 'T'; str() would use a space).
    if hasattr(o, "isoformat"):
        return o.isoformat()
    return str(o)


def load_raw_store() -> dict:
    """Read the xz JSONL archive into {transaction_id: raw_dict}."""
    if not RAW_FILE.exists():
        return {}
    store = {}
    with lzma.open(RAW_FILE, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                obj = json.loads(line)
                store[obj["transaction_id"]] = obj
    return store


def save_raw_store(store: dict):
    """Write {transaction_id: raw_dict} as xz-compressed JSONL (atomic, max compression)."""
    rows = sorted(
        store.values(),
        key=lambda r: (r.get("date") or "", r.get("transaction_id") or ""),
    )
    tmp = RAW_FILE.parent / (RAW_FILE.name + ".tmp")
    with lzma.open(tmp, "wt", encoding="utf-8", preset=9 | lzma.PRESET_EXTREME) as f:
        for r in rows:
            f.write(json.dumps(r, default=_json_default, ensure_ascii=False))
            f.write("\n")
    tmp.replace(RAW_FILE)  # atomic
