"""Recategorize (PFC) — the highest-stakes UI flow: it appends intents to the
transformer's manual-edit log. conftest redirects that log to tmp_path; these
tests drive the real form on the Merchants tab end to end and assert on the
exact payload written (and on what must NOT be written).
"""
import json

import manual_edits

from tests.ui._harness import widget


def test_given_new_category_when_save_clicked_then_intent_appended_to_log(at, isolate_writes):
    primaries, detailed_map = manual_edits.taxonomy()
    cur_p = widget(at.selectbox, key_prefix="rc_p_").value
    new_p = [p for p in primaries if p != cur_p][0]
    widget(at.selectbox, key_prefix="rc_p_").set_value(new_p)
    at.run()  # detailed dropdown repopulates for the new primary

    new_d = detailed_map[new_p][0]
    widget(at.selectbox, key_prefix="rc_d_").set_value(new_d)
    widget(at.radio, key_prefix="rc_s_").set_value("transaction")
    widget(at.text_input, key_prefix="rc_n_").set_value("added by AppTest")
    widget(at.button, key_prefix="rc_b_").click()
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert at.success, "no success message after saving the intent"

    lines = isolate_writes.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, lines
    it = json.loads(lines[0])
    assert it["scope"] == "transaction"
    assert it["match"].get("transaction_id")
    assert (it["set"]["primary"], it["set"]["detailed"]) == (new_p, new_d)
    assert it["source"] == "ui" and it["note"] == "added by AppTest"


def test_given_unchanged_category_when_save_clicked_then_warning_and_no_write(at, isolate_writes):
    widget(at.button, key_prefix="rc_b_").click()
    at.run()
    assert any("already the current category" in str(w.value) for w in at.warning)
    assert not isolate_writes.exists(), "a no-op save must not touch the intent log"


def test_given_pending_intent_when_revoked_then_it_no_longer_applies(boot_app, isolate_writes):
    raw = next(iter(manual_edits.raw_index().values()))
    primaries, detailed_map = manual_edits.taxonomy()
    seeded = manual_edits.add_edit(raw, scope="transaction", primary=primaries[0],
                                   detailed=detailed_map[primaries[0]][0], note="seed")
    at = boot_app()
    widget(at.button, key=f"revoke_{seeded['id']}").click()
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert manual_edits.intents() == [], "revoked intent still applies"
    # the log is append-only: revoking adds a tombstone, never rewrites history
    assert len(isolate_writes.read_text(encoding="utf-8").splitlines()) == 2
