"""Stage 1 — deterministic mechanical rules + persistent merchant memory.

Runs before the LLM and exploits ALL of Plaid's signals, richest identity first:
  1. merchant memory keyed by ``merchant_entity_id`` (Plaid's stable merchant id) —
     an exact id hit is the strongest possible signal (HIGH);
  2. merchant memory keyed by a normalized ``merchant_name`` fallback (MEDIUM);
  3. point-of-sale text prefixes (``TST*`` → restaurant);
  4. ``website`` domain hints;
  5. keyword/token rules over ``merchant_name`` / ``name`` / ``original_description``.

First hit wins. The result is a *suggestion* carried into Stage 2 (the LLM sees it) and
recorded as provenance only if it becomes the final value. ``normalize_merchant`` and
``contains_word`` are ported from ``converter/src/converter.py`` (whole-token matching so
'lime' can't hit 'sublime', etc.).

Memory lives in ``.secrets/merchant_memory.json`` (0600, atomic write); the transformer tops
it up with each run's final decisions so a once-resolved merchant is a HIGH hit next time.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass

from .config import KEYWORD_RULES, POS_PREFIX_RULES, WEBSITE_RULES
from .pfc_taxonomy import is_valid

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_MEMORY = os.path.join(_PROJECT_ROOT, ".secrets", "merchant_memory.json")


@dataclass
class RuleHit:
    """A mechanical suggestion: a category plus which rule produced it and how sure."""
    primary: str
    detailed: str
    rule_name: str
    confidence: str  # "HIGH" (exact entity-id memory) | "MEDIUM" (everything else)
    trust: str       # "auto" = overwrite in place | "flag" = suggest for human review


# ── Text normalization (ported from converter) ───────────────────────────────

_NOISE = re.compile(
    r"((?<![a-z])x{2,}"          # masked card digits 'XXXX' (not 'exxon'/'maxx')
    r"|\d{2,}"                   # multi-digit runs (ref/account numbers)
    r"|#\w+|\*"                  # '#1234', asterisks
    r"|\bna\b|\bllc\b|\binc\b"
    r"|\bending in.*"
    r"|\bpay date.*|\breg shs.*|\bid:.*|\bref:.*)",
    re.I)


def _lc(x) -> str:
    return "" if x is None else str(x).lower()


def normalize_merchant(desc: str) -> str:
    """Collapse a raw description to a stable merchant key:
    'STARBUCKS #1234' -> 'starbucks' ; 'TST* Cielo Rojo 12' -> 'tst cielo rojo'."""
    s = _lc(desc)
    s = _NOISE.sub(" ", s)
    s = re.sub(r"[^a-z\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def contains_word(text: str, keyword: str) -> bool:
    """True if ``keyword`` appears in ``text`` as a standalone token (not a substring
    inside a longer word). ``text`` is assumed already lowercased."""
    pattern = r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])"
    return re.search(pattern, text) is not None


# The static rule tables (POS prefixes, websites, keywords) live in ``config.py`` as
# readable, editable data. They map to exact ``(primary, detailed)`` pairs from the
# vendored PFC taxonomy; any pair that fails taxonomy validation is dropped at apply time
# (caught by tests). This module only holds the matching *logic*, not the rule data.


def _candidate_text(record: dict) -> str:
    """Lowercased merchant_name + name + original_description for keyword matching."""
    parts = [record.get("merchant_name"), record.get("name"),
             record.get("original_description")]
    return " ".join(_lc(p) for p in parts if p)


# ── Merchant memory ────────────────────────────────────────────────────────────

class MerchantMemory:
    """JSON map of a merchant key -> {"primary", "detailed"}.

    Two key spaces in one dict, namespaced so they can't collide:
      ``ent:<merchant_entity_id>``  — exact, stable Plaid merchant id (HIGH-confidence);
      ``name:<normalized_merchant>`` — fuzzy fallback when no entity id (MEDIUM).
    """

    def __init__(self, path: str | None = DEFAULT_MEMORY, read_only: bool = False):
        self.path = path
        self.read_only = read_only
        self.store: dict[str, dict] = {}
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    self.store = json.load(f)
            except Exception as e:  # noqa: BLE001 — corrupt memory must not crash a run
                print(f"  rules: could not load merchant memory ({e}); starting fresh.")
                self.store = {}

    @staticmethod
    def _ent_key(entity_id) -> str | None:
        return f"ent:{entity_id}" if entity_id else None

    @staticmethod
    def _name_key(merchant_name) -> str | None:
        norm = normalize_merchant(merchant_name) if merchant_name else ""
        return f"name:{norm}" if norm else None

    def lookup(self, record: dict) -> RuleHit | None:
        """Entity-id hit (HIGH) first, then normalized-name fallback (MEDIUM)."""
        ek = self._ent_key(record.get("merchant_entity_id"))
        if ek and ek in self.store:
            e = self.store[ek]
            # Exact stable-merchant-id match (incl. user-confirmed corrections) → trusted.
            return RuleHit(e["primary"], e["detailed"], "memory:entity_id", "HIGH", "auto")
        nk = self._name_key(record.get("merchant_name"))
        if nk and nk in self.store:
            e = self.store[nk]
            # Fuzzy normalized-name match → only a suggestion (different merchants collide).
            return RuleHit(e["primary"], e["detailed"], "memory:name", "MEDIUM", "flag")
        return None

    def remember(self, record: dict, primary: str, detailed: str) -> None:
        """Cache a resolved category under both available key spaces (idempotent)."""
        if self.read_only:
            return
        val = {"primary": primary, "detailed": detailed}
        for key in (self._ent_key(record.get("merchant_entity_id")),
                    self._name_key(record.get("merchant_name"))):
            if key:
                self.store[key] = val

    def save(self) -> None:
        if self.read_only or not self.path:
            return
        parent = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(parent, mode=0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.store, f, indent=2, sort_keys=True)
            os.chmod(tmp, 0o600)  # financial data — owner-only
            os.replace(tmp, self.path)
        except Exception:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise


# ── The cascade ─────────────────────────────────────────────────────────────

def apply_rules(record: dict, memory: MerchantMemory | None = None) -> RuleHit | None:
    """Return the first mechanical suggestion for ``record``, or None.

    Order (first hit wins): merchant memory → POS prefix → website → keywords. Any hit
    whose ``(primary, detailed)`` is not valid in the vendored taxonomy is skipped (so a
    typo'd rule table can never emit an out-of-taxonomy category).
    """
    candidates: list[RuleHit] = []

    if memory is not None:
        hit = memory.lookup(record)
        if hit:
            candidates.append(hit)

    # POS / booking text prefixes (e.g. 'TST*' = Toast restaurant, 'COT*FLT' = Capital
    # One Travel flight). Matched as a substring of the raw bank text, most-specific first.
    pos_text = " ".join((_lc(record.get("name")), _lc(record.get("original_description"))))
    for prefix, (p, d), trust in POS_PREFIX_RULES:
        if prefix.lower() in pos_text:
            slug = prefix.lower().rstrip("*")
            candidates.append(RuleHit(p, d, f"pos:{slug}", "MEDIUM", trust))
            break

    website = _lc(record.get("website"))
    if website:
        for domain, (p, d), trust in WEBSITE_RULES:
            if domain in website:
                candidates.append(RuleHit(p, d, f"website:{domain}", "MEDIUM", trust))
                break

    text = _candidate_text(record)
    for kw, (p, d), trust in KEYWORD_RULES:
        if contains_word(text, kw):
            candidates.append(RuleHit(p, d, f"keyword:{kw}", "MEDIUM", trust))
            break

    for hit in candidates:
        if is_valid(hit.primary, hit.detailed):
            return hit
    return None
