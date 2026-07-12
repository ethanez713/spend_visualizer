"""Tests for the read-only rule-table bridge (rules_bridge.py).

Like the manual-edits bridge, this imports the transformer's REAL src.config /
src.rules via transformer_root (deliberate coupling) and skips when the sibling
repo isn't present. Match scans run against synthetic records with an injected
(empty or seeded) merchant memory, so no test depends on live data.
"""
import pytest

import rules_bridge

pytestmark = pytest.mark.skipif(
    rules_bridge.status() is not None,
    reason="plaid_category_transformer repo not available (bridge disabled)",
)


def _record(tid, **kw):
    base = {
        "transaction_id": tid,
        "date": "2026-05-01",
        "amount": 9.99,
        "merchant_name": None,
        "name": None,
        "personal_finance_category": {
            "primary": "GENERAL_SERVICES",
            "detailed": "GENERAL_SERVICES_OTHER_GENERAL_SERVICES",
            "confidence_level": "LOW",
        },
    }
    base.update(kw)
    return base


def _empty_memory():
    _, t_rules = rules_bridge._import_transformer()
    return t_rules.MerchantMemory(None, read_only=True)


def test_every_rule_id_resolves_in_rule_details():
    # The rule_id is the join key between a row's provenance and the rule table
    # (idea C) — every flattened rule must be findable, plus the two memory ids.
    details = rules_bridge.rule_details()
    rules = rules_bridge.transformer_rules()
    assert rules, "transformer rule tables came back empty"
    for r in rules:
        assert r["rule_id"] in details
        assert r["trust"] in ("auto", "flag")
        assert r["origin"] in ("built-in", "personal")
    assert "memory:entity_id" in details and "memory:name" in details


def test_match_scan_groups_hits_under_resolvable_rule_ids():
    # 'spotify' is a shipped keyword rule; whichever rule wins first-match, the
    # scan must file the row under a rule_id that rule_details can explain.
    recs = [_record("t1", merchant_name="Spotify", name="SPOTIFY P1"),
            _record("t2", name="opaque wire xfer")]  # matches nothing
    scan = rules_bridge.match_scan(recs, memory=_empty_memory())
    rows = [r for hits in scan["by_rule"].values() for r in hits]
    assert [r["transaction_id"] for r in rows] == ["t1"]
    for rule_id in scan["by_rule"]:
        assert rule_id in rules_bridge.rule_details()


def test_match_scan_attributes_memory_hits_to_their_entry():
    mem = _empty_memory()
    mem.store["ent:ent_x"] = {"primary": "FOOD_AND_DRINK",
                              "detailed": "FOOD_AND_DRINK_COFFEE"}
    recs = [_record("t1", merchant_entity_id="ent_x", merchant_name="Some Cafe"),
            _record("t2", merchant_entity_id="ent_x", merchant_name="Some Cafe")]
    scan = rules_bridge.match_scan(recs, memory=mem)
    assert scan["by_memory_key"] == {"ent:ent_x": 2}
    assert len(scan["by_rule"]["memory:entity_id"]) == 2

    entries = rules_bridge.merchant_memory_entries(mem)
    assert entries == [{"key": "ent:ent_x", "match": "entity id (auto)",
                        "merchant": "ent_x", "primary": "FOOD_AND_DRINK",
                        "detailed": "FOOD_AND_DRINK_COFFEE"}]


def test_converter_missing_dir_yields_none(monkeypatch, tmp_path):
    monkeypatch.setenv("SPEND_VISUALIZER_CONVERTER", str(tmp_path / "nope"))
    assert rules_bridge.converter_dir() is None
    assert rules_bridge.converter_rules() is None


def test_converter_rules_load_from_env_dir(monkeypatch, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "config.py").write_text(
        'PINNED_RULES = [("Dining Out", ["doordash", "grubhub"])]\n'
        'PFC_PRIMARY_MAP = {"ENTERTAINMENT": "Entertainment"}\n'
        'PFC_DETAILED_MAP = {"FOOD_AND_DRINK_GROCERIES": "Groceries"}\n'
        'PFC_DROP_PRIMARY = {"INCOME"}\n'
        'PFC_DROP_DETAILED = set()\n',
        encoding="utf-8")
    monkeypatch.setenv("SPEND_VISUALIZER_CONVERTER", str(tmp_path))
    conv = rules_bridge.converter_rules()
    assert conv["pinned"] == [("Dining Out", ["doordash", "grubhub"])]
    assert conv["primary_map"] == {"ENTERTAINMENT": "Entertainment"}
    assert conv["detailed_map"] == {"FOOD_AND_DRINK_GROCERIES": "Groceries"}
    assert conv["drop_primary"] == ["INCOME"] and conv["drop_detailed"] == []


def test_converter_broken_config_fails_soft(monkeypatch, tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "config.py").write_text("raise RuntimeError('boom')\n",
                                                encoding="utf-8")
    monkeypatch.setenv("SPEND_VISUALIZER_CONVERTER", str(tmp_path))
    assert rules_bridge.converter_rules() is None
