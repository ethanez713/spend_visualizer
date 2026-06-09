"""Row selection: by default EVERY confidence level is audited (incl. HIGH/VERY_HIGH)."""
import pytest

from src.schema import PROCESS_CONFIDENCE, confidence_of, should_process


@pytest.mark.parametrize("level", ["LOW", "MEDIUM", "HIGH", "VERY_HIGH", "UNKNOWN"])
def given_any_confidence_when_checked_then_audited_by_default(make_record, level):
    rec = make_record(personal_finance_category={"primary": "X", "detailed": "Y",
                                                  "confidence_level": level})
    assert should_process(rec)


@pytest.mark.parametrize("level", ["HIGH", "VERY_HIGH"])
def given_trusted_confidence_when_levels_narrowed_then_not_processed(make_record, level):
    rec = make_record(personal_finance_category={"primary": "X", "detailed": "Y",
                                                  "confidence_level": level})
    assert not should_process(rec, {"LOW", "MEDIUM", "UNKNOWN"})


def given_lowercase_confidence_when_checked_then_normalized(make_record):
    rec = make_record(personal_finance_category={"primary": "X", "detailed": "Y",
                                                  "confidence_level": "low"})
    assert should_process(rec)
    assert confidence_of(rec) == "LOW"


def given_missing_confidence_when_default_levels_then_processed_as_unknown(make_record):
    rec = make_record(personal_finance_category={"primary": "X", "detailed": "Y"})
    assert should_process(rec)  # UNKNOWN in default levels


def given_missing_pfc_when_default_levels_then_processed(make_record):
    rec = make_record(personal_finance_category=None)
    assert should_process(rec)


def given_custom_levels_excluding_unknown_when_missing_then_not_processed(make_record):
    rec = make_record(personal_finance_category={"primary": "X", "detailed": "Y"})
    assert not should_process(rec, {"LOW", "MEDIUM"})


def given_default_levels_constant_then_audits_all_plaid_levels():
    assert {"LOW", "MEDIUM", "HIGH", "VERY_HIGH", "UNKNOWN"} <= PROCESS_CONFIDENCE
