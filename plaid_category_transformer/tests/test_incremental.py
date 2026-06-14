"""Incremental delta + the dedicated flagged-rows worklist.

Covers the behaviours the rest of the pipeline relies on: only NEW/CHANGED rows get
audited, UNCHANGED rows are carried forward untouched, rows gone upstream are pruned, a
pre-hash store is adopted without a disruptive re-audit, and the worklist CSV lists exactly
the rows still pending review (formula-injection guarded).
"""
import persister

from src.incremental import SOURCE_HASH_FIELD, classify, source_hash
from src.schema import FLAG_COLUMNS, NEW_COLUMNS
from src.transformer import run, write_flags_file


def _prior(rec: dict, **extra) -> dict:
    """A categorized-store record: the raw input plus its stamped source hash + any extras."""
    out = dict(rec)
    out.setdefault(SOURCE_HASH_FIELD, source_hash(rec))
    out.update(extra)
    return out


# ── source_hash ───────────────────────────────────────────────────────────────

def given_record_when_our_columns_added_then_hash_unchanged(make_record):
    # The hash covers only raw Plaid content, so our own provenance/review/bookkeeping
    # columns must never shift it (else every audited row would look "changed" next run).
    rec = make_record()
    base = source_hash(rec)
    decorated = dict(rec, **{c: "x" for c in NEW_COLUMNS}, **{SOURCE_HASH_FIELD: "deadbeef"})
    assert source_hash(decorated) == base


def given_record_when_plaid_field_changes_then_hash_changes(make_record):
    rec = make_record(amount=6.5)
    assert source_hash(rec) != source_hash(make_record(amount=99.0))
    assert source_hash(rec) != source_hash(make_record(merchant_name="Someone Else"))


def given_record_when_txn_owner_added_or_changed_then_hash_unchanged(make_record):
    # txn_owner is the collector's ownership stamp, not categorization input. Excluding
    # it means stamping existing history (the multi-user migration) keeps every stored
    # hash valid — i.e. it cannot trigger a mass LLM re-audit of the whole store.
    rec = make_record()
    base = source_hash(rec)
    assert source_hash(dict(rec, txn_owner="Alice")) == base
    assert source_hash(dict(rec, txn_owner="someone_else")) == base


# ── classify ──────────────────────────────────────────────────────────────────

def given_input_key_absent_from_prior_then_new_and_to_process(make_record):
    inp = {"a": make_record(transaction_id="a")}
    d = classify(inp, prior_store={})
    assert d.new == ["a"] and "a" in d.to_process
    assert d.changed == [] and d.carryover == {} and d.removed == []


def given_matching_hash_then_carried_forward_not_audited(make_record):
    rec = make_record(transaction_id="a")
    prior = {"a": _prior(rec, category_review_flag="1", category_review_detailed="X")}
    d = classify({"a": rec}, prior)
    assert d.to_process == {} and d.changed == [] and d.new == []
    # carried VERBATIM — a pending review flag survives across runs untouched.
    assert d.carryover["a"]["category_review_flag"] == "1"
    assert d.carryover["a"]["category_review_detailed"] == "X"


def given_stale_hash_then_changed_and_reaudited(make_record):
    rec = make_record(transaction_id="a", amount=10.0)
    prior = {"a": _prior(make_record(transaction_id="a", amount=999.0))}  # different content
    d = classify({"a": rec}, prior)
    assert d.changed == ["a"] and d.to_process["a"]["amount"] == 10.0
    assert d.carryover == {}


def given_prior_key_absent_from_input_then_removed(make_record):
    prior = {"gone": _prior(make_record(transaction_id="gone")),
             "stay": _prior(make_record(transaction_id="stay"))}
    d = classify({"stay": make_record(transaction_id="stay")}, prior)
    assert d.removed == ["gone"]
    assert "stay" in d.carryover and "gone" not in d.carryover


def given_prior_without_hash_then_adopted_not_reaudited(make_record):
    # A store written before content-hashing exists: adopt it as the baseline (preserving any
    # prior human decision) instead of forcing a full re-audit.
    rec = make_record(transaction_id="a")
    prior = {"a": dict(rec, category_update_step="review")}  # no SOURCE_HASH_FIELD
    d = classify({"a": rec}, prior)
    assert d.to_process == {} and d.changed == []
    assert d.carryover["a"]["category_update_step"] == "review"
    assert d.carryover["a"][SOURCE_HASH_FIELD] == source_hash(rec)


def given_full_then_everything_reaudited_but_removals_still_found(make_record):
    rec = make_record(transaction_id="a")
    prior = {"a": _prior(rec), "gone": _prior(make_record(transaction_id="gone"))}
    d = classify({"a": rec}, prior, full=True)
    assert "a" in d.to_process and d.carryover == {}  # carried-forward path bypassed
    assert d.removed == ["gone"]


# ── flagged-rows worklist ───────────────────────────────────────────────────────

def given_mixed_store_when_worklist_written_then_only_flagged_rows(tmp_path, make_record):
    store = {
        "flagged": make_record(transaction_id="flagged", category_review_flag="1",
                               category_review_primary="FOOD_AND_DRINK",
                               category_review_detailed="FOOD_AND_DRINK_COFFEE",
                               category_review_source="llm"),
        "clean": make_record(transaction_id="clean", category_review_flag=""),
    }
    path = tmp_path / "flags.csv"
    n = write_flags_file(str(path), store)

    assert n == 1
    lines = path.read_text().splitlines()
    assert lines[0].split(",") == FLAG_COLUMNS         # stable worklist header
    assert len(lines) == 2                              # header + the one flagged row
    assert "flagged" in lines[1] and "clean" not in path.read_text()


def given_no_flags_when_worklist_written_then_header_only(tmp_path, make_record):
    path = tmp_path / "flags.csv"
    assert write_flags_file(str(path), {"a": make_record(category_review_flag="")}) == 0
    assert path.read_text().splitlines() == [",".join(FLAG_COLUMNS)]


def given_formula_like_merchant_when_worklist_written_then_guarded(tmp_path, make_record):
    store = {"a": make_record(transaction_id="a", merchant_name="=cmd()",
                              category_review_flag="1")}
    path = tmp_path / "flags.csv"
    write_flags_file(str(path), store)
    # persister's csv_safe guard neutralises the leading '=' (formula injection) by
    # prefixing the cell with a single quote, so it can't execute when opened in a sheet.
    assert "'=cmd()" in path.read_text()


# ── run(): incremental orchestration end-to-end (no LLM, no Drive) ──────────────

def given_two_runs_when_input_changes_then_delta_applied(tmp_path, make_record):
    inp = tmp_path / "input.jsonl"
    out_jsonl = tmp_path / "out.jsonl"
    out_csv = tmp_path / "out.csv"
    flags_csv = tmp_path / "flags.csv"

    def _run():
        run(input_path=str(inp), out_jsonl=str(out_jsonl), out_csv=str(out_csv),
            flags_csv=str(flags_csv), log_path=str(tmp_path / "log.jsonl"),
            levels={"LOW", "MEDIUM", "HIGH", "VERY_HIGH", "UNKNOWN"},
            memory_path=None, do_drive=False, no_llm=True, debug=False)

    low = {"primary": "GENERAL_MERCHANDISE",
           "detailed": "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE",
           "confidence_level": "LOW"}
    a = make_record(transaction_id="A", amount=5.0, personal_finance_category=dict(low))
    b = make_record(transaction_id="B", name="COT*FLT", original_description="COT*FLT",
                    merchant_name="COT", merchant_entity_id=None, website=None,
                    personal_finance_category=dict(low))  # mechanical 'auto' correction
    # C is PENDING: settled pendings are the only rows the prune gate lets vanish.
    c = make_record(transaction_id="C", pending=True,
                    personal_finance_category=dict(low))
    persister.save_jsonl(str(inp), {"A": a, "B": b, "C": c})

    _run()
    s1 = persister.load_jsonl(str(out_jsonl))
    assert set(s1) == {"A", "B", "C"}
    assert s1["B"]["personal_finance_category"]["detailed"] == "TRAVEL_FLIGHTS"  # auto-applied
    assert all(SOURCE_HASH_FIELD in r for r in s1.values())                      # hashes stamped
    assert flags_csv.read_text().splitlines()[0].split(",") == FLAG_COLUMNS

    # Mutate upstream: drop C (removed), change A (re-audit), add D (new); B untouched.
    a2 = make_record(transaction_id="A", amount=42.0, personal_finance_category=dict(low))
    d = make_record(transaction_id="D", personal_finance_category=dict(low))
    persister.save_jsonl(str(inp), {"A": a2, "B": b, "D": d})

    _run()
    s2 = persister.load_jsonl(str(out_jsonl))
    assert set(s2) == {"A", "B", "D"}                       # C pruned, D added
    assert s2["A"]["amount"] == 42.0                        # changed row re-audited
    assert s2["B"]["personal_finance_category"]["detailed"] == "TRAVEL_FLIGHTS"  # carried fwd
