"""Unit tests for the INGEST pipeline ops (PLAN.md §4)."""
from enrich import enrich
from ingest.dedupe import dedupe_by_id, drop_removed_pending, drop_settled_pending
from ingest.live import load_live_ids
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


def test_accounts_generator_reads_identity_from_the_collector_csv(tmp_path):
    from tools.gen_accounts import collector_identity
    csv_path = tmp_path / "transactions.csv"
    csv_path.write_text(
        "institution,account_id,account_name,account_mask,account_type,account_subtype\n"
        "Big Bank,acc1,Everyday Checking,1234,depository,checking\n"
        "Big Bank,acc1,Everyday Checking,1234,depository,checking\n"
        "Card Co,acc2,'=Rewards,,credit,credit card\n", encoding="utf-8")
    ident = collector_identity(csv_path)
    assert ident["acc1"] == {"name": "Everyday Checking ··1234", "type": "depository",
                             "subtype": "checking", "institution": "Big Bank"}
    assert ident["acc2"]["name"] == "=Rewards" and ident["acc2"]["type"] == "credit"
    assert collector_identity(tmp_path / "absent.csv") == {}


def test_given_pending_row_plaid_no_longer_reports_when_filtered_then_dropped():
    rows = [_raw("gone", pending=True), _raw("alive", pending=True), _raw("posted")]

    out, dropped = drop_removed_pending(rows, {"alive", "posted"})

    assert {r["transaction_id"] for r in out} == {"alive", "posted"} and dropped == 1


def test_given_posted_row_absent_from_the_mirror_when_filtered_then_kept():
    rows = [_raw("posted", pending=False)]

    assert drop_removed_pending(rows, set()) == (rows, 0)


def test_given_no_mirror_when_filtered_then_nothing_is_dropped():
    rows = [_raw("p", pending=True)]

    assert drop_removed_pending(rows, None) == (rows, 0)


def test_given_mirror_csv_when_loaded_then_its_transaction_ids(tmp_path):
    mirror = tmp_path / "t.csv"
    mirror.write_text("institution,transaction_id,amount\nBank,a1,5\nBank,,6\nBank,b2,7\n")

    assert load_live_ids(mirror) == {"a1", "b2"}


def test_given_missing_or_columnless_mirror_when_loaded_then_none(tmp_path):
    columnless = tmp_path / "c.csv"
    columnless.write_text("a,b\n1,2\n")

    assert load_live_ids(tmp_path / "absent.csv") is None
    assert load_live_ids(columnless) is None
