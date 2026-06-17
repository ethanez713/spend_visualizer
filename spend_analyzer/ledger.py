"""Load an external pre-categorized ledger CSV for the Budget tab.

This is the *alternative* Budget-tab source (opt-in via ``ledger.csv`` in the
personal ``budget.yaml`` — see ``config_io.Budget``). The ledger is produced by
the sibling "converter" project, which translates Plaid's PFC taxonomy into the
established budget's categories and emits the schema::

    Source, Date, Description, Category, Debit, Credit, Debit Less Credit

We only need three of those columns to drive the Budget view: who (``Source`` →
person), when (``Date`` → month), what (``Category``), and how much
(``Debit Less Credit`` → net spend, positive = money out). Everything else
(account type, channel, necessity) is absent here, which is why the alternative
view honors only the date-range and person filters.

Read-only and fail-soft: malformed dates/amounts are dropped per the security
baseline, never fatal.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

# The converter's ledger schema (plaid_bridge.py): we read these by name.
_PERSON_COL = "Source"
_DATE_COL = "Date"
_CATEGORY_COL = "Category"
_NET_COL = "Debit Less Credit"


def _money(series: pd.Series) -> pd.Series:
    """Parse a ``$1,234.56`` / ``-$58.28`` money column into floats.

    Also tolerates a leading single quote (the converter formula-escapes some
    cells) and blanks → NaN. Positive = money out, matching the ledger.
    """
    cleaned = (
        series.astype(str)
        .str.replace(r"^'", "", regex=True)      # strip formula-injection guard
        .str.replace(r"[$,]", "", regex=True)
        .str.strip()
    )
    return pd.to_numeric(cleaned, errors="coerce")


def load_ledger(path: str | Path) -> pd.DataFrame:
    """Read the converter ledger CSV into a tidy frame.

    Returns columns ``person``, ``month`` (``YYYY-MM``), ``category``, ``spend``
    (net, positive = out). Rows with an unparseable date or a zero/blank amount
    are dropped. Returns an empty frame if the file is missing or unreadable.
    """
    p = Path(path)
    cols = ["person", "month", "category", "spend"]
    if not p.is_file():
        return pd.DataFrame(columns=cols)

    df = pd.read_csv(p, dtype=str)
    needed = {_DATE_COL, _CATEGORY_COL, _NET_COL}
    if needed - set(df.columns):
        return pd.DataFrame(columns=cols)

    # `format="mixed"` parses each row independently, so the loader tolerates
    # whatever the converter emits (it writes MM/DD/YYYY) without one odd row's
    # separator forcing the whole column to NaT.
    dates = pd.to_datetime(df[_DATE_COL], errors="coerce", format="mixed")
    spend = _money(df[_NET_COL])
    out = pd.DataFrame(
        {
            "person": (df[_PERSON_COL] if _PERSON_COL in df.columns else "").astype(str),
            "month": dates.dt.strftime("%Y-%m"),
            "category": df[_CATEGORY_COL].astype(str).str.strip(),
            "spend": spend,
        }
    )
    out = out[out["month"].notna() & out["spend"].notna() & (out["spend"] != 0)]
    return out.reset_index(drop=True)


def monthly_pivot(
    ledger: pd.DataFrame,
    *,
    persons: list[str] | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> pd.DataFrame:
    """Category × month pivot of net spend, honoring the supported filters.

    ``persons`` filters by ``Source``; ``date_from``/``date_to`` are inclusive
    ``YYYY-MM-DD`` bounds compared against each row's month. Index = category,
    columns = month, mirroring the cube's ``_monthly_pivot`` so the Budget view
    can feed either source through the same downstream rendering.
    """
    if ledger.empty:
        return pd.DataFrame()
    df = ledger
    if persons:
        df = df[df["person"].isin(persons)]
    if date_from:
        df = df[df["month"] >= date_from[:7]]
    if date_to:
        df = df[df["month"] <= date_to[:7]]
    if df.empty:
        return pd.DataFrame()
    return df.pivot_table(index="category", columns="month", values="spend",
                          aggfunc="sum").fillna(0.0)
