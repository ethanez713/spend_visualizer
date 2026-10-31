"""Which merchants count as recurring, on synthetic charge series."""
from datetime import date, timedelta

import pandas as pd

from enrich import _detect_recurrence

UTILITY, RESTAURANT, PHONE = ("RENT_AND_UTILITIES_GAS_AND_ELECTRICITY", "FOOD_AND_DRINK_RESTAURANT",
                              "RENT_AND_UTILITIES_TELEPHONE")


def _detect(amounts, atom=UTILITY, days=None):
    days = days or [date(2026, 1, 5) + timedelta(days=30 * i) for i in range(len(amounts))]
    df = pd.DataFrame({"merchant_id": "m", "flow": "spend", "refund": False, "atom": atom,
                       "date_resolved": [d.isoformat() for d in days], "abs_amount": amounts})
    row = _detect_recurrence(df).iloc[0]
    return row["recurrence"], row["recurrence_amount"]


def test_given_a_monthly_utility_with_seasonal_amounts_then_recurring_variable():
    assert _detect([24.0, 163.0, 88.0, 41.0, 120.0, 60.0]) == ("recurring", "variable")


def test_given_a_monthly_bill_with_a_4x_first_charge_then_recurring_fixed():
    assert _detect([134.0, 35.0, 35.0, 35.0, 35.0], atom=PHONE) == ("recurring", "fixed")


def test_given_one_extra_payment_splitting_a_month_then_still_recurring():
    start = date(2026, 1, 5)
    days = [start + timedelta(days=d) for d in (0, 30, 60, 66, 90, 120, 150)]
    assert _detect([80.0, 200.0, 90.0, 40.0, 150.0, 60.0, 110.0], days=days)[0] == "recurring"


def test_given_a_restaurant_visited_monthly_with_varying_amounts_then_one_off():
    assert _detect([24.0, 163.0, 88.0, 41.0, 120.0, 60.0], atom=RESTAURANT) == ("one-off", "")


def test_given_a_phone_bill_within_5_percent_then_recurring_fixed():
    assert _detect([50.0, 51.0, 49.0, 52.0, 50.0], atom=PHONE) == ("recurring", "fixed")


def test_given_two_charges_then_one_off():
    assert _detect([50.0, 50.0], atom=PHONE) == ("one-off", "")


def test_given_several_charges_on_one_day_then_they_count_as_one_charge_day():
    one_day = [date(2026, 1, 5)] * 2 + [date(2026, 2, 4)]
    assert _detect([25.0, 25.0, 50.0], atom=PHONE, days=one_day) == ("one-off", "")


UNEVEN_MONTHLY = [date(2026, 1, 5) + timedelta(days=d) for d in (0, 39, 45, 94, 123, 157, 186)]


def test_given_a_bill_paid_on_uneven_dates_then_recurring_variable():
    assert _detect([180.0, 113.0, 574.0, 68.0, 126.0, 219.0, 181.0],
                   days=UNEVEN_MONTHLY) == ("recurring", "variable")


def test_given_fixed_charges_on_the_same_uneven_dates_but_not_a_bill_then_one_off():
    assert _detect([50.0] * 7, atom=RESTAURANT, days=UNEVEN_MONTHLY) == ("one-off", "")


def test_given_irregular_gaps_then_one_off():
    start = date(2026, 1, 5)
    days = [start + timedelta(days=d) for d in (0, 3, 40, 47, 100, 104)]
    assert _detect([50.0] * 6, atom=PHONE, days=days) == ("one-off", "")
