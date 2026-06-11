"""Unit tests for the INGEST pipeline ops (PLAN.md §4)."""
from enrich import enrich
from ingest.dedupe import dedupe_by_id, drop_settled_pending
from ingest.normalize import normalize_one
from taxonomy import load_taxonomy


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


def test_dedupe_keeps_latest():
    rows = [_raw("a", amount=1.0), _raw("b"), _raw("a", amount=2.0)]
    out, dropped = dedupe_by_id(rows)
    assert dropped == 1
    amounts = {r["transaction_id"]: r["amount"] for r in out}
    assert amounts["a"] == 2.0  # last wins


def test_drop_settled_pending():
    rows = [
        _raw("pending1", pending=True),
        _raw("posted1", pending=False, pending_transaction_id="pending1"),
        _raw("pending2", pending=True),  # still outstanding -> kept
    ]
    out, dropped = drop_settled_pending(rows)
    ids = {r["transaction_id"] for r in out}
    assert dropped == 1
    assert "pending1" not in ids
    assert "pending2" in ids and "posted1" in ids


def test_normalize_direction_and_sign():
    out = normalize_one(_raw("x", amount=25.0))
    assert out.direction == "out" and out.amount == 25.0
    inc = normalize_one(_raw("y", amount=-100.0))
    assert inc.direction == "in"
    assert inc.pfc_detailed == "FOOD_AND_DRINK_GROCERIES"
    assert inc.raw is not None  # raw retained losslessly


def test_normalize_carries_owner_stamp():
    assert normalize_one(_raw("x", txn_owner="Alice")).owner == "Alice"
    assert normalize_one(_raw("y")).owner is None  # pre-migration records


def test_enrich_person_prefers_record_owner_over_accounts_yaml():
    # The collector-written txn_owner stamp is authoritative; accounts.yaml `person`
    # only fills in for un-stamped history; "Unknown" is the last resort.
    taxonomy = load_taxonomy()
    txns = [
        normalize_one(_raw("stamped", txn_owner="Alice")),
        normalize_one(_raw("unstamped")),
        normalize_one(_raw("nowhere", account_id="acct_unmapped")),
    ]
    accounts = {"acct": {"person": "FromYaml"}}

    df = enrich(txns, taxonomy, accounts)

    person = df.set_index("transaction_id")["person"]
    assert person["stamped"] == "Alice"      # record stamp wins
    assert person["unstamped"] == "FromYaml"  # accounts.yaml fallback
    assert person["nowhere"] == "Unknown"     # last resort
