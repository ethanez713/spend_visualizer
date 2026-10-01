"""Date-range presets and the period a range is compared against.

Every preset is anchored on the latest date in the archive, not on today: the
archive is refreshed on a monthly ritual, so "this month" measured from the wall
clock is usually an empty range.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta

CUSTOM = "Custom"
ALL_TIME = "All time"
PRESETS = ["This month", "Last month", "Last 3 months", "Last 12 months",
           "Year to date", "Last year", ALL_TIME, CUSTOM]
DEFAULT_PRESET = "Last 12 months"


def month_start(d: date) -> date:
    return d.replace(day=1)


def month_end(d: date) -> date:
    return d.replace(day=monthrange(d.year, d.month)[1])


def add_months(d: date, n: int) -> date:
    """Shift by whole months, clamping the day to the target month's length."""
    y, m = divmod(d.year * 12 + d.month - 1 + n, 12)
    return date(y, m + 1, min(d.day, monthrange(y, m + 1)[1]))


def preset_range(preset: str, anchor: date, earliest: date) -> tuple[date, date]:
    """Inclusive (from, to) for a preset. "Last N months" means N whole calendar
    months ending with the anchor's month, so monthly averages over the range
    divide by N."""
    this_start = month_start(anchor)
    if preset == "This month":
        return this_start, anchor
    if preset == "Last month":
        prev = add_months(this_start, -1)
        return prev, month_end(prev)
    if preset == "Last 3 months":
        return add_months(this_start, -2), anchor
    if preset == "Last 12 months":
        return add_months(this_start, -11), anchor
    if preset == "Year to date":
        return date(anchor.year, 1, 1), anchor
    if preset == "Last year":
        return date(anchor.year - 1, 1, 1), date(anchor.year - 1, 12, 31)
    return earliest, anchor


def previous_period(date_from: date, date_to: date) -> tuple[date, date]:
    """The equal-length period immediately before a range.

    A range made of whole calendar months steps back by months, so February is
    compared with January rather than with a 28-day window straddling two months.
    """
    whole_months = date_from.day == 1 and date_to == month_end(date_to)
    if whole_months:
        n = (date_to.year - date_from.year) * 12 + date_to.month - date_from.month + 1
        prev_from = add_months(date_from, -n)
        return prev_from, month_end(add_months(prev_from, n - 1))
    if date_from.day == 1:
        # month-to-date style range: same day span, shifted by its month count
        n = (date_to.year - date_from.year) * 12 + date_to.month - date_from.month + 1
        return add_months(date_from, -n), add_months(date_to, -n)
    span = (date_to - date_from).days + 1
    return date_from - timedelta(days=span), date_from - timedelta(days=1)
