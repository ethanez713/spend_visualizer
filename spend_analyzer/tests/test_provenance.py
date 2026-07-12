"""Provenance carry-through: the transformer's category_update_* / review columns
must survive ingest → enrich (the 'why this category?' features read them from
the cube), and the badge helper must format every step."""
from enrich import enrich
from ingest.normalize import normalize_one
from taxonomy import load_taxonomy
from viz import provenance_why


def _raw(tid, **kw):
    base = {
        "transaction_id": tid,
        "account_id": "acct",
        "amount": 10.0,
        "date": "2026-05-01",
        "iso_currency_code": "USD",
        "personal_finance_category": {
            "primary": "FOOD_AND_DRINK",
            "detailed": "FOOD_AND_DRINK_GROCERIES",
            "confidence_level": "HIGH",
        },
    }
    base.update(kw)
    return base


def _enrich_one(raw):
    df = enrich([normalize_one(raw)], load_taxonomy(), {})
    return df.iloc[0]


def test_enrich_carries_applied_provenance():
    row = _enrich_one(_raw(
        "t1",
        category_update_step="mechanical",
        category_update_reason="keyword:spotify",
        category_update_confidence="MEDIUM",
        original_pf_category_primary="GENERAL_SERVICES",
        original_pf_category_detailed="GENERAL_SERVICES_OTHER_GENERAL_SERVICES",
    ))
    assert row["category_update_step"] == "mechanical"
    assert row["category_update_reason"] == "keyword:spotify"
    assert row["category_update_confidence"] == "MEDIUM"
    assert row["original_pf_primary"] == "GENERAL_SERVICES"
    assert row["original_pf_detailed"] == "GENERAL_SERVICES_OTHER_GENERAL_SERVICES"
    assert bool(row["review_pending"]) is False


def test_enrich_carries_pending_review_flag():
    row = _enrich_one(_raw(
        "t2",
        category_review_flag="1",
        category_review_primary="ENTERTAINMENT",
        category_review_detailed="ENTERTAINMENT_MUSIC_AND_AUDIO",
        category_review_reason="keyword:spotify",
        category_review_source="mechanical",
    ))
    assert bool(row["review_pending"]) is True
    assert row["review_primary"] == "ENTERTAINMENT"
    assert row["review_reason"] == "keyword:spotify"
    assert row["review_source"] == "mechanical"


def test_enrich_blank_provenance_for_uncorrected_feed():
    # Pointing the analyzer at the raw (pre-transformer) store must not crash
    # the provenance features — everything reads as "Plaid default".
    row = _enrich_one(_raw("t3"))
    assert row["category_update_step"] == ""
    assert row["category_update_reason"] == ""
    assert bool(row["review_pending"]) is False


def test_provenance_why_covers_every_step():
    assert provenance_why("mechanical", "keyword:spotify") == "🔧 rule · keyword:spotify"
    assert provenance_why("llm", "looks like music") == "🤖 LLM · looks like music"
    assert provenance_why("review", "manual re-pick") == "👁 review · manual re-pick"
    assert provenance_why("manual", "i-abc123") == "✏️ manual · i-abc123"
    assert provenance_why("", "") == ""                      # Plaid default: no badge
    assert provenance_why("", "", True) == "⏳ suggestion"   # pending flag only
    assert provenance_why("mechanical", "pos:tst", True) == (
        "🔧 rule · pos:tst  ⏳ suggestion")
