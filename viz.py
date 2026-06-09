"""Shared presentation helpers: formatting, heatmaps, safe export."""
from __future__ import annotations

import pandas as pd

# ----------------------------------------------------------------- formatting
def money(x: float) -> str:
    try:
        return f"${x:,.0f}" if abs(x) >= 100 else f"${x:,.2f}"
    except (TypeError, ValueError):
        return "—"


def humanize_atom(s: str | None) -> str:
    """Render an atom code for display: underscores -> spaces (line-breakable)."""
    if not s:
        return ""
    return str(s).replace("_", " ")


def blank_if_missing(v):
    """None / NaN / 'None' -> '' so image columns don't print the word None."""
    if v is None:
        return ""
    if isinstance(v, float) and pd.isna(v):
        return ""
    if isinstance(v, str) and v.strip().lower() in ("none", "nan", ""):
        return ""
    return v


# -------------------------------------------------------------------- heatmaps
def _green_scale(series: pd.Series) -> list[str]:
    """Sequential green with black text — darker green = larger value."""
    vals = pd.to_numeric(series, errors="coerce")
    lo, hi = vals.min(), vals.max()
    span = (hi - lo) or 1.0
    out = []
    for v in vals:
        if pd.isna(v):
            out.append("")
            continue
        t = (v - lo) / span                       # 0..1
        r = int(232 - t * 140)
        b = int(232 - t * 140)
        g = int(245 - t * 40)
        out.append(f"background-color: rgb({r}, {g}, {b}); color: black")
    return out


def _diverging_scale(series: pd.Series, midpoint: float) -> list[str]:
    """Green below the midpoint (good/under), red above (bad/over). Black text."""
    vals = pd.to_numeric(series, errors="coerce")
    hi = max(abs(vals.max() - midpoint), abs(vals.min() - midpoint)) or 1.0
    out = []
    for v in vals:
        if pd.isna(v):
            out.append("")
            continue
        t = (v - midpoint) / hi                   # -1..1
        if t >= 0:                                # over budget -> red
            inten = min(t, 1.0)
            g = int(235 - inten * 150)
            out.append(f"background-color: rgb(235, {g}, {g}); color: black")
        else:                                     # under budget -> green
            inten = min(-t, 1.0)
            r = int(235 - inten * 150)
            out.append(f"background-color: rgb({r}, 235, {r}); color: black")
    return out


def style_table(
    df: pd.DataFrame,
    *,
    money_cols: list[str] | None = None,
    int_cols: list[str] | None = None,
    pct_cols: list[str] | None = None,
    green_cols: list[str] | None = None,
    diverging: dict[str, float] | None = None,
    grey_rows: pd.Series | None = None,
):
    """One styler for every table: typed formatting + green / diverging heatmaps.

    money_cols  -> $ formatted     int_cols -> plain integers (NOT $)
    pct_cols    -> 0% formatted     green_cols -> sequential green gradient
    diverging   -> {col: midpoint}  red(>mid)/green(<mid)
    grey_rows   -> boolean Series; True rows rendered greyed (hidden indicator)
    """
    money_cols = money_cols or []
    int_cols = int_cols or []
    pct_cols = pct_cols or []
    green_cols = green_cols or []
    diverging = diverging or {}

    fmt: dict = {}
    for c in df.columns:
        if c in money_cols:
            fmt[c] = lambda v: money(v) if pd.notna(v) else "—"
        elif c in pct_cols:
            fmt[c] = lambda v: f"{v:,.0f}%" if pd.notna(v) else "—"
        elif c in int_cols:
            fmt[c] = lambda v: f"{int(v):,}" if pd.notna(v) else "—"

    styler = df.style.format(fmt, na_rep="—")
    for c in green_cols:
        if c in df.columns:
            styler = styler.apply(_green_scale, subset=[c])
    for c, mid in diverging.items():
        if c in df.columns:
            styler = styler.apply(lambda s, m=mid: _diverging_scale(s, m), subset=[c])
    if grey_rows is not None and grey_rows.any():
        def _grey(row):
            if grey_rows.get(row.name, False):
                return ["color: #999; font-style: italic; background-color: #f4f4f4"] * len(row)
            return [""] * len(row)
        styler = styler.apply(_grey, axis=1)
    return styler


# ----------------------------------------------------------------------- export
_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def sanitize_for_csv(df: pd.DataFrame) -> pd.DataFrame:
    """Guard against CSV/spreadsheet formula injection (security baseline).

    Any string cell starting with =, +, -, @, tab or CR is prefixed with a
    single quote so it cannot execute when opened in Excel/Sheets.
    """
    out = df.copy()
    for col in out.columns:
        if out[col].dtype == object:
            out[col] = out[col].map(
                lambda v: "'" + v if isinstance(v, str) and v.startswith(_INJECTION_PREFIXES) else v
            )
    return out
