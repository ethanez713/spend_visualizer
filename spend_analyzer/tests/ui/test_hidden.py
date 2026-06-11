"""The hide/unhide flow: view-layer only, totals exclude hidden, fully reversible."""
import state
from viz import money

from tests.ui._harness import metric, texts, widget


def _hide_rule(value):
    return {"dim": "tier1", "value": value, "label": str(value)}


def test_given_top_category_hidden_when_rerun_then_total_excludes_it(at):
    table = at.dataframe[0].value
    top, amt = table.index[0], float(table["Actual"].iloc[0])
    full = float(table["Actual"].sum())
    at.session_state[state.HIDDEN_KEY] = [_hide_rule(top)]
    at.run()
    assert not at.exception, [str(e.value) for e in at.exception]
    assert metric(at, "Total") == money(full - amt)
    assert any("🙈" in t and str(top) in t for t in texts(at)), "sidebar hidden-rule row missing"


def test_given_hidden_category_when_sidebar_unhide_clicked_then_total_restored(at):
    table = at.dataframe[0].value
    top, full = table.index[0], float(table["Actual"].sum())
    at.session_state[state.HIDDEN_KEY] = [_hide_rule(top)]
    at.run()
    widget(at.button, key=f"unhide_tier1_{top}").click()
    at.run()
    assert at.session_state[state.HIDDEN_KEY] == []
    assert metric(at, "Total") == money(full)


def test_given_multiple_hidden_when_show_all_clicked_then_all_restored(at):
    table = at.dataframe[0].value
    at.session_state[state.HIDDEN_KEY] = [_hide_rule(v) for v in table.index[:2]]
    at.run()
    widget(at.button, label="Show all").click()
    at.run()
    assert at.session_state[state.HIDDEN_KEY] == []
