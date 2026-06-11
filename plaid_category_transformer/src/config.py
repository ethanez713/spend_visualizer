"""All editable categorization policy — the ONE file to touch when changing rules.

Everything that decides *which rows get audited*, *what the deterministic rules do*, and
*how the LLM stage is configured* lives here as plain, readable data. Execution logic
lives in the other modules:
  - ``rules.py``       consumes the hardcoded rule tables below (first match wins).
  - ``schema.py``      consumes ``AUDIT_CONFIDENCE_LEVELS`` to pick rows.
  - ``llm.py``         consumes the ``LLM_*`` knobs.
  - ``transformer.py`` consumes ``REQUIRE_HIGH_LLM_TO_OVERRIDE_TRUSTED``.

Categories must use exact ``(primary, detailed)`` strings from the vendored PFC taxonomy
(``pfc_taxonomy.csv``); any rule whose pair isn't valid is dropped at apply time and
caught by ``tests/test_rules.py`` / ``tests/test_config.py``.

The companion taxonomy file ``pfc_taxonomy.csv`` is the menu of legal categories.
"""
from __future__ import annotations

# ── 1. SELECTION POLICY — which rows do we audit? ─────────────────────────────
# Plaid stamps every category with a confidence_level. We re-audit ALL of them: even
# HIGH/VERY_HIGH rows have been observed wrong (e.g. Capital One Travel flights, 'COT*FLT',
# landing in GENERAL_SERVICES_POSTAGE_AND_SHIPPING). A missing/blank level counts as
# UNKNOWN. To audit fewer rows (and run faster), remove levels from this set — e.g. drop
# {"HIGH", "VERY_HIGH"} to only re-check Plaid's own low-confidence guesses.
AUDIT_CONFIDENCE_LEVELS = {"LOW", "MEDIUM", "HIGH", "VERY_HIGH", "UNKNOWN"}

# ── AUTHORITY MODEL — who is allowed to overwrite a category? ─────────────────
# Most-trusted signal wins, and the noisy local LLM never SILENTLY overwrites a category.
# Authority, highest to lowest:
#   1. Mechanical 'auto' rules (entity-id memory + the specific COT* booking prefixes) —
#      reliable enough to overwrite in place.
#   2. The LLM — a cheap, noisy REVIEWER. By default it only FLAGS disagreements (writing
#      the category_review_* columns) for a human to adjudicate; it does not overwrite.
#   3. Mechanical 'flag' rules (loose keyword/website/TST* matches, name-memory) — treated
#      as suggestions too: they FLAG, they don't overwrite (they over-reach, e.g. a bare
#      'lyft' keyword hitting Capital Bikeshare, or 'TST*' downgrading a coffee shop).
#
# LLM_AUTHORITY tunes how much the LLM may auto-apply:
#   "flag"            — (default) the LLM NEVER overwrites; every disagreement is a flag.
#   "apply_when_high" — the LLM auto-applies ONLY when it is HIGH-confidence AND Plaid's own
#                       label was untrusted (LOW/MEDIUM/UNKNOWN); everything else is flagged.
#   "final"           — the LLM overwrites whenever it disagrees on an untrusted row (legacy;
#                       trusted rows are still only flagged). Not recommended.
LLM_AUTHORITY = "flag"

# Plaid confidence levels we treat as trusted: the LLM may NEVER auto-change these (only
# flag them), under any LLM_AUTHORITY. Mechanical 'auto' rules still apply to them.
TRUSTED_CONFIDENCE_LEVELS = {"HIGH", "VERY_HIGH"}


# ── 2. HARDCODED MECHANICAL RULES (Stage 1) ───────────────────────────────────
# Deterministic overrides applied BEFORE the LLM. Cascade order is fixed in rules.py:
# merchant memory → POS prefix → website → keyword; FIRST hit wins. Every rule also shows
# its suggestion to the LLM.
#
# Each rule carries a TRUST level controlling what happens on a match:
#   "auto" — reliable enough to OVERWRITE the category in place (recorded as a correction).
#   "flag" — only a SUGGESTION: it raises a review flag for a human, never auto-overwrites.
# Keep "auto" for genuinely unambiguous identifiers; loose keyword/word matches over-reach
# (a bare 'lyft' hits Capital Bikeshare; 'TST*' downgrades a coffee shop to RESTAURANT), so
# those are "flag". Any rule whose (primary, detailed) isn't a real taxonomy pair is dropped.

# (a) Point-of-sale / booking text prefixes. Matched (case-insensitive) as a substring of
# the raw bank 'name' / 'original_description'. List the most specific prefixes first.
#   'COT*FLT' = Capital One Travel flight booking → flights   (specific → auto).
#   'COT*HTL' = Capital One Travel hotel booking  → lodging   (specific → auto).
#   'TST*'    = Toast POS (any Toast merchant)    → restaurant (over-reaches → flag).
POS_PREFIX_RULES: list[tuple[str, tuple[str, str], str]] = [
    ("COT*FLT", ("TRAVEL", "TRAVEL_FLIGHTS"),               "auto"),
    ("COT*HTL", ("TRAVEL", "TRAVEL_LODGING"),               "auto"),
    ("TST*",    ("FOOD_AND_DRINK", "FOOD_AND_DRINK_RESTAURANT"), "flag"),
]

# (b) Merchant website domain → category. Matched as a substring of the 'website' field.
# Domains are decent hints but not definitive (a domain can front several lines of
# business), so these are suggestions: "flag".
WEBSITE_RULES: list[tuple[str, tuple[str, str], str]] = [
    ("uber.com",      ("TRANSPORTATION", "TRANSPORTATION_TAXIS_AND_RIDE_SHARES"), "flag"),
    ("lyft.com",      ("TRANSPORTATION", "TRANSPORTATION_TAXIS_AND_RIDE_SHARES"), "flag"),
    ("doordash.com",  ("FOOD_AND_DRINK", "FOOD_AND_DRINK_RESTAURANT"),            "flag"),
    ("grubhub.com",   ("FOOD_AND_DRINK", "FOOD_AND_DRINK_RESTAURANT"),            "flag"),
    ("starbucks.com", ("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE"),                "flag"),
    ("netflix.com",   ("ENTERTAINMENT", "ENTERTAINMENT_TV_AND_MOVIES"),           "flag"),
    ("spotify.com",   ("ENTERTAINMENT", "ENTERTAINMENT_MUSIC_AND_AUDIO"),         "flag"),
    ("amazon.com",    ("GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES"), "flag"),
    ("walmart.com",   ("GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_SUPERSTORES"), "flag"),
    ("target.com",    ("GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_SUPERSTORES"), "flag"),
]

# (c) Keyword token → category. Matched as a WHOLE token (word boundaries, not substrings,
# so 'lime' can't hit 'sublime') against merchant_name + name + original_description. A
# single keyword is the weakest signal (it can name a sub-brand, e.g. 'lyft' on a bikeshare
# charge), so all keyword rules are "flag".
KEYWORD_RULES: list[tuple[str, tuple[str, str], str]] = [
    ("uber",      ("TRANSPORTATION", "TRANSPORTATION_TAXIS_AND_RIDE_SHARES"), "flag"),
    ("lyft",      ("TRANSPORTATION", "TRANSPORTATION_TAXIS_AND_RIDE_SHARES"), "flag"),
    ("starbucks", ("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE"),                "flag"),
    ("netflix",   ("ENTERTAINMENT", "ENTERTAINMENT_TV_AND_MOVIES"),           "flag"),
    ("spotify",   ("ENTERTAINMENT", "ENTERTAINMENT_MUSIC_AND_AUDIO"),         "flag"),
    ("shell",     ("TRANSPORTATION", "TRANSPORTATION_GAS"),                   "flag"),
    ("chevron",   ("TRANSPORTATION", "TRANSPORTATION_GAS"),                   "flag"),
]


# ── 3. LLM STAGE (Stage 2) ────────────────────────────────────────────────────
# Local model via Ollama's OpenAI-compatible endpoint. The single source of truth for both
# production runs and the integration tests (so they can't diverge).
LLM_MODEL = "qwen2.5:7b"
LLM_HOST = "http://localhost:11434"
# temperature=0 makes categorization reproducible (same input → same output); seed is
# belt-and-suspenders. Residual run-to-run drift is hardware (float kernels), not sampling.
LLM_SAMPLING = {"temperature": 0, "seed": 0}
# Rows per LLM call. This task is far harder per-row than a flag-audit (each row carries ~8
# signal fields AND the model must pick from a ~100-line taxonomy menu), and qwen2.5:7b
# cannot hold row-alignment across a multi-row batch: integration testing showed it dropping
# rows and SHIFTING decisions (a reason from one row attaching to the next). Even batches of
# 5 drifted; single-row was the only reliable setting. It's slower, but this is a monthly
# batch tool and correctness dominates. Raise it here if a stronger future model allows it.
LLM_BATCH_SIZE = 1
