"""Review adjudication: the pure accept/reject/re-pick actions and the interactive loop."""
import pytest

from src.review import (
    accept_flag,
    flagged_rows,
    reject_flag,
    repick_flag,
    run_review,
)
from src.rules import MerchantMemory
from src.schema import ensure_new_columns, set_review_flag


def _flagged(make_record, suggested=("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE"),
             source="llm", **over):
    rec = make_record(**over)
    ensure_new_columns(rec)
    set_review_flag(rec, suggested[0], suggested[1], "looks like a cafe", "HIGH", source)
    return rec


# ── pure actions ──────────────────────────────────────────────────────────────

def given_a_flag_when_accepted_then_applied_and_memory_taught(make_record):
    mem = MerchantMemory(path=None)
    rec = _flagged(make_record, merchant_entity_id="ent_x")
    assert accept_flag(rec, mem) is True

    pfc = rec["personal_finance_category"]
    assert (pfc["primary"], pfc["detailed"]) == ("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE")
    assert pfc["confidence_level"] == "CORRECTED"
    assert rec["category_update_step"] == "review"
    assert rec["category_review_flag"] == ""          # flag cleared
    # taught to memory → a trusted auto hit next run
    hit = mem.lookup(rec)
    assert hit is not None and hit.trust == "auto"
    assert (hit.primary, hit.detailed) == ("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE")


def given_a_flag_when_rejected_then_category_kept_and_flag_cleared(make_record):
    rec = _flagged(make_record)
    before = dict(rec["personal_finance_category"])
    reject_flag(rec)
    assert rec["personal_finance_category"] == before  # unchanged
    assert rec["category_review_flag"] == ""
    assert rec["category_update_step"] == ""


def given_a_flag_when_repicked_then_chosen_category_applied(make_record):
    mem = MerchantMemory(path=None)
    rec = _flagged(make_record, merchant_entity_id="ent_y")
    assert repick_flag(rec, "TRAVEL", "TRAVEL_FLIGHTS", mem) is True
    assert rec["personal_finance_category"]["detailed"] == "TRAVEL_FLIGHTS"
    assert rec["category_review_flag"] == ""
    assert mem.lookup(rec).detailed == "TRAVEL_FLIGHTS"


def given_an_invalid_repick_when_applied_then_raises(make_record):
    rec = _flagged(make_record)
    with pytest.raises(ValueError):
        repick_flag(rec, "TRAVEL", "NOT_A_REAL_DETAILED")


def given_a_store_when_listed_then_only_flagged_rows_returned(make_record):
    flagged = _flagged(make_record, transaction_id="a")
    clean = make_record(transaction_id="b")
    ensure_new_columns(clean)
    store = {"a": flagged, "b": clean}
    assert [tid for tid, _ in flagged_rows(store)] == ["a"]


# ── interactive loop (TTY forced on; input injected) ──────────────────────────

@pytest.fixture
def _tty(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)


def _driver(answers):
    it = iter(answers)
    return lambda _prompt="": next(it)


def given_flags_when_loop_accepts_and_rejects_then_counts_and_effects(make_record, _tty):
    store = {"a": _flagged(make_record, transaction_id="a"),
             "b": _flagged(make_record, transaction_id="b")}
    summary = run_review(store, MerchantMemory(path=None),
                         input_fn=_driver(["a", "r"]), out=lambda *a, **k: None)
    assert summary["accepted"] == 1 and summary["rejected"] == 1
    assert store["a"]["personal_finance_category"]["detailed"] == "FOOD_AND_DRINK_COFFEE"
    assert store["b"]["category_review_flag"] == ""  # rejected → flag cleared, category kept


def given_flags_when_loop_quits_then_remaining_left_flagged(make_record, _tty):
    store = {"a": _flagged(make_record, transaction_id="a"),
             "b": _flagged(make_record, transaction_id="b")}
    summary = run_review(store, None, input_fn=_driver(["q"]), out=lambda *a, **k: None)
    assert summary["accepted"] == 0
    assert summary["skipped"] == 2
    assert store["a"]["category_review_flag"] == "1"  # still pending


def given_no_flags_when_review_then_noop(make_record):
    store = {"b": make_record(transaction_id="b")}
    ensure_new_columns(store["b"])
    summary = run_review(store, None, input_fn=_driver([]), out=lambda *a, **k: None)
    assert summary == {"accepted": 0, "rejected": 0, "repicked": 0, "skipped": 0, "log": []}
