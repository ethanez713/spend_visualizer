"""Cadence and next-charge estimates for recurring merchants."""
from __future__ import annotations

from datetime import date, timedelta
from statistics import median

import pandas as pd

# (label, nominal gap in days, charges per month). A median gap is matched to the
# nearest nominal gap within 25%; anything else is "irregular" and gets no forecast.
_CADENCES = [("weekly", 7, 52 / 12), ("every 2 weeks", 14, 26 / 12), ("monthly", 30.4, 1.0),
             ("every 2 months", 61, 0.5), ("quarterly", 91, 1 / 3),
             ("twice a year", 182, 1 / 6), ("yearly", 365, 1 / 12)]
_TOLERANCE = 0.25
IRREGULAR = "irregular"


def cadence(gap_days: float) -> tuple[str, float | None, float | None]:
    """(label, nominal gap, charges per month) for a median gap between charges."""
    for label, nominal, per_month in _CADENCES:
        if abs(gap_days - nominal) <= nominal * _TOLERANCE:
            return label, nominal, per_month
    return IRREGULAR, None, None


def schedule(rows: pd.DataFrame, as_of: date) -> pd.DataFrame:
    """One row per recurring merchant: cadence, typical charge, monthly cost and
    the next expected charge date.

    Same-day charges count as one occurrence, since a merchant billing two line
    items on one day is one billing event. ``Monthly`` is the typical charge
    times the cadence's charges per month; an irregular merchant falls back to
    its mean spend per active month. A next date already in the past stays in
    the past: an overdue charge is the signal that a subscription has ended.
    """
    out = []
    for mid, g in rows.groupby("merchant_id"):
        by_day = g.groupby("date_resolved")["spend"].sum().sort_index()
        days = [date.fromisoformat(str(d)[:10]) for d in by_day.index]
        gaps = [(b - a).days for a, b in zip(days, days[1:])]
        typical = float(median(by_day.values))
        label, nominal, per_month = cadence(median(gaps)) if gaps else (IRREGULAR, None, None)
        if per_month is not None:
            monthly = typical * per_month
            next_due = days[-1] + timedelta(days=round(nominal))
        else:
            monthly = float(g.groupby("month")["spend"].sum().mean())
            next_due = None
        out.append({
            "merchant_id": mid,
            "Merchant": g["merchant"].iloc[0],
            "Category": g["tier1"].iloc[0],
            "Cadence": label,
            "Typical charge": typical,
            "Monthly": round(monthly, 2),
            "Charges": len(days),
            "Last": days[-1].isoformat(),
            "Next expected": next_due.isoformat() if next_due else None,
            "Overdue": bool(next_due and next_due < as_of),
        })
    cols = ["merchant_id", "Merchant", "Category", "Cadence", "Typical charge", "Monthly",
            "Charges", "Last", "Next expected", "Overdue"]
    return (pd.DataFrame(out, columns=cols)
            .sort_values("Monthly", ascending=False).reset_index(drop=True))
