"""The report-only correction queue: add via the form, list, delete.

conftest redirects corrections.STORE to tmp_path, so every append/delete here
hits a throwaway file. The form under test is the merchants-tab 'Other fix
(report only)' tab, which renders once a merchant is opened.
"""
import corrections as corr

from tests.ui._harness import go, metric, open_first_merchant, widget


def test_given_merchant_name_fix_when_submitted_then_correction_recorded_upstream(at):
    open_first_merchant(at)
    box = widget(at.text_input, label="merchant_name →")
    box.set_value((box.value or "Unknown") + " (fixed)")
    widget(at.radio, label="Fix belongs to").set_value("upstream")
    widget(at.button, label="Add to corrections report").click()
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]

    recs = corr.load_corrections()
    assert len(recs) == 1, recs
    assert recs[0]["layer"] == "upstream" and recs[0]["scope"] == "merchant"
    assert recs[0]["suggestion"]["merchant_name"].endswith("(fixed)")
    # the post-submit rerun lists it in the Corrections tab
    assert metric(go(at, "Overrides"), "Upstream fixes (collector)") == "1"


def test_given_no_field_changed_when_submitted_then_warning_and_nothing_stored(at):
    open_first_merchant(at)
    widget(at.button, label="Add to corrections report").click()
    at.run()
    assert any("Change at least one field" in str(w.value) for w in at.warning)
    assert corr.load_corrections() == []


def test_given_recorded_correction_when_deleted_then_queue_empty(boot_app):
    seeded = corr.add_correction(scope="merchant", target={"label": "T", "merchant": "T"},
                                 original={"merchant_name": "T"},
                                 suggestion={"merchant_name": "U"}, note="seed")
    at = boot_app("Overrides")
    widget(at.button, key=f"del_{seeded['id']}").click()
    at.run()
    assert corr.load_corrections() == []
