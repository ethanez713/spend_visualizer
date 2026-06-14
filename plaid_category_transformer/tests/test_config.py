"""The editable policy in ``config.py`` is well-formed and its rules fire as intended.

These guard the readable-config surface the project leans on: every hardcoded
``(primary, detailed)`` pair must be a real taxonomy category (a typo would otherwise be
silently dropped at apply time), and the Capital One Travel rules — the motivating example
('COT*FLT' was mislabeled shipping) — must actually resolve to flights/lodging.
"""
import pytest

from src import config
from src.pfc_taxonomy import is_valid
from src.rules import apply_rules

ALL_RULE_TABLES = {
    "POS_PREFIX_RULES": config.POS_PREFIX_RULES,
    "WEBSITE_RULES": config.WEBSITE_RULES,
    "KEYWORD_RULES": config.KEYWORD_RULES,
}


@pytest.mark.parametrize("table_name", sorted(ALL_RULE_TABLES))
def given_a_config_rule_table_when_validated_then_every_pair_is_in_taxonomy(table_name):
    for match, (primary, detailed), trust in ALL_RULE_TABLES[table_name]:
        assert is_valid(primary, detailed), (
            f"{table_name} rule {match!r} → {primary}/{detailed} is not a valid PFC pair")
        assert trust in {"auto", "flag"}, (
            f"{table_name} rule {match!r} has unknown trust {trust!r}")


def given_audit_levels_when_inspected_then_default_covers_all_plaid_levels():
    assert {"LOW", "MEDIUM", "HIGH", "VERY_HIGH"} <= config.AUDIT_CONFIDENCE_LEVELS


# ── The motivating example: Capital One Travel ────────────────────────────────

def given_cot_flight_row_when_ruled_then_travel_flights(make_record):
    rec = make_record(merchant_name="COT", name="COT*FLT", original_description="COT*FLT",
                      website=None, merchant_entity_id=None)
    hit = apply_rules(rec, None)
    assert hit is not None
    assert (hit.primary, hit.detailed) == ("TRAVEL", "TRAVEL_FLIGHTS")
    assert hit.rule_name == "pos:cot*flt"


def given_cot_hotel_row_when_ruled_then_travel_lodging(make_record):
    rec = make_record(merchant_name="COT", name="COT*HTL", original_description="COT*HTL",
                      website=None, merchant_entity_id=None)
    hit = apply_rules(rec, None)
    assert hit is not None
    assert (hit.primary, hit.detailed) == ("TRAVEL", "TRAVEL_LODGING")


# ── Personal rule overlay (personal/local rules live under the data root) ──────
# config._load_personal_rules reads DATA_ROOT lazily inside the function, so we point
# src.paths.DATA_ROOT at a tmp dir and call it directly (re-importing config to mutate
# the module-level tables would leak into other tests).

def _write_personal(tmp_path, monkeypatch, text):
    import src.paths as paths
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)
    cfg = tmp_path / "plaid_category_transformer" / "config"
    cfg.mkdir(parents=True)
    (cfg / "personal_rules.json").write_text(text, encoding="utf-8")


def given_no_data_root_file_when_loaded_then_empty(tmp_path, monkeypatch):
    import src.paths as paths
    monkeypatch.setattr(paths, "DATA_ROOT", tmp_path)  # dir exists, file does not
    assert config._load_personal_rules() == {"pos_prefix": [], "website": [], "keyword": []}


def given_valid_personal_rules_when_loaded_then_parsed(tmp_path, monkeypatch):
    _write_personal(tmp_path, monkeypatch,
        '{"keyword": [["my local coop", ["FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES"], "auto"]]}')
    loaded = config._load_personal_rules()
    assert loaded["keyword"] == [
        ("my local coop", ("FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES"), "auto")]
    assert loaded["pos_prefix"] == [] and loaded["website"] == []


def given_malformed_personal_file_when_loaded_then_fail_soft(tmp_path, monkeypatch):
    # A malformed JSON file (or a bad entry) must never break the run — fail soft to empty.
    _write_personal(tmp_path, monkeypatch, "{ not valid json")
    assert config._load_personal_rules() == {"pos_prefix": [], "website": [], "keyword": []}


def given_bad_entry_shape_when_loaded_then_skipped(tmp_path, monkeypatch):
    _write_personal(tmp_path, monkeypatch,
        '{"keyword": [["too", "short"], ["ok phrase", ["FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES"], "auto"]]}')
    # The well-formed entry survives; the malformed one is dropped.
    assert config._load_personal_rules()["keyword"] == [
        ("ok phrase", ("FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES"), "auto")]
