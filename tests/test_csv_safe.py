"""Tests for csv_safe.py — formula-injection guard."""
import pytest

from persister.csv_safe import csv_safe


@pytest.mark.parametrize("dangerous", ["=SUM(A1)", "+1", "-cmd", "@x", "\tx", "\rx"])
def given_dangerous_text_when_guarded_then_quote_prefixed(dangerous):
    assert csv_safe(dangerous) == "'" + dangerous


def given_safe_text_when_guarded_then_unchanged():
    assert csv_safe("Trader Joe's") == "Trader Joe's"


def given_numeric_when_guarded_then_unchanged():
    # Numerics are not str → never quoted, so negative amounts stay numeric.
    assert csv_safe(-42.5) == -42.5
    assert csv_safe(7) == 7
    assert csv_safe(True) is True


def given_none_when_guarded_then_unchanged():
    assert csv_safe(None) is None
