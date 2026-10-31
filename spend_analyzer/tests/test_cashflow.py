"""Which Cash flow periods the archive covers only in part."""
from datetime import date

import pandas as pd

from views.cashflow import partial_periods

EARLIEST, ANCHOR = date(2026, 3, 6), date(2026, 9, 29)


def _months(*months):
    return pd.DataFrame({"month": list(months)})


def test_given_first_month_starting_late_when_checked_then_it_alone_is_partial():
    assert partial_periods(_months("2026-03", "2026-04"), "Month", EARLIEST, ANCHOR).tolist() == [True, False]


def test_given_last_month_ending_early_when_checked_then_it_alone_is_partial():
    assert partial_periods(_months("2026-08", "2026-09"), "Month", EARLIEST, ANCHOR).tolist() == [False, True]


def test_given_archive_ending_on_the_last_day_when_checked_then_the_month_is_settled():
    assert partial_periods(_months("2026-09"), "Month", EARLIEST, date(2026, 9, 30)).tolist() == [False]


def test_given_a_quarter_with_one_partial_month_when_checked_then_the_quarter_is_partial():
    quarters = pd.DataFrame({"year": [2026, 2026, 2026], "quarter": ["Q1", "Q2", "Q3"]})
    assert partial_periods(quarters, "Quarter", EARLIEST, ANCHOR).tolist() == [True, False, True]


def test_given_years_when_checked_then_a_year_the_archive_clips_is_partial():
    years = pd.DataFrame({"year": [2025, 2026]})
    assert partial_periods(years, "Year", date(2025, 1, 1), ANCHOR).tolist() == [False, True]
