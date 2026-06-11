"""Regression: a click on any chart sector must resolve to a filterable (non-empty) slice.

The treemap/sunburst "crash + snap back to top" bug: sector paths were stored humanized
(atom 'FOOD AND DRINK COFFEE') while _drill_filter matches the raw column
('FOOD_AND_DRINK_COFFEE'), so every drill through the atom level filtered to zero rows and
silently reset wheel_root. This locks in the invariant the bug violated — every emitted
sector's click path selects a non-empty slice — exercised with underscore-bearing atoms.
"""
import pandas as pd

from cube import CATEGORY_LEVELS
from views.drilldown import _build_hierarchy_fig, _clicked_path, _drill_filter

_ROWS = pd.DataFrame([
    {"tier0": "Discretionary", "tier1": "Dining Out", "tier2": "Coffee",
     "atom": "FOOD_AND_DRINK_COFFEE", "merchant": "Blue Bottle", "spend": 6.5},
    {"tier0": "Discretionary", "tier1": "Dining Out", "tier2": "Coffee",
     "atom": "FOOD_AND_DRINK_COFFEE", "merchant": "Sightglass", "spend": 5.0},
    {"tier0": "Necessary", "tier1": "Groceries", "tier2": "Groceries",
     "atom": "FOOD_AND_DRINK_GROCERIES", "merchant": "Costco", "spend": 343.0},
    {"tier0": "Necessary", "tier1": "Utilities", "tier2": "Telephone",
     "atom": "RENT_AND_UTILITIES_TELEPHONE", "merchant": "T-Mobile", "spend": 70.0},
])


def test_every_sector_click_resolves_to_a_nonempty_slice():
    fig, abs_nodes = _build_hierarchy_fig(_ROWS, CATEGORY_LEVELS, [], treemap=False)
    assert any(len(p) == len(CATEGORY_LEVELS) for p in abs_nodes), "no merchant-depth leaf"
    for i, path in enumerate(abs_nodes):
        target = _clicked_path([{"curveNumber": 0, "pointNumber": i}], abs_nodes)
        assert target == path
        assert not _drill_filter(_ROWS, target).empty, f"sector {i} {target} filtered to nothing"


def test_atom_sector_labels_are_humanized_even_though_state_is_raw():
    fig, abs_nodes = _build_hierarchy_fig(_ROWS, CATEGORY_LEVELS, [], treemap=False)
    # the drill state (abs_nodes) keeps the raw underscore code...
    assert ["FOOD_AND_DRINK_COFFEE"] != ["FOOD AND DRINK COFFEE"]
    assert any(p[-1] == "FOOD_AND_DRINK_COFFEE" for p in abs_nodes if len(p) == 4)
    # ...while the visible label for that sector is humanized
    labels = list(fig.data[0].labels)
    assert "FOOD AND DRINK COFFEE" in labels
    assert "FOOD_AND_DRINK_COFFEE" not in labels
