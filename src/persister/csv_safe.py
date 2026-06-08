"""Spreadsheet formula-injection guard for derived-CSV cells.

A leading ``=``, ``+``, ``-``, ``@``, tab, or CR makes Excel / Google Sheets treat
a text cell as a formula, which can execute on open. Prefix such *text* cells with
a single quote to neutralise them. Numeric cells (int/float/bool) are left untouched,
so a negative amount like ``-42.5`` stays numeric instead of becoming text.

Copied from ``transactions/src/fetch_transactions.py`` ``_csv_safe`` (single source
of the same guard across the sibling projects).
"""
from __future__ import annotations

# A leading one of these makes a spreadsheet treat a cell as a formula.
_CSV_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(v):
    """Neutralise spreadsheet formula injection in text cells by prefixing a quote.

    Numeric cells are floats/ints/bools (not ``str``), so negative amounts stay numeric.
    """
    if isinstance(v, str) and v[:1] in _CSV_INJECTION_PREFIXES:
        return "'" + v
    return v
