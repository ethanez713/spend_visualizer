"""Shared presentation helpers: formatting, heatmaps, safe export."""
from __future__ import annotations

import pandas as pd

import theme

def money(x: float) -> str:
    try:
        sign = "−" if x < 0 else ""
        return f"{sign}${abs(x):,.0f}" if abs(x) >= 100 else f"{sign}${abs(x):,.2f}"
    except (TypeError, ValueError):
        return "—"


def money_md(x: float) -> str:
    """`money` for Markdown surfaces (captions, st.markdown): two bare dollar signs
    in one string are read as LaTeX delimiters and garble the text between them."""
    return money(x).replace("$", "\\$")


_MD_SPECIAL = str.maketrans({c: "\\" + c for c in "\\`*_[]$~"})


def md_text(s: str) -> str:
    """Data shown inside a Markdown surface, with its markup characters neutralised:
    a merchant called ``**1234`` is a name, not a bold run."""
    return str(s).translate(_MD_SPECIAL)


_ROW_PX = 35


def table_height(n_rows: int, max_rows: int = 30) -> int:
    """Pixel height that shows every row of an st.dataframe up to `max_rows`, so a
    short table is never cut off above empty page."""
    return _ROW_PX * (min(n_rows, max_rows) + 1) + 3


def humanize_atom(s: str | None) -> str:
    """Render an atom code for display: underscores -> spaces (line-breakable)."""
    if not s:
        return ""
    return str(s).replace("_", " ")


# Categorization-provenance badges: who last set this row's category.
# Blank step = Plaid's own category, never changed by the pipeline.
PROVENANCE_BADGES = {
    "mechanical": "rule",
    "llm": "LLM",
    "review": "review",
    "manual": "manual",
}


def provenance_why(step: str | None, reason: str | None,
                   review_pending: bool = False) -> str:
    """One-cell 'why this category' label: badge + the rule name / intent id."""
    parts = []
    badge = PROVENANCE_BADGES.get(str(step or ""))
    if badge:
        parts.append(f"{badge} · {reason}" if reason else badge)
    if review_pending:
        parts.append("suggestion pending")
    return "  ".join(parts)


def blank_if_missing(v):
    """None / NaN / 'None' -> '' so image columns don't print the word None."""
    if v is None:
        return ""
    if isinstance(v, float) and pd.isna(v):
        return ""
    if isinstance(v, str) and v.strip().lower() in ("none", "nan", ""):
        return ""
    return v


def _sequential_scale(series: pd.Series) -> list[str]:
    """Deeper fill = larger value."""
    vals = pd.to_numeric(series, errors="coerce")
    lo, hi = vals.min(), vals.max()
    span = (hi - lo) or 1.0
    return ["" if pd.isna(v)
            else f"background-color: {theme.sequential((v - lo) / span)}; {theme.CELL_TEXT}"
            for v in vals]


def _diverging_scale(series: pd.Series, midpoint: float, sign: int = 1) -> list[str]:
    """Jade on the good side of the midpoint, hazard on the bad side. `sign` is 1
    where above the midpoint is bad (spend over budget), -1 where it is good (net)."""
    vals = pd.to_numeric(series, errors="coerce")
    hi = max(abs(vals.max() - midpoint), abs(vals.min() - midpoint)) or 1.0
    return ["" if pd.isna(v)
            else f"background-color: {theme.diverging(sign * (v - midpoint) / hi)}; {theme.CELL_TEXT}"
            for v in vals]


def style_table(
    df: pd.DataFrame,
    *,
    money_cols: list[str] | None = None,
    int_cols: list[str] | None = None,
    pct_cols: list[str] | None = None,
    heat_cols: list[str] | None = None,
    diverging: dict[str, float] | None = None,
    higher_is_better: list[str] | None = None,
    grey_rows: pd.Series | None = None,
):
    """One styler for every table: typed formatting + sequential / diverging heatmaps.

    money_cols  -> $ formatted     int_cols -> plain integers (NOT $)
    pct_cols    -> 0% formatted     heat_cols -> sequential magnitude fill
    diverging   -> {col: midpoint}  over(>mid)/under(<mid); columns also listed in
                   higher_is_better flip, so above the midpoint reads as good
    grey_rows   -> boolean Series; True rows rendered greyed (hidden indicator)
    """
    money_cols = money_cols or []
    int_cols = int_cols or []
    pct_cols = pct_cols or []
    heat_cols = heat_cols or []
    diverging = diverging or {}

    fmt: dict = {}
    for c in df.columns:
        if c in money_cols:
            fmt[c] = lambda v: money(v) if pd.notna(v) else "—"
        elif c in pct_cols:
            fmt[c] = lambda v: f"{v:,.0f}%".replace("-", "−") if pd.notna(v) else "—"
        elif c in int_cols:
            fmt[c] = lambda v: f"{int(v):,}" if pd.notna(v) else "—"

    styler = df.style.format(fmt, na_rep="—")
    for c in heat_cols:
        if c in df.columns:
            styler = styler.apply(_sequential_scale, subset=[c])
    for c, mid in diverging.items():
        if c in df.columns:
            sign = -1 if c in (higher_is_better or []) else 1
            styler = styler.apply(lambda s, m=mid, g=sign: _diverging_scale(s, m, g), subset=[c])
    if grey_rows is not None and grey_rows.any():
        def _grey(row):
            if grey_rows.get(row.name, False):
                return [theme.HIDDEN_ROW] * len(row)
            return [""] * len(row)
        styler = styler.apply(_grey, axis=1)
    return styler


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
