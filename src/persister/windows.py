"""Compute a bounded date window for a targeted Plaid ``/transactions/get`` repair.

The everyday path is cursor ``/transactions/sync``; the windowed ``get`` fires only
when the reconciler finds drift. Keeping the window tight avoids over-fetch while always
covering pending rows and any specific transactions (conflicts / remote-only gaps) the
caller wants re-checked.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable

# Days back from the latest settled transaction to start the repair window.
_BACKSTOP_DAYS = 7
# Default backfill span when the store is empty / has no settled rows to anchor on.
EMPTY_BACKFILL_DAYS = 730


@dataclass
class Window:
    start_date: str  # ISO YYYY-MM-DD
    end_date: str    # ISO (today)
    pending_ids: list[str]


def _to_date(value) -> date:
    """Parse an ISO date string (tolerating a trailing time component) to ``date``."""
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def compute_window(store: dict[str, dict], extra_tids: Iterable[str] = ()) -> Window:
    """Date window for a targeted Plaid ``/transactions/get`` repair fetch.

    - ``latest_settled`` = max date among non-pending records.
    - ``start`` = ``latest_settled - 7d`` (one week back past the latest settled row).
    - ``start`` is then pulled earlier to also cover every pending record's date and the
      date of every ``extra_tid`` present in the store (conflicts / remote-only gaps).
    - ``end`` = today.
    - ``pending_ids`` = keys of all pending records.

    Empty store (or a store with no settled rows to anchor on) defaults to a
    ``today - 730d .. today`` backfill — wide but bounded; pending/extra dates only
    ever widen it further, never shrink it.
    """
    today = date.today()

    settled_dates = [
        r["date"] for r in store.values()
        if not r.get("pending") and r.get("date")
    ]
    pending_ids = [key for key, r in store.items() if r.get("pending")]
    pending_dates = [
        r["date"] for r in store.values()
        if r.get("pending") and r.get("date")
    ]
    extra_dates = [
        store[t]["date"] for t in extra_tids
        if t in store and store[t].get("date")
    ]

    if settled_dates:
        start = max(_to_date(d) for d in settled_dates) - timedelta(days=_BACKSTOP_DAYS)
    else:
        # Empty store, or every row pending: no settled anchor → safe bounded backfill.
        start = today - timedelta(days=EMPTY_BACKFILL_DAYS)

    # Always cover pending rows and any explicitly-requested transactions.
    for d in pending_dates + extra_dates:
        start = min(start, _to_date(d))

    return Window(start.isoformat(), today.isoformat(), pending_ids)
