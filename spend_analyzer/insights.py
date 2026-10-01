"""Period-over-period comparisons behind the Overview tab and the spend table."""
from __future__ import annotations

from calendar import monthrange
from datetime import date

import pandas as pd

from periods import add_months, month_start


def _month_key(d: date) -> str:
    return f"{d.year}-{d.month:02d}"


def cumulative_by_day(rows: pd.DataFrame, month: date, through_day: int | None = None) -> pd.Series:
    """Running spend total indexed by day of month (1..last day, or ..through_day).

    Days without spend carry the running total forward, so two months can be read
    against each other at any day.
    """
    last = through_day or monthrange(month.year, month.month)[1]
    in_month = rows[rows["month"] == _month_key(month)]
    daily = in_month.groupby(in_month["date_resolved"].str[8:10].astype(int))["spend"].sum()
    return daily.reindex(range(1, last + 1), fill_value=0.0).cumsum()


def average_month_pace(rows: pd.DataFrame, anchor: date, months: int) -> pd.Series | None:
    """Mean cumulative curve of the `months` whole months before the anchor's month.

    Truncated at day 28 beyond which months differ in length; None when the
    archive holds none of those months.
    """
    present = set(rows["month"].dropna())
    curves = []
    for i in range(1, months + 1):
        m = add_months(month_start(anchor), -i)
        if _month_key(m) in present:
            curves.append(cumulative_by_day(rows, m, through_day=28))
    if not curves:
        return None
    return pd.concat(curves, axis=1).mean(axis=1)


def period_changes(rows: pd.DataFrame, col: str, current: tuple[date, date],
                   previous: tuple[date, date]) -> pd.DataFrame:
    """Spend per `col` value in two periods: columns Current, Previous, Change.

    Sorted by the size of the change, largest first, whichever its direction.
    """
    def total(span: tuple[date, date]) -> pd.Series:
        d = rows["date_resolved"].str[:10]
        sel = rows[(d >= span[0].isoformat()) & (d <= span[1].isoformat())]
        return sel.groupby(col)["spend"].sum()

    out = pd.DataFrame({"Current": total(current), "Previous": total(previous)}).fillna(0.0)
    out["Change"] = out["Current"] - out["Previous"]
    out = out[(out["Current"] > 0) | (out["Previous"] > 0)]
    return out.reindex(out["Change"].abs().sort_values(ascending=False).index)
