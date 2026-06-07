"""Fetch all transactions from every linked Plaid Item into one transactions.csv.

Uses /transactions/sync (cursor-based, incremental). Keeps a local store keyed
by transaction_id and applies each sync's added / modified / removed deltas, so
the CSV always reflects current state and is automatically de-duplicated.

The CSV is intentionally wide — it captures essentially every populated field
Plaid returns per transaction (flattened), plus per-account metadata from
/accounts/get, so a downstream analytics app has the maximum raw data to work
with. The variable-length `counterparties` list is preserved as a JSON string.

Run:  ./venv/bin/python fetch_transactions.py
Safe to re-run / schedule — only new or changed transactions are pulled.
"""
import csv
import json
import sys
import time

from plaid.exceptions import ApiException
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.transactions_sync_request import TransactionsSyncRequest
from plaid.model.transactions_sync_request_options import TransactionsSyncRequestOptions

from plaid_client import (
    CSV_FILE,
    RAW_FILE,
    get_client,
    load_cursors,
    load_raw_store,
    load_tokens,
    save_cursor,
    save_raw_store,
)

CSV_COLUMNS = [
    # --- account identity (from /accounts/get) ---
    "institution",
    "account_id",
    "account_name",
    "account_mask",
    "account_official_name",
    "account_type",
    "account_subtype",
    # --- transaction core ---
    "transaction_id",
    "pending",
    "pending_transaction_id",
    "date",                 # posted date
    "authorized_date",      # when the purchase was actually authorized
    "datetime",             # posted timestamp (when available)
    "authorized_datetime",  # authorization timestamp (when available)
    "name",
    "original_description",
    "merchant_name",
    "merchant_entity_id",   # stable merchant id — group-by-merchant without string matching
    "website",
    "logo_url",
    "amount",
    "iso_currency_code",
    "unofficial_currency_code",
    "payment_channel",
    "transaction_type",
    "transaction_code",
    "check_number",
    "account_owner",
    # --- categorization (personal_finance_category) ---
    "pf_category_primary",
    "pf_category_detailed",
    "pf_category_confidence",
    "pf_category_version",
    "pf_category_icon_url",
    # --- location ---
    "location_address",
    "location_city",
    "location_region",
    "location_postal_code",
    "location_country",
    "location_lat",
    "location_lon",
    "location_store_number",
    # --- payment metadata (checks / ACH / transfers) ---
    "payment_reference_number",
    "payment_ppd_id",
    "payment_payee",
    "payment_by_order_of",
    "payment_payer",
    "payment_method",
    "payment_processor",
    "payment_reason",
    # --- counterparties (primary flattened + full list as JSON) ---
    "counterparty_name",
    "counterparty_type",
    "counterparty_entity_id",
    "counterparty_confidence",
    "counterparties_json",
]

# First sync after linking: Plaid may still be pulling history. Retry a few times.
FIRST_RUN_RETRIES = 4
FIRST_RUN_DELAY_S = 5


def _v(x):
    """CSV-safe scalar: None -> '', date/datetime -> ISO string, else as-is."""
    if x is None:
        return ""
    if isinstance(x, bool):
        return x
    if hasattr(x, "isoformat"):
        return x.isoformat()
    return x


def _g(obj, key):
    """Safe get from a (possibly None) Plaid model / dict."""
    if obj is None:
        return None
    return obj.get(key)


def get_account_meta(client, tokens) -> dict:
    """account_id -> {institution, account_name, account_mask, official_name, type, subtype}."""
    meta = {}
    for t in tokens:
        try:
            resp = client.accounts_get(AccountsGetRequest(access_token=t["access_token"]))
            for a in resp["accounts"]:
                meta[a["account_id"]] = {
                    "institution": t["institution"],
                    "account_name": _v(a.get("name")),
                    "account_mask": _v(a.get("mask")),
                    "account_official_name": _v(a.get("official_name")),
                    "account_type": _v(str(a.get("type")) if a.get("type") else ""),
                    "account_subtype": _v(str(a.get("subtype")) if a.get("subtype") else ""),
                }
        except ApiException as e:
            print(f"  warn: accounts_get failed for {t['institution']}: {e.body}",
                  file=sys.stderr)
    return meta


def txn_to_row(txn, account_meta: dict) -> dict:
    """Project one raw transaction dict (Plaid object as dict) into a flat CSV row."""
    pfc = txn.get("personal_finance_category")
    loc = txn.get("location")
    pm = txn.get("payment_meta")
    cps = txn.get("counterparties") or []
    primary_cp = cps[0] if cps else None
    acct = account_meta.get(txn.get("account_id"), {})

    return {
        "institution": acct.get("institution", ""),
        "account_id": _v(txn.get("account_id")),
        "account_name": acct.get("account_name", ""),
        "account_mask": acct.get("account_mask", ""),
        "account_official_name": acct.get("account_official_name", ""),
        "account_type": acct.get("account_type", ""),
        "account_subtype": acct.get("account_subtype", ""),

        "transaction_id": _v(txn.get("transaction_id")),
        "pending": _v(txn.get("pending")),
        "pending_transaction_id": _v(txn.get("pending_transaction_id")),
        "date": _v(txn.get("date")),
        "authorized_date": _v(txn.get("authorized_date")),
        "datetime": _v(txn.get("datetime")),
        "authorized_datetime": _v(txn.get("authorized_datetime")),
        "name": _v(txn.get("name")),
        "original_description": _v(txn.get("original_description")),
        "merchant_name": _v(txn.get("merchant_name")),
        "merchant_entity_id": _v(txn.get("merchant_entity_id")),
        "website": _v(txn.get("website")),
        "logo_url": _v(txn.get("logo_url")),
        "amount": _v(txn.get("amount")),
        "iso_currency_code": _v(txn.get("iso_currency_code")),
        "unofficial_currency_code": _v(txn.get("unofficial_currency_code")),
        "payment_channel": _v(txn.get("payment_channel")),
        "transaction_type": _v(txn.get("transaction_type")),
        "transaction_code": _v(txn.get("transaction_code")),
        "check_number": _v(txn.get("check_number")),
        "account_owner": _v(txn.get("account_owner")),

        "pf_category_primary": _v(_g(pfc, "primary")),
        "pf_category_detailed": _v(_g(pfc, "detailed")),
        "pf_category_confidence": _v(_g(pfc, "confidence_level")),
        "pf_category_version": _v(_g(pfc, "version")),
        "pf_category_icon_url": _v(txn.get("personal_finance_category_icon_url")),

        "location_address": _v(_g(loc, "address")),
        "location_city": _v(_g(loc, "city")),
        "location_region": _v(_g(loc, "region")),
        "location_postal_code": _v(_g(loc, "postal_code")),
        "location_country": _v(_g(loc, "country")),
        "location_lat": _v(_g(loc, "lat")),
        "location_lon": _v(_g(loc, "lon")),
        "location_store_number": _v(_g(loc, "store_number")),

        "payment_reference_number": _v(_g(pm, "reference_number")),
        "payment_ppd_id": _v(_g(pm, "ppd_id")),
        "payment_payee": _v(_g(pm, "payee")),
        "payment_by_order_of": _v(_g(pm, "by_order_of")),
        "payment_payer": _v(_g(pm, "payer")),
        "payment_method": _v(_g(pm, "payment_method")),
        "payment_processor": _v(_g(pm, "payment_processor")),
        "payment_reason": _v(_g(pm, "reason")),

        "counterparty_name": _v(_g(primary_cp, "name")),
        "counterparty_type": _v(str(_g(primary_cp, "type")) if primary_cp else ""),
        "counterparty_entity_id": _v(_g(primary_cp, "entity_id")),
        "counterparty_confidence": _v(_g(primary_cp, "confidence_level")),
        # cps are already plain dicts (raw store holds to_dict()ed objects).
        "counterparties_json": json.dumps(cps, default=str) if cps else "",
    }


def sync_item(client, entry, raw_store) -> dict:
    """Pull all pending deltas for one Item; mutate `raw_store` (full raw objects) in place."""
    item_id = entry["item_id"]
    access_token = entry["access_token"]

    cursor = load_cursors().get(item_id)
    counts = {"added": 0, "modified": 0, "removed": 0}
    first_call = cursor is None

    while True:
        options = TransactionsSyncRequestOptions(include_personal_finance_category=True)
        kwargs = dict(access_token=access_token, options=options)
        if cursor:
            kwargs["cursor"] = cursor
        resp = client.transactions_sync(TransactionsSyncRequest(**kwargs))

        # First-run history may not be ready yet — empty + has_more False. Retry.
        if (
            first_call
            and not resp["added"]
            and not resp["has_more"]
            and not resp["next_cursor"]
        ):
            return {**counts, "_retry": True}
        first_call = False

        for txn in resp["added"]:
            raw_store[txn["transaction_id"]] = txn.to_dict()
            counts["added"] += 1
        for txn in resp["modified"]:
            raw_store[txn["transaction_id"]] = txn.to_dict()
            counts["modified"] += 1
        for removed in resp["removed"]:
            if raw_store.pop(removed["transaction_id"], None) is not None:
                counts["removed"] += 1

        cursor = resp["next_cursor"]
        save_raw_store(raw_store)  # checkpoint raw archive before advancing cursor
        save_cursor(item_id, cursor)  # checkpoint after every page

        if not resp["has_more"]:
            break

    return counts


def write_csv(raw_store: dict, account_meta: dict):
    """Derive the flat CSV from the raw store (the single source of truth)."""
    rows = [txn_to_row(txn, account_meta) for txn in raw_store.values()]
    rows.sort(key=lambda r: (r.get("date") or "", r.get("institution") or ""))
    with open(CSV_FILE, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main():
    client = get_client()
    tokens = load_tokens()
    if not tokens:
        print("No linked banks yet. Run app.py and link a bank first.")
        return

    account_meta = get_account_meta(client, tokens)
    raw_store = load_raw_store()
    print(f"Syncing {len(tokens)} linked bank(s)…\n")

    for entry in tokens:
        institution = entry["institution"]
        try:
            counts = sync_item(client, entry, raw_store)
            attempt = 0
            while counts.get("_retry") and attempt < FIRST_RUN_RETRIES:
                attempt += 1
                print(
                    f"  {institution}: history not ready, retrying "
                    f"({attempt}/{FIRST_RUN_RETRIES})…"
                )
                time.sleep(FIRST_RUN_DELAY_S)
                counts = sync_item(client, entry, raw_store)
            if counts.get("_retry"):
                print(f"  {institution}: still no data — try again in a few minutes.")
                continue
            print(
                f"  {institution}: +{counts['added']} added, "
                f"{counts['modified']} modified, {counts['removed']} removed"
            )
        except ApiException as e:
            print(f"  {institution}: API error — {e.body}", file=sys.stderr)

    save_raw_store(raw_store)
    write_csv(raw_store, account_meta)
    size_kb = RAW_FILE.stat().st_size / 1024 if RAW_FILE.exists() else 0
    print(f"\nWrote {len(raw_store)} transactions to {CSV_FILE}")
    print(f"Raw archive: {RAW_FILE.name} ({size_kb:.0f} KB compressed)")


if __name__ == "__main__":
    main()
