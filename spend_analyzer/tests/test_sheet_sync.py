"""Sheet sync: Sheet edits become budget intents, app edits become an outbox."""
import json
from pathlib import Path

import pytest

import manual_edits
from tools import sheet_sync as tool
from tools.sheet_sync import EXCLUDED

KNOWN = ["Groceries", "Dining Out", "Travel"]
LEDGER = {
    "Source": "Alex", "Date": "07/06/2026", "Description": "Market",
    "Category": "Groceries", "Debit": "$10.00", "Credit": "",
    "Debit Less Credit": "$10.00", "Account": "Checking", "Note": "",
}


def _ledger(**overrides):
    return {**LEDGER, **overrides}


def _row(txn="t1", pending=False, replaces=None, **ledger):
    return {"transaction_id": txn, "pending": pending, "replaces": replaces,
            "ledger": _ledger(**ledger)}


def _manifest(file_id="f1", title="July", rows=None, silent=None, txn="t1", **ledger):
    if rows is None:
        rows = [_row(txn, **ledger)]
    return {"file_id": file_id, "title": title, "url": f"https://example.test/{file_id}",
            "rows": rows, "silent": silent or []}


def _edit(before, after, types, txn="t1", matched=True, ambiguous=False,
          file_id="f1", title="July"):
    upstream = {"matched": matched, "ambiguous": ambiguous}
    if txn is not None and matched:
        upstream["transaction_id"] = txn
    return {"sheet": {"file_id": file_id, "title": title, "url": "u"},
            "edit_types": types, "before": before, "after": after,
            "upstream": upstream, "matched_by": "date+amount" if matched else None}


def _raw(txn="t1", pending=False, **extra):
    return {txn: {"transaction_id": txn, "pending": pending, "name": "Market",
                  "merchant_name": "Market", **extra}}


def _plan(edits=(), manifest=None, base=(), raw=None, overrides=None, known=None):
    if manifest is None:
        manifest = [_manifest()]
    if raw is None:
        raw = _raw()
    return tool.plan(list(edits), list(manifest), list(base), raw, overrides or {},
                     KNOWN if known is None else known)


def _base(plan):
    return {(row["file_id"], row["row_id"], row["field"]): row["value"] for row in plan.base}


def _category_edit(category="Dining Out", before="Groceries", txn="t1", **kw):
    return _edit(_ledger(Category=before), _ledger(Category=category),
                 ["category_change"], txn=txn, **kw)


def test_category_change_pulls_one_transaction_intent():
    result = _plan([_category_edit()])

    assert len(result.intents) == 1
    intent = result.intents[0]
    assert intent["action"] == "budget_edit" and intent["source"] == "sheet"
    assert intent["scope"] == "transaction"
    assert intent["match"] == {"transaction_id": "t1"}
    assert intent["set"] == {"budget_category": "Dining Out"} and intent["note"] == ""
    assert intent["sheet"] == {"file_id": "f1", "title": "July"}
    assert _base(result) == {("f1", "t1", "category"): "Dining Out"}
    assert result.outbox == [] and result.conflicts == []
    assert result.counts["pulled"] == 1


def test_category_change_does_not_also_write_a_note():
    result = _plan([_category_edit()])

    assert [intent["note"] for intent in result.intents] == [""]
    assert "note" not in {field for _, _, field in _base(result)}


@pytest.mark.parametrize("after,expected", [
    ({"Note": "split the bill"}, "split the bill"),
    ({"Note": "PENDING", "Annotation": "cash"}, "cash"),
    ({"Note": "⚠ skipped", "Annotation": "cash"}, "cash"),
    ({"Note": "⚠ skipped", "Description": "Farmers Market"}, "Sheet description: Farmers Market"),
    ({"Note": "hello", "Annotation": "cash", "Description": "Farmers Market"}, "hello"),
    ({"Description": "Farmers Market"}, "Sheet description: Farmers Market"),
])
def test_note_annotation_and_description_map_in_that_order(after, expected):
    before = _ledger()
    edited = _ledger(**after)
    kinds = []
    if edited.get("Note", "") != before["Note"]:
        kinds.append("note_change")
    if "Annotation" in after:
        kinds.append("annotation_change")
    if edited["Description"] != before["Description"]:
        kinds.append("description_change")
    result = _plan([_edit(before, edited, kinds)])

    notes = [intent for intent in result.intents if intent["set"] == {}]
    assert [intent["note"] for intent in notes] == [expected]
    assert _base(result)[("f1", "t1", "note")] == expected
    assert not any(intent["set"].get("budget_category") for intent in result.intents)


def test_pending_note_alone_is_not_a_household_note():
    edit = _edit(_ledger(Note="PENDING"), _ledger(Note="PENDING"), ["note_change"])

    assert _plan([edit]).intents == []


def test_warning_mark_on_a_household_note_does_not_drop_the_category():
    edit = _edit(_ledger(), _ledger(Category="Dining Out", Note="⚠ check later"),
                 ["category_change", "note_change"])

    result = _plan([edit])

    assert [intent["set"].get("budget_category") for intent in result.intents] == ["Dining Out"]
    assert result.counts["skipped_warning"] == 0


def test_deleted_posted_row_pulls_an_exclusion():
    edit = _edit(_ledger(), None, ["row_deleted"])

    result = _plan([edit])

    assert result.intents[0]["set"] == {"exclude": True}
    assert result.intents[0]["match"] == {"transaction_id": "t1"}
    assert _base(result)[("f1", "t1", "category")] == EXCLUDED


@pytest.mark.parametrize("manifest_pending,raw", [
    (True, _raw(pending=False)),
    (False, _raw(pending=True)),
    (False, {}),
])
def test_deletion_of_a_pending_or_missing_row_is_not_imported(manifest_pending, raw):
    edit = _edit(_ledger(), None, ["row_deleted"])
    manifest = [_manifest(rows=[_row(pending=manifest_pending)])]

    result = _plan([edit], manifest=manifest, raw=raw)

    assert result.intents == []
    assert result.counts["skipped_pending_delete"] == 1
    assert _base(result) == {}


def test_warning_rows_are_not_imported():
    warning = _ledger(Note="⚠ UPDATED FROM LAST EXECUTION — delete the old row")
    edited = _edit(warning, {**warning, "Category": "Dining Out"}, ["category_change"])
    added = _edit(None, {**warning, "Description": "ignore me", "Debit Less Credit": "$5.00"},
                  ["row_added"])
    manifest = [_manifest(rows=[_row()])]

    result = _plan([edited, added], manifest=manifest)

    assert result.intents == []
    assert result.counts["skipped_warning"] == 2


def test_unmatched_and_ambiguous_edits_are_not_imported():
    unmatched = _category_edit(matched=False)
    ambiguous = _category_edit(ambiguous=True)

    missed = _plan([unmatched])
    unsure = _plan([ambiguous])

    assert missed.intents == [] and missed.counts["skipped_unmatched"] == 1
    assert unsure.intents == [] and unsure.counts["skipped_ambiguous"] == 1


def test_unknown_category_is_reported_and_not_imported():
    result = _plan([_category_edit("Spaceships")])

    assert result.intents == []
    assert result.conflicts == [{
        "kind": "unknown_category", "file_id": "f1", "title": "July",
        "row_id": "t1", "field": "category", "value": "Spaceships"}]
    assert result.counts["skipped_unknown_category"] == 1
    assert _base(result) == {}


def test_injected_vocabulary_decides_what_is_known():
    refused = _plan([_category_edit("Dining Out")], known=[])
    accepted = _plan([_category_edit("Dining Out")], known=["Dining Out"])

    assert refused.intents == [] and refused.conflicts[0]["kind"] == "unknown_category"
    assert accepted.intents[0]["set"]["budget_category"] == "Dining Out"


def test_one_row_with_different_values_in_two_sheets_conflicts():
    sheets = [
        _manifest(file_id="f1", title="July", txn="t1"),
        _manifest(file_id="f2", title="July copy", txn="t1"),
    ]
    edits = [
        _category_edit("Dining Out", file_id="f1", title="July"),
        _category_edit("Travel", file_id="f2", title="July copy"),
    ]

    result = _plan(edits, manifest=sheets)

    assert result.intents == [] and _base(result) == {}
    assert result.conflicts == [{
        "kind": "cross_sheet", "row_id": "t1", "field": "category",
        "values": [
            {"file_id": "f1", "title": "July", "row_id": "t1", "value": "Dining Out"},
            {"file_id": "f2", "title": "July copy", "row_id": "t1", "value": "Travel"},
        ]}]
    assert result.counts["skipped_cross_sheet"] == 1


def test_sheets_that_agree_pull_one_intent_and_advance_both_bases():
    sheets = [
        _manifest(file_id="f1", title="July"),
        _manifest(file_id="f2", title="July copy"),
    ]
    edits = [
        _category_edit(file_id="f1"),
        _category_edit(file_id="f2", title="July copy"),
    ]

    result = _plan(edits, manifest=sheets)

    assert len(result.intents) == 1
    assert _base(result) == {("f1", "t1", "category"): "Dining Out",
                            ("f2", "t1", "category"): "Dining Out"}


def test_deletion_and_recategory_of_the_same_row_conflict():
    sheets = [_manifest(file_id="f1"), _manifest(file_id="f2", title="July copy")]
    edits = [
        _edit(_ledger(), None, ["row_deleted"], file_id="f1"),
        _category_edit(file_id="f2", title="July copy"),
    ]

    result = _plan(edits, manifest=sheets)

    assert result.intents == []
    assert {value["value"] for value in result.conflicts[0]["values"]} == {EXCLUDED, "Dining Out"}


def test_app_change_pushes_an_outbox_line_and_does_not_pull():
    overrides = {"t1": {"category": "Travel", "edit": "e1", "note": "trip"}}

    result = _plan(overrides=overrides)

    by_action = {line["action"]: line for line in result.outbox}
    assert set(by_action) == {"set_category", "set_note"}
    assert by_action["set_category"]["value"] == "Travel"
    assert by_action["set_note"]["value"] == "trip"
    assert by_action["set_category"]["ledger"] == LEDGER
    assert by_action["set_category"]["row_id"] == "t1"
    assert result.intents == []
    assert result.base == []


def test_note_only_is_not_an_exclusion():
    overrides = {"t1": {"category": None, "edit": "", "note": "trip"}}

    result = _plan(overrides=overrides)

    assert [line["action"] for line in result.outbox] == ["set_note"]


def test_exclusion_pushes_delete_row_without_a_value():
    overrides = {"t1": {"category": None, "edit": "e1", "note": ""}}

    result = _plan(overrides=overrides)

    assert result.outbox == [{
        "action": "delete_row", "file_id": "f1", "title": "July", "row_id": "t1",
        "ledger": LEDGER}]
    assert "value" not in result.outbox[0]


def test_computed_category_is_not_an_app_opinion():
    raw = _raw(budget_cat="Travel", category="Travel")

    result = _plan(raw=raw)

    assert result.intents == [] and result.outbox == [] and result.base == []


def test_both_sides_changing_to_the_same_value_advances_base_without_an_intent():
    overrides = {"t1": {"category": "Dining Out", "edit": "e1", "note": ""}}

    result = _plan([_category_edit()], overrides=overrides)

    assert result.intents == [] and result.outbox == [] and result.conflicts == []
    assert _base(result) == {("f1", "t1", "category"): "Dining Out"}
    assert result.counts["agreed"] == 1


def test_both_sides_changing_to_different_values_conflict_and_keep_base():
    overrides = {"t1": {"category": "Travel", "edit": "e1", "note": ""}}
    existing = [{"file_id": "f1", "row_id": "t1", "field": "category", "value": "Groceries"}]

    result = _plan([_category_edit()], overrides=overrides, base=existing)

    assert result.intents == [] and result.outbox == []
    assert result.conflicts == [{
        "kind": "value_conflict", "file_id": "f1", "title": "July", "row_id": "t1",
        "field": "category", "base": "Groceries", "sheet": "Dining Out", "app": "Travel"}]
    assert _base(result) == {("f1", "t1", "category"): "Groceries"}


def test_amount_only_edit_does_not_undo_an_app_push():
    edit = _edit(_ledger(Debit="$10.00"), _ledger(Debit="$12.00", **{"Debit Less Credit": "$12.00"}),
                 ["amount_change"])
    overrides = {"t1": {"category": "Travel", "edit": "e1", "note": ""}}

    result = _plan([edit], overrides=overrides)

    assert result.intents == []
    assert [(line["action"], line.get("value")) for line in result.outbox] == [("set_category", "Travel")]
    assert result.base == []


def test_row_added_becomes_an_adjustment_and_a_rerun_does_not_repeat_it():
    added = _edit(None, _ledger(Description="Farmers market", Category="Groceries",
                                Source="Alex", **{"Debit Less Credit": "$1,234.50"}),
                  ["row_added"])

    first = _plan([added])
    second = _plan([added], base=first.base)

    intent = first.intents[0]
    assert intent["action"] == "budget_adjust" and intent["source"] == "sheet"
    assert intent["sheet"] == {"file_id": "f1", "title": "July"}
    assert intent["set"] == {"date": "2026-07-06", "description": "Farmers market",
                            "budget_category": "Groceries", "amount": 1234.5, "person": "Alex"}
    assert first.counts["adjustments"] == 1
    assert second.intents == [] and second.base == first.base and second.counts["adjustments"] == 0


def test_row_added_credit_is_a_negative_amount():
    added = _edit(None, _ledger(Description="Refund", Category="Groceries",
                                **{"Debit Less Credit": "-$12.50"}),
                  ["row_added"])

    assert _plan([added]).intents[0]["set"]["amount"] == -12.5


@pytest.mark.parametrize("row", [
    {"Debit Less Credit": "", "Description": "Farmers market", "Category": "Groceries"},
    {"Debit Less Credit": "$0.00", "Description": "Farmers market", "Category": "Groceries"},
    {"Debit Less Credit": "$5.00", "Description": "", "Category": "Groceries"},
    {"Debit Less Credit": "n/a", "Description": "Farmers market", "Category": "Groceries"},
])
def test_row_added_without_an_amount_or_a_description_is_skipped(row):
    added = _edit(None, _ledger(**row), ["row_added"])

    result = _plan([added])

    assert result.intents == [] and result.base == []


def test_row_added_with_an_unknown_category_is_reported():
    added = _edit(None, _ledger(Description="Cash", Category="Spaceships",
                                **{"Debit Less Credit": "$8.00"}),
                  ["row_added"])

    result = _plan([added])

    assert result.intents == []
    assert result.conflicts[0]["kind"] == "unknown_category"
    assert result.conflicts[0]["value"] == "Spaceships"


def test_silent_successor_is_the_transaction_the_row_stands_for():
    manifest = [
        _manifest(file_id="f1", title="July", rows=[_row("p", pending=True, Note="PENDING")]),
        _manifest(file_id="f2", title="August", rows=[], silent=[{"transaction_id": "s", "replaces": "p"}]),
    ]
    raw = {**_raw("p", pending=True), **_raw("s", pending=False)}
    edit = _category_edit(txn="p")

    pulled = _plan([edit], manifest=manifest, raw=raw)
    pushed = _plan(manifest=manifest, raw=raw,
                   overrides={"s": {"category": "Travel", "edit": "e1", "note": ""}})

    assert pulled.intents[0]["match"] == {"transaction_id": "s"}
    assert pushed.outbox[0]["file_id"] == "f1"
    assert pushed.outbox[0]["row_id"] == "p"
    assert pushed.outbox[0]["value"] == "Travel"


def test_a_later_manifest_row_is_the_one_a_push_updates():
    manifest = [
        _manifest(file_id="f1", title="July", rows=[_row("p", pending=True)]),
        {"file_id": "f2", "title": "August", "rows": [], "silent": [{"transaction_id": "s", "replaces": "p"}]},
        _manifest(file_id="f3", title="September", txn="s"),
    ]
    raw = _raw("s")

    result = _plan(manifest=manifest, raw=raw,
                   overrides={"s": {"category": "Travel", "edit": "e1", "note": ""}})

    assert [(line["file_id"], line["row_id"]) for line in result.outbox] == [("f3", "s")]


def test_deleting_the_pending_row_that_shows_a_settled_transaction_is_not_imported():
    manifest = [
        _manifest(rows=[_row("p", pending=True)]),
        {"file_id": "f1", "title": "July", "rows": [], "silent": [{"transaction_id": "s", "replaces": "p"}]},
    ]
    raw = {**_raw("p", pending=True), **_raw("s")}
    edit = _edit(_ledger(Note="PENDING"), None, ["row_deleted"], txn="p")

    result = _plan([edit], manifest=manifest, raw=raw)

    assert result.intents == []
    assert result.counts["skipped_pending_delete"] == 1


def test_a_pull_then_the_same_inputs_append_nothing():
    first = _plan([_category_edit()])
    overrides = {"t1": {"category": "Dining Out", "edit": first.intents[0]["id"], "note": ""}}

    second = _plan([_category_edit()], base=first.base, overrides=overrides)

    assert second.intents == [] and second.outbox == [] and second.conflicts == []
    assert second.base == first.base
    assert all(count == 0 for count in second.counts.values())


def test_a_push_whose_write_never_landed_is_planned_again():
    overrides = {"t1": {"category": "Travel", "edit": "e1", "note": ""}}

    first = _plan(overrides=overrides)
    second = _plan(base=first.base, overrides=overrides)

    assert first.outbox and second.outbox == first.outbox
    assert second.intents == [] and second.base == []


def test_a_landed_push_agrees_then_the_next_run_plans_nothing():
    overrides = {"t1": {"category": "Travel", "edit": "e1", "note": ""}}
    pushed = _plan(overrides=overrides)
    mined = [_category_edit("Travel")]

    landed = _plan(mined, base=pushed.base, overrides=overrides)
    again = _plan(mined, base=landed.base, overrides=overrides)

    assert pushed.outbox and pushed.base == []
    assert landed.intents == [] and landed.outbox == [] and landed.conflicts == []
    assert _base(landed) == {("f1", "t1", "category"): "Travel"}
    assert landed.counts["agreed"] == 1
    assert again.intents == [] and again.outbox == [] and again.conflicts == []
    assert again.base == landed.base
    assert all(count == 0 for count in again.counts.values())


def test_a_pulled_value_reverted_to_the_upload_is_pulled_back():
    first = _plan([_category_edit()])
    overrides = {"t1": {"category": "Dining Out", "edit": first.intents[0]["id"], "note": ""}}

    reverted = _plan([], base=first.base, overrides=overrides)

    assert reverted.outbox == [] and reverted.conflicts == []
    assert reverted.intents[0]["set"] == {"budget_category": "Groceries"}
    assert reverted.intents[0]["match"] == {"transaction_id": "t1"}
    assert _base(reverted) == {("f1", "t1", "category"): "Groceries"}
    assert reverted.counts["pulled"] == 1


def test_format_plan_lists_changes_and_conflicts_by_sheet():
    counts = {kind: 0 for kind in tool.KINDS}
    counts.update(pulled=1, pushed=3, conflicts=3, adjustments=1)
    plan = tool.SyncPlan(
        intents=[
            {"action": "budget_edit", "sheet": {"file_id": "f1", "title": "July"},
             "match": {"transaction_id": "t1"},
             "set": {"budget_category": "Dining Out"}, "note": ""},
            {"action": "budget_adjust", "sheet": {"file_id": "f1", "title": "July"},
             "set": {"date": "2026-07-06", "description": "Farmers market",
                     "budget_category": "Groceries", "amount": 1234.5, "person": "Alex"}},
        ],
        outbox=[
            {"file_id": "f2", "title": "August", "row_id": "t2", "action": "set_category",
             "value": "Travel", "ledger": {"Date": "08/01/2026", "Description": "Cafe",
                                           "Category": "Groceries", "Debit Less Credit": "$4.00"}},
            {"file_id": "f2", "title": "August", "row_id": "t9", "action": "delete_row",
             "ledger": {"Date": "08/02/2026", "Description": "Old", "Category": "Groceries",
                        "Debit Less Credit": "$3.00"}},
            {"file_id": "f2", "title": "August", "row_id": "t8", "action": "set_note",
             "value": "trip", "ledger": {"Date": "08/03/2026", "Description": "Hotel",
                                         "Category": "Travel", "Debit Less Credit": "$80.00",
                                         "Note": "PENDING"}},
        ],
        conflicts=[
            {"kind": "value_conflict", "file_id": "f1", "title": "July", "row_id": "t3",
             "field": "category", "base": "Groceries", "sheet": "Dining Out", "app": "Travel"},
            {"kind": "unknown_category", "file_id": "f1", "title": "July", "row_id": "t4",
             "field": "category", "value": "Spaceships"},
            {"kind": "cross_sheet", "row_id": "t5", "field": "note", "values": [
                {"file_id": "f1", "title": "July", "row_id": "t5", "value": "cash"},
                {"file_id": "f2", "title": "August", "row_id": "t5", "value": "card"},
            ]},
        ],
        # The saved base already holds the pull. The listing's before-value is the
        # prior base, and the upload when that is missing — not this saved base.
        base=[{"file_id": "f1", "row_id": "t1", "field": "category", "value": "Dining Out"}],
        counts=counts,
        rows=[
            {"file_id": "f1", "row_id": "t1", "txn": "t1", "date": "07/06/2026",
             "amount": "$10.00", "description": "Market", "category": "Groceries", "note": ""},
            {"file_id": "f1", "row_id": "t3", "txn": "t3", "date": "07/07/2026",
             "amount": "$8.00", "description": "Cafe", "category": "Groceries", "note": ""},
            {"file_id": "f1", "row_id": "t4", "txn": "t4", "date": "07/08/2026",
             "amount": "$2.00", "description": "Mystery", "category": "Groceries", "note": ""},
            {"file_id": "f1", "row_id": "t5", "txn": "t5", "date": "07/09/2026",
             "amount": "$1.00", "description": "Split", "category": "Groceries", "note": ""},
        ],
        prior=[{"file_id": "f2", "row_id": "t9", "field": "category", "value": "Dining Out"}],
    )

    listed = "\n".join([
        "August",
        "  08/01/2026  $4.00  Cafe  category  Groceries → Travel",
        "  08/02/2026  $3.00  Old  category  Dining Out → EXCLUDED",
        "  08/03/2026  $80.00  Hotel  note  → trip",
        "July",
        "  07/06/2026  $1,234.50  Farmers market  category  → Groceries",
        "  07/06/2026  $10.00  Market  category  Groceries → Dining Out",
        "  07/07/2026  $8.00  Cafe  category  conflict: sheet Dining Out ≠ app Travel",
        "  07/08/2026  $2.00  Mystery  category  conflict: Groceries → Spaceships",
        "cross-sheet",
        "  07/09/2026  $1.00  Split  note  conflict: July: cash ≠ August: card",
    ])
    footer = "".join(f"{kind}: {counts[kind]}\n" for kind in tool.KINDS)

    assert tool.format_plan(plan) == listed + "\n" + footer
    assert tool.format_plan(plan, dry_run=True) == "dry-run\n" + listed + "\n" + footer


def test_a_conflict_stays_a_conflict_and_still_appends_nothing():
    overrides = {"t1": {"category": "Travel", "edit": "e1", "note": ""}}
    first = _plan([_category_edit()], overrides=overrides)

    second = _plan([_category_edit()], base=first.base, overrides=overrides)

    assert second.intents == [] and second.conflicts == first.conflicts
    assert second.base == first.base


def _root(tmp_path: Path, edits, manifest, goals, rows=None):
    rows = rows or [{"transaction_id": "t1", "pending": False, "name": "Market",
                     "merchant_name": "Market"}]
    archive = tmp_path / "plaid_category_transformer" / "data" / "transactions_categorized.jsonl"
    archive.parent.mkdir(parents=True)
    archive.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    edits_path = tmp_path / "sheet_edits" / "edits.jsonl"
    edits_path.parent.mkdir(parents=True)
    edits_path.write_text("".join(json.dumps(edit) + "\n" for edit in edits), encoding="utf-8")
    manifest_path = tmp_path / "sheet_exports" / "manifest.jsonl"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text("".join(json.dumps(record) + "\n" for record in manifest)
                             + "not json\n", encoding="utf-8")
    budget = tmp_path / "spend_analyzer" / "config" / "budget.yaml"
    budget.parent.mkdir(parents=True)
    budget.write_text("goals:\n" + "".join(f"  {name}: 1\n" for name in goals), encoding="utf-8")
    return tmp_path


def _real_log_sig():
    """Size and mtime of the process's intent log, or None if it cannot be read.

    Compares the log ``edits_path()`` names. An unreadable root counts as absent
    so the check never opens a sealed store; a change that this process can see
    still fails the test.
    """
    path = Path(manual_edits.edits_path())
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_size, stat.st_mtime_ns)


def test_first_run_without_accept_backfill_writes_nothing(tmp_path, capsys):
    root = _root(tmp_path, [_category_edit()], [_manifest()], ["Groceries", "Dining Out"])

    assert tool.main(["--data-root", str(root)]) == 0

    printed = capsys.readouterr().out
    assert printed.startswith("dry-run\n")
    assert "07/06/2026  $10.00  Market  category  Groceries → Dining Out" in printed
    assert printed.rstrip().endswith(tool.BACKFILL_HINT)
    assert not (root / "sheet_sync").exists()
    assert not (root / "plaid_category_transformer" / "data" / "manual_edits.jsonl").exists()


def test_accept_backfill_appends_intents_and_writes_base(tmp_path):
    root = _root(tmp_path, [_category_edit()], [_manifest()], ["Groceries", "Dining Out"])

    assert tool.main(["--data-root", str(root), "--accept-backfill"]) == 0

    log = (root / "plaid_category_transformer" / "data" / "manual_edits.jsonl").read_text(encoding="utf-8")
    assert log.count("\n") == 1 and "Dining Out" in log
    assert (root / "sheet_sync" / "base.jsonl").is_file()

    assert tool.main(["--data-root", str(root), "--accept-backfill"]) == 0
    assert (root / "plaid_category_transformer" / "data" / "manual_edits.jsonl").read_text(encoding="utf-8") == log


def test_accept_backfill_of_an_empty_plan_creates_base(tmp_path):
    root = _root(tmp_path, [], [_manifest()], ["Groceries"])

    assert tool.main(["--data-root", str(root), "--accept-backfill"]) == 0

    base = root / "sheet_sync" / "base.jsonl"
    assert base.is_file()
    assert base.read_text(encoding="utf-8") == ""


def test_execute_pulls_once_and_a_rerun_writes_nothing(tmp_path, capsys):
    root = _root(tmp_path, [_category_edit()], [_manifest()], ["Groceries", "Dining Out"])
    real = _real_log_sig()

    assert tool.main(["--data-root", str(root), "--accept-backfill"]) == 0
    printed = capsys.readouterr().out
    assert "July\n" in printed and "Groceries → Dining Out" in printed and "pulled: 1" in printed
    log = (root / "plaid_category_transformer" / "data" / "manual_edits.jsonl").read_text(encoding="utf-8")
    base = (root / "sheet_sync" / "base.jsonl").read_text(encoding="utf-8")
    assert log.count("\n") == 1 and "Dining Out" in log
    assert (root / "sheet_sync" / "outbox.jsonl").read_text(encoding="utf-8") == ""
    assert _real_log_sig() == real

    capsys.readouterr()
    assert tool.main(["--data-root", str(root)]) == 0

    assert (root / "plaid_category_transformer" / "data" / "manual_edits.jsonl").read_text(encoding="utf-8") == log
    assert (root / "sheet_sync" / "base.jsonl").read_text(encoding="utf-8") == base
    assert (root / "sheet_sync" / "outbox.jsonl").read_text(encoding="utf-8") == ""
    assert (root / "sheet_sync" / "conflicts.jsonl").read_text(encoding="utf-8") == ""
    assert "pulled: 0" in capsys.readouterr().out


def test_outputs_are_private_and_replaced(tmp_path):
    root = _root(tmp_path, [_category_edit()], [_manifest()], ["Groceries", "Dining Out"])
    stale = root / "sheet_sync" / "outbox.jsonl"
    stale.parent.mkdir()
    stale.write_text("stale\n", encoding="utf-8")

    tool.main(["--data-root", str(root), "--accept-backfill"])

    directory = root / "sheet_sync"
    assert directory.stat().st_mode & 0o777 == 0o700
    for name in ("base.jsonl", "outbox.jsonl", "conflicts.jsonl"):
        assert (directory / name).stat().st_mode & 0o777 == 0o600
    assert stale.read_text(encoding="utf-8") == ""


def test_dry_run_prints_counts_and_writes_nothing(tmp_path, capsys):
    root = _root(tmp_path, [_category_edit()], [_manifest()], ["Groceries", "Dining Out"])
    real = _real_log_sig()

    assert tool.main(["--data-root", str(root), "--dry-run"]) == 0

    printed = capsys.readouterr().out
    assert printed.startswith("dry-run\n")
    assert "July\n" in printed
    assert "07/06/2026  $10.00  Market  category  Groceries → Dining Out" in printed
    assert "pulled: 1" in printed
    assert not (root / "sheet_sync").exists()
    assert not (root / "plaid_category_transformer" / "data" / "manual_edits.jsonl").exists()
    assert _real_log_sig() == real


def test_a_category_already_on_a_sheet_is_known_without_a_goal(tmp_path):
    manifest = [_manifest(rows=[_row(), _row("t2", Category="From The Sheet")])]
    root = _root(tmp_path, [_category_edit("From The Sheet")], manifest, ["Dining Out"],
                 rows=[{"transaction_id": "t1", "pending": False, "name": "Market"},
                       {"transaction_id": "t2", "pending": False, "name": "Market"}])

    tool.main(["--data-root", str(root), "--accept-backfill"])

    log = (root / "plaid_category_transformer" / "data" / "manual_edits.jsonl").read_text(encoding="utf-8")
    assert json.loads(log)["set"]["budget_category"] == "From The Sheet"
    assert (root / "sheet_sync" / "conflicts.jsonl").read_text(encoding="utf-8") == ""
