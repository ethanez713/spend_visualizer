"""Spreadsheet formula-injection guard for derived CSVs.

A cell whose text begins with ``= + - @`` (or a leading tab/CR) is executed as a
formula by Excel and Google Sheets. Transaction text (merchant names, memos, the raw
bank description) is attacker-influenceable — e.g. the memo on a payment someone else
sends you — so a crafted value could run a formula (``=IMPORTDATA(...)``) inside an
uploaded Sheet and exfiltrate it. Prefix a single quote to force literal text.

Copied from ``transactions/src/fetch_transactions.py`` / ``converter/src/converter.py``
to keep this project self-contained (no cross-repo import). Numeric cells (floats/bools,
e.g. ``amount``) are left untouched so negative amounts stay numeric.
"""
from __future__ import annotations

# A leading =, +, -, @, tab, or CR makes a spreadsheet treat a cell as a formula.
_CSV_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value):
    """Neutralise formula injection in a single cell value; pass non-text through."""
    if isinstance(value, str) and value[:1] in _CSV_INJECTION_PREFIXES:
        return "'" + value
    return value
