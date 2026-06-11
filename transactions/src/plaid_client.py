"""Shared Plaid client setup and small local-state helpers."""
import json
import lzma
import os
from pathlib import Path

import plaid
from dotenv import load_dotenv
from plaid.api import plaid_api

BASE_DIR = Path(__file__).resolve().parent   # src/ package dir (also holds link.html)
PROJECT_ROOT = BASE_DIR.parent
SECRETS_DIR = PROJECT_ROOT / ".secrets"       # gitignored, 0700: secrets only
DATA_DIR = PROJECT_ROOT / "data"              # gitignored, 0700: runtime state + raw archive

# Credentials live in .secrets/.env (quarantined, never committed).
load_dotenv(SECRETS_DIR / ".env")

TOKENS_FILE = SECRETS_DIR / "tokens.json"
CURSORS_FILE = DATA_DIR / "sync_cursors.json"
# Single source of truth: the full raw Plaid object per transaction, keyed by
# transaction_id, persisted as xz-compressed JSONL (one JSON object per line).
# The CSV is a derived projection of this. Kept for audit / QC.
RAW_FILE = DATA_DIR / "transactions_raw.jsonl.xz"
# The CSV is the user-facing deliverable — kept at the project root.
CSV_FILE = PROJECT_ROOT / "transactions.csv"

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


def _ensure_secure_dir(path: Path):
    """Create `path` if needed and enforce owner-only (0700) perms — these dirs hold secrets + private data."""
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)


def _load_json(path: Path, default):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return default


def _save_json(path: Path, data):
    _ensure_secure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2, default=str)
    os.chmod(tmp, 0o600)  # tokens.json holds access tokens — keep owner-only
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


def normalize_txn(raw: dict) -> dict:
    """JSON round-trip a fresh Plaid record (``.to_dict()``) into JSON-native values.

    Plaid's models carry datetime.date / datetime objects, while records loaded back
    from the xz archive carry ISO strings. Normalizing every record at the fetch
    boundary keeps the in-memory store homogeneous: sorting never compares date-to-str
    (a TypeError on delta runs over an existing archive), and a record serializes and
    content-hashes identically everywhere it lands (xz archive, durable store, Drive
    remote) — otherwise datetime fields would diverge ('T' vs space separator) and
    raise permanent spurious reconcile conflicts.
    """
    return json.loads(json.dumps(raw, default=_json_default, ensure_ascii=False))


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
    _ensure_secure_dir(RAW_FILE.parent)
    # str() the keys defensively (mirrors persister.save_jsonl): a not-yet-normalized
    # record holding a datetime.date must never break the sort against ISO strings.
    rows = sorted(
        store.values(),
        key=lambda r: (str(r.get("date") or ""), str(r.get("transaction_id") or "")),
    )
    tmp = RAW_FILE.parent / (RAW_FILE.name + ".tmp")
    with lzma.open(tmp, "wt", encoding="utf-8", preset=9 | lzma.PRESET_EXTREME) as f:
        for r in rows:
            f.write(json.dumps(r, default=_json_default, ensure_ascii=False))
            f.write("\n")
    tmp.replace(RAW_FILE)  # atomic
