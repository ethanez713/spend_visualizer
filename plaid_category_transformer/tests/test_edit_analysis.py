"""Edit-log mining: promotion/demotion candidates and the LLM scorecard.

Pure-data tests over ``mine``/``report_markdown`` with intents built the same way the
UI/CLI build them (via ``build_intent``), so the analysis is tested against the real
log format.
"""
from src.edit_analysis import mine, report_markdown
from src.manual import build_intent, build_revoke
from src.schema import set_provenance, set_review_flag

COFFEE = ("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE")
FLIGHTS = ("TRAVEL", "TRAVEL_FLIGHTS")


def _edit(record, cat=COFFEE, scope="transaction"):
    return build_intent(scope=scope, primary=cat[0], detailed=cat[1], record=record)


def given_repeated_consistent_edits_when_mined_then_merchant_promoted(make_record):
    a = _edit(make_record(transaction_id="t1"))
    b = _edit(make_record(transaction_id="t2"))
    m = mine([a, b])
    assert [c["merchant"] for c in m["promote"]] == ["blue bottle coffee"]
    assert m["promote"][0]["count"] == 2
    assert m["conflicted"] == []
    # the report carries a paste-ready config rule for it
    assert '("bluebottlecoffee.com", ("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE")' \
        in report_markdown([a, b])


def given_conflicting_edits_when_mined_then_flagged_not_promoted(make_record):
    a = _edit(make_record(transaction_id="t1"), cat=COFFEE)
    b = _edit(make_record(transaction_id="t2"), cat=FLIGHTS)
    m = mine([a, b])
    assert m["promote"] == []
    assert [c["merchant"] for c in m["conflicted"]] == ["blue bottle coffee"]


def given_a_revoked_edit_when_mined_then_not_counted_as_evidence(make_record):
    a = _edit(make_record(transaction_id="t1"))
    b = _edit(make_record(transaction_id="t2"))
    m = mine([a, b, build_revoke(b["id"])])
    assert m["n_intents"] == 1
    assert m["n_revoked"] == 1
    assert m["promote"] == []          # one edit left — below min_count


def given_an_overridden_rule_when_mined_then_demotion_counted(make_record):
    rec = make_record()
    set_provenance(rec, *FLIGHTS, "mechanical", "keyword:lyft", "MEDIUM")
    m = mine([_edit(rec)])
    assert m["demote"] == {"mechanical: keyword:lyft": 1}


def given_pending_llm_flags_when_mined_then_scorecard_classifies(make_record):
    right = make_record(transaction_id="t1")
    set_review_flag(right, *COFFEE, "looks like a cafe", "HIGH", "llm")
    wrong = make_record(transaction_id="t2")
    set_review_flag(wrong, *FLIGHTS, "looks like travel", "LOW", "llm")
    missed = make_record(transaction_id="t3")
    m = mine([_edit(right), _edit(wrong), _edit(missed)])
    assert m["scorecard"] == {"flag_right": 1, "flag_wrong": 1, "no_flag": 1}
