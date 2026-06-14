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
#                       DISCOURAGED: qwen2.5:7b's self-reported confidence is anti-calibrated
#                       — in the 2026-06 audit its HIGH-confidence suggestions were WRONG 76%
#                       of the time (no better than MEDIUM/LOW). Its confidence token carries
#                       no usable signal, so do not gate auto-apply on it; prefer "flag".
#   "final"           — the LLM overwrites whenever it disagrees on an untrusted row (legacy;
#                       trusted rows are still only flagged). Not recommended.
LLM_AUTHORITY = "flag"

# Plaid confidence levels we treat as trusted: the LLM may NEVER auto-change these (only
# flag them), under any LLM_AUTHORITY. Mechanical 'auto' rules still apply to them.
TRUSTED_CONFIDENCE_LEVELS = {"HIGH", "VERY_HIGH"}

# ── SIGN GUARD — drop sign-impossible LLM suggestions before they become flags ─
# Plaid's amount convention: a POSITIVE amount is money LEAVING the account (a debit —
# purchase, payment, transfer out); a NEGATIVE amount is money ARRIVING (a credit —
# refund, deposit, paycheck, transfer in). These INFLOW primaries are money-in ONLY, so a
# positive-amount suggestion into one is sign-impossible. qwen2.5:7b made this exact error
# repeatedly — it tried to label outgoing brokerage BUY purchases INCOME_WAGES with the
# reason "positive amount, indicating income." transformer._sign_violation drops such LLM
# suggestions before they reach the review worklist (a deterministic guard survives prompt
# drift and model swaps; the prompt's AMOUNT SIGN rule improves the model's *correct*
# suggestions too — belt and suspenders).
#
# Deliberately ASYMMETRIC: the reverse (a negative amount → a spend primary) is NOT a
# violation. A refund legitimately carries a negative amount while keeping the merchant's
# normal spend category (a returned purchase stays GENERAL_MERCHANDISE, not INCOME), and
# LOAN_PAYMENTS_CREDIT_CARD_PAYMENT is negative on depository accounts — so a
# "negative-on-spend" rule would suppress correct re-categorizations.
INFLOW_PRIMARIES = {"INCOME", "TRANSFER_IN"}

# ── FLAG THRESHOLD — suppress intra-tier-1 laterals ───────────────────────────
# A suggestion that keeps the same tier-1 primary and only changes the detailed (e.g.
# FOOD_AND_DRINK_FAST_FOOD ↔ _RESTAURANT, _COFFEE ↔ _RESTAURANT) does not move tier-1
# spend analysis but still costs a human review. qwen2.5:7b produced many such low-value
# flags (demoting coffee/fast-food chains and a grocery co-op to "restaurant" on a bare
# merchant hunch). By default these intra-primary laterals are NOT flagged. Set True only if you
# care about tier-2 (detailed-level) granularity. Mechanical 'auto' rules and LLM
# auto-applies are unaffected — only review FLAGS are suppressed (see transformer._decide).
FLAG_INTRA_PRIMARY_LATERALS = False


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
# charge), so single-keyword rules are "flag". A fully-qualified, distinctive merchant
# PHRASE (matched as one contiguous token run) is unambiguous, so it MAY be "auto" — add
# your own such rules locally (none ship by default; personal/local merchants stay out of
# the public defaults — keep them in your private config).
KEYWORD_RULES: list[tuple[str, tuple[str, str], str]] = [
    ("uber",      ("TRANSPORTATION", "TRANSPORTATION_TAXIS_AND_RIDE_SHARES"), "flag"),
    ("lyft",      ("TRANSPORTATION", "TRANSPORTATION_TAXIS_AND_RIDE_SHARES"), "flag"),
    ("starbucks", ("FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE"),                "flag"),
    ("netflix",   ("ENTERTAINMENT", "ENTERTAINMENT_TV_AND_MOVIES"),           "flag"),
    ("spotify",   ("ENTERTAINMENT", "ENTERTAINMENT_MUSIC_AND_AUDIO"),         "flag"),
    ("shell",     ("TRANSPORTATION", "TRANSPORTATION_GAS"),                   "flag"),
    ("chevron",   ("TRANSPORTATION", "TRANSPORTATION_GAS"),                   "flag"),
]


# ── PERSONAL RULE OVERLAY (kept OUT of this public repo) ───────────────────────
# Personal / local-merchant rules (e.g. a neighborhood co-op) reveal where you live
# and aren't useful to anyone else, so they live under the DATA ROOT, never in this
# committed file. Optional JSON at
#   <data_root>/plaid_category_transformer/config/personal_rules.json
# extends the generic tables above. Shape (trust is "auto" | "flag"):
#   {"pos_prefix": [["COT*XYZ", ["TRAVEL", "TRAVEL_FLIGHTS"], "auto"]],
#    "website":    [["example.com", ["SHOPPING", "..."], "flag"]],
#    "keyword":    [["takoma park silver spring",
#                    ["FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES"], "auto"]]}
# Personal rules are PREPENDED (first-match-wins, so your local knowledge takes
# precedence); any rule with an invalid taxonomy pair is dropped at apply time, same
# as the built-ins. Missing/malformed file → no-op (fail soft), so a fresh clone with
# no data root still runs on the generic rules alone.
def _load_personal_rules() -> dict[str, list]:
    import json
    from .paths import DATA_ROOT

    out: dict[str, list] = {"pos_prefix": [], "website": [], "keyword": []}
    path = DATA_ROOT / "plaid_category_transformer" / "config" / "personal_rules.json"
    if not path.is_file():
        return out
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return out
    for key in out:
        for entry in data.get(key, []) or []:
            try:
                pattern, (primary, detailed), trust = entry
            except (ValueError, TypeError):
                continue
            out[key].append((pattern, (primary, detailed), trust))
    return out


_PERSONAL_RULES = _load_personal_rules()
POS_PREFIX_RULES = _PERSONAL_RULES["pos_prefix"] + POS_PREFIX_RULES
WEBSITE_RULES = _PERSONAL_RULES["website"] + WEBSITE_RULES
KEYWORD_RULES = _PERSONAL_RULES["keyword"] + KEYWORD_RULES


# ── 3. LLM STAGE (Stage 2) ────────────────────────────────────────────────────
# Whether the local-LLM review stage runs by DEFAULT (no CLI flag given). Turned OFF after
# the 2026-06 audit: qwen2.5:7b was too noisy to be worth the review time — 23% flag
# precision, and it couldn't reliably read the amount-sign convention even with an explicit
# prompt rule (see LLM_ASSESSMENT.md). The deterministic rules + sign guard still run on
# EVERY run; the strong periodic review is now the out-of-band Claude ritual
# (src/claude_audit.py — categorize.py --claude-export / --claude-apply). Flip to True (or
# pass --llm) to re-enable the local reviewer once a bigger/better local model is available;
# the sign guard is model-agnostic and will protect it too.
LLM_ENABLED_BY_DEFAULT = False

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
