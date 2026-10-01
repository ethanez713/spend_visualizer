from datetime import date

import pytest

from periods import add_months, preset_range, previous_period

ANCHOR, EARLIEST = date(2026, 9, 29), date(2024, 10, 3)


@pytest.mark.parametrize("preset,expected", [
    ("This month", (date(2026, 9, 1), ANCHOR)),
    ("Last month", (date(2026, 8, 1), date(2026, 8, 31))),
    ("Last 3 months", (date(2026, 7, 1), ANCHOR)),
    ("Last 12 months", (date(2025, 10, 1), ANCHOR)),
    ("Year to date", (date(2026, 1, 1), ANCHOR)),
    ("Last year", (date(2025, 1, 1), date(2025, 12, 31))),
    ("All time", (EARLIEST, ANCHOR)),
])
def test_preset_ranges_are_anchored_on_the_latest_archive_date(preset, expected):
    assert preset_range(preset, ANCHOR, EARLIEST) == expected


def test_add_months_clamps_to_the_shorter_month_and_crosses_years():
    assert add_months(date(2026, 3, 31), -1) == date(2026, 2, 28)
    assert add_months(date(2026, 1, 15), -2) == date(2025, 11, 15)
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)


def test_whole_month_range_is_compared_with_the_whole_months_before_it():
    assert previous_period(date(2026, 2, 1), date(2026, 2, 28)) == (
        date(2026, 1, 1), date(2026, 1, 31))
    assert previous_period(date(2026, 1, 1), date(2026, 3, 31)) == (
        date(2025, 10, 1), date(2025, 12, 31))


def test_month_to_date_range_is_compared_with_the_same_days_a_period_earlier():
    assert previous_period(date(2026, 9, 1), date(2026, 9, 29)) == (
        date(2026, 8, 1), date(2026, 8, 29))
    # three months to date step back three months, not one
    assert previous_period(date(2026, 7, 1), date(2026, 9, 29)) == (
        date(2026, 4, 1), date(2026, 6, 29))


def test_arbitrary_range_is_compared_with_the_equal_span_immediately_before():
    assert previous_period(date(2026, 9, 10), date(2026, 9, 19)) == (
        date(2026, 8, 31), date(2026, 9, 9))
