"""Enrich CanonicalTransaction[] into the analysis cube (a pandas DataFrame).

Resolves taxonomy tags, finalizes flow (applying exclusions), derives the flat
facets (person, geo, recurrence, channel, account_type, time) and stores
category/geo/time BOTH as level columns (fast group-by) and as *_path LIST
columns (tree walking / top-K) per PLAN.md §7.4.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from statistics import median

import pandas as pd

from models import CanonicalTransaction
from taxonomy import Taxonomy

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _norm_merchant(name: str | None) -> str:
    if not name:
        return ""
    return _NON_ALNUM.sub("_", name.strip().lower()).strip("_")


def _resolve_date(t: CanonicalTransaction) -> str | None:
    # authorized_date preferred, posted_date fallback (PLAN.md §8 time)
    return t.authorized_date or t.posted_date


def _time_dims(iso: str | None) -> dict:
    if not iso:
        return dict(year=None, quarter=None, month=None, week=None, dow=None, path=[])
    try:
        d = datetime.strptime(iso[:10], "%Y-%m-%d").date()
    except ValueError:
        return dict(year=None, quarter=None, month=None, week=None, dow=None, path=[])
    q = f"Q{(d.month - 1) // 3 + 1}"
    iso_year, iso_week, _ = d.isocalendar()
    week = f"{iso_year}-W{iso_week:02d}"
    month = f"{d.year}-{d.month:02d}"
    dow = d.strftime("%a")
    return dict(
        year=d.year, quarter=q, month=month, week=week, dow=dow,
        path=[str(d.year), q, month, week, dow],
    )


def _geo_dims(t: CanonicalTransaction) -> dict:
    loc = t.location or {}
    country = loc.get("country")
    currency = (t.currency or "USD").upper()
    international = (country not in (None, "US")) or (currency != "USD")
    scope = "International" if international else "Domestic"
    country_label = country or ("US" if not international else "Unknown")
    city = loc.get("city") or "Unknown"
    return dict(
        scope=scope, country=country_label, city=city,
        path=[scope, country_label, city],
    )


def _merchant_dims(t: CanonicalTransaction) -> dict:
    name = t.merchant_name
    brand = None
    if not name and t.counterparties:
        cp = t.counterparties[0] or {}
        name = cp.get("name")
        brand = cp.get("name")
    label = name or (t.name or "Unknown")
    mid = t.merchant_entity_id or _norm_merchant(name) or "unknown"
    return dict(id=mid, name=label, brand=brand or name)


def _detect_recurrence(df: pd.DataFrame) -> pd.Series:
    """Flag recurring merchants: >=3 hits, regular cadence, similar amounts.

    Heuristic, per-merchant (PLAN.md §8). Cadence regular if the median gap is
    near a weekly/biweekly/monthly period and gaps are low-variance; amounts
    similar if coefficient of variation is small.
    """
    rec = pd.Series("one-off", index=df.index, dtype="object")
    spend = df[df["flow"] == "spend"]
    for mid, grp in spend.groupby("merchant_id"):
        if mid in ("", "unknown") or len(grp) < 3:
            continue
        g = grp.sort_values("date_resolved")
        dates = [
            datetime.strptime(d[:10], "%Y-%m-%d").date()
            for d in g["date_resolved"].dropna()
        ]
        if len(dates) < 3:
            continue
        gaps = [(b - a).days for a, b in zip(dates, dates[1:]) if (b - a).days > 0]
        if len(gaps) < 2:
            continue
        med = median(gaps)
        if med <= 0:
            continue
        spread = max(gaps) - min(gaps)
        near_period = any(abs(med - p) <= tol for p, tol in ((7, 3), (14, 4), (30, 6), (90, 12)))
        amounts = g["abs_amount"].tolist()
        mean_amt = sum(amounts) / len(amounts)
        cv = (pd.Series(amounts).std() / mean_amt) if mean_amt else 1.0
        if near_period and spread <= med and cv < 0.35:
            rec.loc[g.index] = "recurring"
    return rec


def enrich(
    txns: list[CanonicalTransaction],
    taxonomy: Taxonomy,
    accounts: dict[str, dict] | None = None,
) -> pd.DataFrame:
    accounts = accounts or {}
    records: list[dict] = []

    for t in txns:
        acct = accounts.get(t.account_id, {})
        if acct.get("include") is False:
            continue

        tags = taxonomy.resolve(
            t.pfc_detailed, t.pfc_primary, t.merchant_name, t.merchant_entity_id
        )
        # finalize flow: exclusions win, else direction -> income/spend
        if tags.excluded:
            flow = "excluded"
        else:
            flow = "income" if t.direction == "in" else "spend"

        abs_amt = abs(t.amount)
        m = _merchant_dims(t)
        g = _geo_dims(t)
        iso = _resolve_date(t)
        tm = _time_dims(iso)

        records.append(
            {
                "transaction_id": t.transaction_id,
                "account_id": t.account_id,
                "name": t.name,
                "amount": t.amount,
                "abs_amount": abs_amt,
                "flow": flow,
                "spend": abs_amt if flow == "spend" else 0.0,
                "income": abs_amt if flow == "income" else 0.0,
                "count": 1,
                # category dims — level columns + path
                "tier0": tags.tier0,
                "tier1": tags.tier1,
                "tier2": tags.tier2,
                "atom": tags.atom,
                "merchant_id": m["id"],
                "merchant": m["name"],
                "merchant_brand": m["brand"],
                "category_path": tags.category_path + [m["name"]],
                "necessity": tags.tier0,
                "unmapped": tags.unmapped,
                # pfc signals
                "pfc_primary": t.pfc_primary,
                "pfc_detailed": t.pfc_detailed,
                "confidence": t.pfc_confidence or "UNKNOWN",
                # geo
                "geo_scope": g["scope"],
                "geo_country": g["country"],
                "geo_city": g["city"],
                "geo_path": g["path"],
                # time
                "date_resolved": iso,
                "year": tm["year"],
                "quarter": tm["quarter"],
                "month": tm["month"],
                "week": tm["week"],
                "dow": tm["dow"],
                "time_path": tm["path"],
                # facets — the record's own owner stamp (collector-written) wins;
                # accounts.yaml `person` is the fallback for un-stamped history.
                "person": t.owner or acct.get("person", "Unknown"),
                "channel": t.payment_channel or "unknown",
                "account_type": acct.get("type", "unknown"),
                "account_name": acct.get("name", (t.account_id or "?")[:8]),
                "institution": acct.get("institution", "Unknown"),
                # presentation
                "logo_url": t.logo_url,
                "website": t.website,
                "pending": t.pending,
            }
        )

    df = pd.DataFrame.from_records(records)
    if df.empty:
        return df
    df["recurrence"] = _detect_recurrence(df)
    return df
