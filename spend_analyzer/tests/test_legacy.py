"""Imported sheet history: the coverage cutoff and the goals file."""
import json

import pandas as pd

from legacy import before_coverage, load_goals


def _ledger(*dates):
    return pd.DataFrame({"date": list(dates), "spend": [1.0] * len(dates)})


def test_given_rows_either_side_of_the_first_bank_date_when_cut_then_only_earlier_ones_remain():
    ledger = _ledger("2026-03-08", "2026-03-09", "2026-04-01")

    assert before_coverage(ledger, "2026-03-09")["date"].tolist() == ["2026-03-08"]


def test_given_a_timestamp_when_cut_then_only_its_date_part_counts():
    assert before_coverage(_ledger("2026-03-09"), "2026-03-09T14:00:00")["date"].tolist() == []


def test_given_empty_ledger_when_cut_then_empty():
    assert before_coverage(pd.DataFrame(), "2026-03-09").empty


def test_given_goals_file_when_loaded_then_year_keyed_floats(tmp_path):
    path = tmp_path / "g.json"
    path.write_text(json.dumps({"2024": {"Rent": 3965, "Bad": "x", "Flag": True}, "notayear": {"A": 1}}))

    assert load_goals(path) == {2024: {"Rent": 3965.0}}


def test_given_missing_or_broken_goals_file_when_loaded_then_empty(tmp_path):
    broken = tmp_path / "b.json"
    broken.write_text("{nope")
    wrong_shape = tmp_path / "w.json"
    wrong_shape.write_text("[1, 2]")

    assert load_goals(tmp_path / "absent.json") == {} == load_goals(broken) == load_goals(wrong_shape)
