"""The vendored PFC taxonomy is well-formed and matches Plaid's documented shape."""
from src import pfc_taxonomy


def given_vendored_csv_when_loaded_then_has_16_known_primaries():
    assert set(pfc_taxonomy.PRIMARY) == pfc_taxonomy.EXPECTED_PRIMARY
    assert len(pfc_taxonomy.PRIMARY) == 16


def given_detailed_map_when_inspected_then_every_detailed_is_prefixed_by_its_primary():
    for primary, detaileds in pfc_taxonomy.DETAILED.items():
        assert detaileds, f"{primary} has no detailed categories"
        for d in detaileds:
            assert d.startswith(primary + "_"), f"{d} not prefixed by {primary}"


def given_a_real_pair_when_validated_then_is_valid():
    assert pfc_taxonomy.is_valid("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE")


def given_a_detailed_under_wrong_primary_when_validated_then_invalid():
    # COFFEE is a real detailed value, but not under ENTERTAINMENT.
    assert not pfc_taxonomy.is_valid("ENTERTAINMENT", "FOOD_AND_DRINK_COFFEE")


def given_a_nonexistent_pair_when_validated_then_invalid():
    assert not pfc_taxonomy.is_valid("MADE_UP", "MADE_UP_THING")


def given_taxonomy_block_when_rendered_then_contains_primaries_and_glosses():
    block = pfc_taxonomy.taxonomy_block()
    assert "FOOD_AND_DRINK:" in block
    assert "FOOD_AND_DRINK_COFFEE" in block
    # gloss text is included
    assert "coffee" in block.lower()


def given_all_detailed_when_counted_then_matches_flat_set():
    total = sum(len(v) for v in pfc_taxonomy.DETAILED.values())
    assert total == len(pfc_taxonomy.ALL_DETAILED)
