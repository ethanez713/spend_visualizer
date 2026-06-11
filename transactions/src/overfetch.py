"""Periodic 90-day safety-net overfetch over the everyday cursor sync.

The cursor-based /transactions/sync is the only thing keeping the store correct: a
missed `added` delta silently loses a transaction; a missed `removed` delta silently
leaves a stale one. Roughly every 30 days this module re-fetches a full 90-day window
via /transactions/get (Plaid golden when it succeeds) and reconciles it against the raw
store. A rolling 90-day window on a 30-day cadence re-fetches every record 3–4 times
over its life, so any single sync miss is caught within a month.

Policy (deliberate, user-confirmed): ADD records Plaid returns that we're missing,
OVERWRITE records whose content changed — but NEVER delete. In-window posted records
absent from Plaid's golden response (on an item that fetched cleanly) are flagged as
stale in data/overfetch_log.jsonl for manual review. Never deleting sidesteps the
persister's preservation bias (a pruned record would be resurrected from the Drive
remote at the next reconcile) and makes a false deletion of real history impossible.

In steady state every count in the log is ~0; a nonzero added/stale count means the
cursor path missed a delta — that signal is the point of this safety net.
"""
import sys
from datetime import date, timedelta

from .fetch_window import _fetch_window_items
from .plaid_client import (
    append_overfetch_log,
    load_overfetch_state,
    save_overfetch_state,
    save_raw_store,
)

OVERFETCH_WINDOW_DAYS = 90
OVERFETCH_INTERVAL_DAYS = 30
# Most-recent days excluded from STALE detection only (pending churn + get-vs-sync
# timing make the tail unreliable); adds/overwrites still use the full window.
STALE_TRAILING_BUFFER_DAYS = 3
# Cap per-run added/changed id samples in the log (the first run can add thousands);
# stale ids are the actionable part and are always logged in full.
_LOG_SAMPLE_CAP = 20


def overfetch_due(force) -> bool:
    """Whether the safety-net overfetch should run this fetch.

    `force` is True (--overfetch) / False (--no-overfetch) / None (auto: due when no
    state exists yet or the last run is >= OVERFETCH_INTERVAL_DAYS old).
    """
    if force is not None:
        return force
    last = load_overfetch_state().get("last_overfetch")
    if not last:
        return True
    return (date.today() - date.fromisoformat(last)).days >= OVERFETCH_INTERVAL_DAYS


def run_overfetch(client, tokens, raw_store: dict) -> dict:
    """Full-window /transactions/get over the last 90 days; mutate `raw_store` in place.

    Adds missing records, overwrites changed ones (Plaid golden), flags — never
    deletes — stale ones. Stale = posted, dated inside the window (minus the trailing
    buffer), on an account covered by a fully-successful item, yet absent from the
    golden response. Partial records from an item that errored mid-pagination still
    merge (adds/overwrites are always safe), but that item covers nothing, so its
    records can never be falsely flagged. If every item fails, nothing is merged and
    the cadence clock is NOT advanced (a transient Plaid outage retries next run).
    """
    today = date.today()
    start = today - timedelta(days=OVERFETCH_WINDOW_DAYS)

    fresh: list[dict] = []
    covered_accounts: set = set()
    items_ok = items_failed = 0
    for _t, records, ok in _fetch_window_items(client, tokens, start, today):
        fresh.extend(records)
        if ok:
            items_ok += 1
            covered_accounts |= {r.get("account_id") for r in records}
        else:
            items_failed += 1

    if items_ok == 0:
        print(
            "  overfetch: every item failed — nothing merged; will retry next run",
            file=sys.stderr,
        )
        return {"added": 0, "changed": 0, "stale": 0, "items_ok": 0}

    prior = dict(raw_store)
    added: set = set()
    changed: set = set()
    for rec in fresh:
        tid = rec.get("transaction_id")
        if tid is None:
            continue
        if tid not in prior:
            added.add(tid)
        elif prior[tid] != rec:
            changed.add(tid)
        raw_store[tid] = rec

    golden_ids = {r.get("transaction_id") for r in fresh}
    start_iso = start.isoformat()
    stale_cutoff = (today - timedelta(days=STALE_TRAILING_BUFFER_DAYS)).isoformat()
    stale_ids = sorted(
        tid for tid, r in prior.items()
        if tid not in golden_ids
        and not r.get("pending")
        and r.get("account_id") in covered_accounts
        and r.get("date")
        and start_iso <= str(r["date"])[:10] <= stale_cutoff
    )

    append_overfetch_log({
        "ts": today.isoformat(),
        "window_start": start_iso,
        "window_end": today.isoformat(),
        "items_ok": items_ok,
        "items_failed": items_failed,
        "added_count": len(added),
        "changed_count": len(changed),
        "stale_count": len(stale_ids),
        "added_ids_sample": sorted(added)[:_LOG_SAMPLE_CAP],
        "changed_ids_sample": sorted(changed)[:_LOG_SAMPLE_CAP],
        "stale_ids": stale_ids,
    })

    print(
        f"  overfetch: [{start_iso} .. {today.isoformat()}] "
        f"+{len(added)} added (missed by cursor sync), {len(changed)} overwritten, "
        f"{len(stale_ids)} stale flagged"
    )
    if stale_ids:
        shown = ", ".join(stale_ids[:10]) + (", …" if len(stale_ids) > 10 else "")
        print(
            f"  overfetch: STALE — local but absent from Plaid's golden window; "
            f"NOT deleted. Review data/overfetch_log.jsonl: {shown}",
            file=sys.stderr,
        )

    # Checkpoint the merged store before advancing the cadence clock (mirrors
    # sync_item's store-then-cursor order): a crash in between re-runs the overfetch.
    save_raw_store(raw_store)
    save_overfetch_state(today.isoformat())
    return {
        "added": len(added),
        "changed": len(changed),
        "stale": len(stale_ids),
        "items_ok": items_ok,
    }
