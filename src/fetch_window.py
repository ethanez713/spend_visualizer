"""Windowed /transactions/get repair fetch — the reconciliation path (NOT the everyday sync).

The everyday path is cursor-based /transactions/sync (see fetch_transactions.py). This module
adds a *bounded* /transactions/get fetch that the persist orchestration fires only when the
reconciler finds local↔remote drift, over a tight date window computed by persister. Plaid is
the golden source ONLY when it returns data: on an ApiException we log the error and SKIP that
item, never letting a Plaid error delete or overwrite durable local data.

Returns raw dicts in the SAME shape as plaid_client.load_raw_store() values (each Plaid
transaction `.to_dict()`), so they merge cleanly by transaction_id downstream.

Account metadata is deliberately NOT baked into these records. The everyday sync path stores
pure transaction dicts; account fields (institution / account_name / …) are joined only at
CSV-projection time via txn_to_row(txn, account_meta). Mixing account meta into the record
here would make get-path records differ byte-for-byte from sync-path records and trigger
permanent spurious reconcile conflicts. persist_runner fetches account meta separately for the
derived CSV, so keeping records pure preserves reconcile parity with the sync path.
"""
import sys
from datetime import date

from plaid.exceptions import ApiException
from plaid.model.transactions_get_request import TransactionsGetRequest
from plaid.model.transactions_get_request_options import TransactionsGetRequestOptions

from .plaid_client import normalize_txn

# Plaid caps /transactions/get at 500 records per page.
_PAGE_SIZE = 500


def _to_date(value) -> date:
    """Coerce an ISO date string (or date) to a datetime.date for the Plaid request."""
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def fetch_window(client, tokens, start_date, end_date) -> list[dict]:
    """Bounded repair fetch over [start_date, end_date] using /transactions/get.

    For each linked token/item, paginate /transactions/get by offset until every
    transaction in the window is collected, then return the raw `.to_dict()` records.
    `start_date`/`end_date` accept ISO strings (as compute_window emits) or date objects.

    On ApiException for an item: log the Plaid error code and SKIP that item — other items
    still return. A Plaid error must NEVER delete or overwrite local data (Plaid is golden
    only on success).
    """
    start = _to_date(start_date)
    end = _to_date(end_date)

    collected: list[dict] = []
    for t in tokens:
        try:
            offset = 0
            while True:
                req = TransactionsGetRequest(
                    access_token=t["access_token"],
                    start_date=start,
                    end_date=end,
                    options=TransactionsGetRequestOptions(
                        include_personal_finance_category=True,
                        count=_PAGE_SIZE,
                        offset=offset,
                    ),
                )
                resp = client.transactions_get(req)
                page = resp["transactions"]
                for txn in page:
                    collected.append(normalize_txn(txn.to_dict()))
                offset += len(page)
                total = resp["total_transactions"]
                # Stop once we've pulled the whole window (or Plaid returned an empty page).
                if not page or offset >= total:
                    break
        except ApiException as e:
            print(
                f"  fetch_window: skipping {t.get('institution', t.get('item_id'))} — "
                f"Plaid API error: {e.body}",
                file=sys.stderr,
            )
            continue

    return collected
