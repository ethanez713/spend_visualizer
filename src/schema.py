"""Output schema, row selection, provenance columns, and CSV projection.

This module owns three concerns that the rest of the pipeline depends on:

1. **Selection** — which rows the transformer touches. Plaid's
   ``personal_finance_category.confidence_level`` of LOW / MEDIUM (and
   UNKNOWN / missing) is unreliable; HIGH / VERY_HIGH pass through untouched.

2. **Schema** — the 54 base columns (mirrors
   ``transactions/src/fetch_transactions.py`` ``CSV_COLUMNS`` exactly) plus the 12
   new provenance/review columns. ``row_fn`` projects a raw record (with overwritten +
   provenance fields) into a flat CSV row.

3. **Provenance** — ``set_provenance`` writes a correction in place: it copies the
   originals into ``original_*`` columns, overwrites both the flat-derived and the
   nested ``personal_finance_category`` values, sets the ``CORRECTED`` confidence
   sentinel, and records which stage made the change. See the decision rules below.

The output record stays a full raw Plaid object (every original field preserved, so
``persister`` can still persist it) PLUS the 12 nullable provenance/review fields,
present on EVERY output record (null/empty when unchanged) so the schema is uniform.
"""
from __future__ import annotations

import json

from .config import AUDIT_CONFIDENCE_LEVELS

# ── Selection ────────────────────────────────────────────────────────────────
# Confidence levels we re-categorize, from config. Default audits ALL rows (even
# HIGH/VERY_HIGH, which have been observed wrong); narrow ``AUDIT_CONFIDENCE_LEVELS`` in
# config.py to skip trusted rows. A missing/blank level counts as UNKNOWN.
PROCESS_CONFIDENCE = AUDIT_CONFIDENCE_LEVELS

# Sentinel written into pf_category_confidence (and nested confidence_level) on a
# correction, so downstream knows the value is no longer Plaid's own confidence.
CORRECTED_CONFIDENCE = "CORRECTED"


def confidence_of(record: dict) -> str:
    """The record's PFC confidence level, upper-cased; '' when absent."""
    pfc = record.get("personal_finance_category") or {}
    return str(pfc.get("confidence_level") or "").upper()


def should_process(record: dict, levels: set[str] = PROCESS_CONFIDENCE) -> bool:
    """True if this row should be re-categorized (its confidence is in ``levels``).

    A missing/blank confidence counts as UNKNOWN — it is processed when UNKNOWN is in
    ``levels`` (the default), since absent confidence is exactly the unreliable case.
    """
    conf = confidence_of(record)
    if not conf:
        return "UNKNOWN" in levels
    return conf in levels


# ── Provenance + review columns (the new fields) ──────────────────────────────
# Two groups:
#   * provenance (``original_*`` + ``category_update_*``) — record an APPLIED correction
#     (a mechanical 'auto' rule, an accepted review, or an LLM auto-apply). The category is
#     overwritten in place; the originals are preserved here.
#   * review (``category_review_*``) — record a FLAG: a suggestion (from the LLM or a loose
#     mechanical rule) that disagrees with the current category but was NOT auto-applied.
#     The category is left untouched; a human adjudicates these via the review session.
NEW_COLUMNS = [
    "original_pf_category_primary",
    "original_pf_category_detailed",
    "original_pf_category_confidence",
    "category_update_step",        # "mechanical" | "llm" | "review" | "" (none)
    "category_update_reason",      # rule name or LLM/review reason
    "category_update_confidence",  # corrector's confidence (HIGH for entity-id memory; LLM self-rating)
    "category_review_flag",        # "1" when a suggestion is pending human review, else ""
    "category_review_primary",     # suggested primary (not yet applied)
    "category_review_detailed",    # suggested detailed (not yet applied)
    "category_review_reason",      # why the suggestion was raised
    "category_review_confidence",  # suggester's confidence
    "category_review_source",      # "llm" | "mechanical"
]


def ensure_new_columns(record: dict) -> None:
    """Make a record schema-uniform: every provenance column present, defaulting empty."""
    for col in NEW_COLUMNS:
        record.setdefault(col, None if col.startswith("original_") else "")


def set_provenance(record: dict, primary: str, detailed: str,
                   step: str, reason: str, confidence: str) -> bool:
    """Overwrite the record's category in place and record provenance. Returns changed?.

    ``(primary, detailed)`` is the final, validated category. If it differs from the
    record's current (original) PFC value this is a CHANGE:
      1. originals → the three ``original_*`` columns;
      2. overwrite nested ``personal_finance_category.{primary,detailed}``;
      3. confidence → the ``CORRECTED`` sentinel (original kept in ``original_*``);
      4. ``category_update_{step,reason,confidence}`` describe the correcting stage.
    If it equals the original, the provenance columns stay empty (no-op).
    """
    ensure_new_columns(record)
    pfc = dict(record.get("personal_finance_category") or {})
    orig_primary = pfc.get("primary")
    orig_detailed = pfc.get("detailed")
    orig_conf = pfc.get("confidence_level")

    if (primary, detailed) == (orig_primary, orig_detailed):
        return False

    record["original_pf_category_primary"] = orig_primary
    record["original_pf_category_detailed"] = orig_detailed
    record["original_pf_category_confidence"] = orig_conf

    pfc["primary"] = primary
    pfc["detailed"] = detailed
    pfc["confidence_level"] = CORRECTED_CONFIDENCE
    record["personal_finance_category"] = pfc

    record["category_update_step"] = step
    record["category_update_reason"] = reason
    record["category_update_confidence"] = confidence
    return True


def set_review_flag(record: dict, primary: str, detailed: str,
                    reason: str, confidence: str, source: str) -> bool:
    """Record a pending suggestion WITHOUT changing the category. Returns flagged?.

    Used when a suggestion (LLM or a loose mechanical rule) disagrees with the current
    category but isn't trusted enough to auto-apply. The category is left untouched; a
    human adjudicates the flag later (``review`` session), which then either applies it
    (``set_provenance``) or clears it (``clear_review_flag``). A suggestion that equals the
    current category is not a disagreement, so no flag is raised.
    """
    ensure_new_columns(record)
    pfc = record.get("personal_finance_category") or {}
    if (primary, detailed) == (pfc.get("primary"), pfc.get("detailed")):
        return False
    record["category_review_flag"] = "1"
    record["category_review_primary"] = primary
    record["category_review_detailed"] = detailed
    record["category_review_reason"] = reason
    record["category_review_confidence"] = confidence
    record["category_review_source"] = source
    return True


def clear_review_flag(record: dict) -> None:
    """Drop a pending review flag (e.g. after a human accepts or rejects it)."""
    ensure_new_columns(record)
    for col in NEW_COLUMNS:
        if col.startswith("category_review_"):
            record[col] = ""


# ── Base schema (54 cols, mirrors transactions/src/fetch_transactions.py) ──────
BASE_COLUMNS = [
    # --- account identity (blank here: raw store carries no /accounts/get meta) ---
    "institution", "account_id", "account_name", "account_mask",
    "account_official_name", "account_type", "account_subtype",
    # --- transaction core ---
    "transaction_id", "pending", "pending_transaction_id", "date", "authorized_date",
    "datetime", "authorized_datetime", "name", "original_description", "merchant_name",
    "merchant_entity_id", "website", "logo_url", "amount", "iso_currency_code",
    "unofficial_currency_code", "payment_channel", "transaction_type",
    "transaction_code", "check_number", "account_owner",
    # --- categorization (personal_finance_category) ---
    "pf_category_primary", "pf_category_detailed", "pf_category_confidence",
    "pf_category_version", "pf_category_icon_url",
    # --- location ---
    "location_address", "location_city", "location_region", "location_postal_code",
    "location_country", "location_lat", "location_lon", "location_store_number",
    # --- payment metadata ---
    "payment_reference_number", "payment_ppd_id", "payment_payee",
    "payment_by_order_of", "payment_payer", "payment_method", "payment_processor",
    "payment_reason",
    # --- counterparties (primary flattened + full list as JSON) ---
    "counterparty_name", "counterparty_type", "counterparty_entity_id",
    "counterparty_confidence", "counterparties_json",
]

# Derived-CSV column order: the 54 base columns + the 12 provenance/review columns.
COLUMNS = BASE_COLUMNS + NEW_COLUMNS


def _v(x):
    """Scalar → CSV-safe: None → ''; date/datetime → ISO; bool/number as-is."""
    if x is None:
        return ""
    if isinstance(x, bool):
        return x
    if hasattr(x, "isoformat"):
        return x.isoformat()
    return x


def _g(obj, key):
    """Safe get from a possibly-None dict."""
    return None if obj is None else obj.get(key)


def row_fn(record: dict) -> dict:
    """Project one raw (possibly-corrected) record into a flat row for ``derive_csv``.

    Mirrors ``transactions.txn_to_row`` so the 54 base columns match exactly; the
    nested ``personal_finance_category`` reflects any correction (set_provenance writes
    it there), and the 12 provenance/review columns are read from the record's top level.
    Account-identity columns are blank — the raw store carries no /accounts/get meta.
    """
    pfc = record.get("personal_finance_category")
    loc = record.get("location")
    pm = record.get("payment_meta")
    cps = record.get("counterparties") or []
    primary_cp = cps[0] if cps else None
    cp_type = _g(primary_cp, "type")

    row = {
        "institution": "", "account_id": _v(record.get("account_id")),
        "account_name": "", "account_mask": "", "account_official_name": "",
        "account_type": "", "account_subtype": "",

        "transaction_id": _v(record.get("transaction_id")),
        "pending": _v(record.get("pending")),
        "pending_transaction_id": _v(record.get("pending_transaction_id")),
        "date": _v(record.get("date")),
        "authorized_date": _v(record.get("authorized_date")),
        "datetime": _v(record.get("datetime")),
        "authorized_datetime": _v(record.get("authorized_datetime")),
        "name": _v(record.get("name")),
        "original_description": _v(record.get("original_description")),
        "merchant_name": _v(record.get("merchant_name")),
        "merchant_entity_id": _v(record.get("merchant_entity_id")),
        "website": _v(record.get("website")),
        "logo_url": _v(record.get("logo_url")),
        "amount": _v(record.get("amount")),
        "iso_currency_code": _v(record.get("iso_currency_code")),
        "unofficial_currency_code": _v(record.get("unofficial_currency_code")),
        "payment_channel": _v(record.get("payment_channel")),
        "transaction_type": _v(record.get("transaction_type")),
        "transaction_code": _v(record.get("transaction_code")),
        "check_number": _v(record.get("check_number")),
        "account_owner": _v(record.get("account_owner")),

        "pf_category_primary": _v(_g(pfc, "primary")),
        "pf_category_detailed": _v(_g(pfc, "detailed")),
        "pf_category_confidence": _v(_g(pfc, "confidence_level")),
        "pf_category_version": _v(_g(pfc, "version")),
        "pf_category_icon_url": _v(record.get("personal_finance_category_icon_url")),

        "location_address": _v(_g(loc, "address")),
        "location_city": _v(_g(loc, "city")),
        "location_region": _v(_g(loc, "region")),
        "location_postal_code": _v(_g(loc, "postal_code")),
        "location_country": _v(_g(loc, "country")),
        "location_lat": _v(_g(loc, "lat")),
        "location_lon": _v(_g(loc, "lon")),
        "location_store_number": _v(_g(loc, "store_number")),

        "payment_reference_number": _v(_g(pm, "reference_number")),
        "payment_ppd_id": _v(_g(pm, "ppd_id")),
        "payment_payee": _v(_g(pm, "payee")),
        "payment_by_order_of": _v(_g(pm, "by_order_of")),
        "payment_payer": _v(_g(pm, "payer")),
        "payment_method": _v(_g(pm, "payment_method")),
        "payment_processor": _v(_g(pm, "payment_processor")),
        "payment_reason": _v(_g(pm, "reason")),

        "counterparty_name": _v(_g(primary_cp, "name")),
        "counterparty_type": str(cp_type) if cp_type is not None else "",
        "counterparty_entity_id": _v(_g(primary_cp, "entity_id")),
        "counterparty_confidence": _v(_g(primary_cp, "confidence_level")),
        "counterparties_json": json.dumps(cps, default=str) if cps else "",
    }
    for col in NEW_COLUMNS:
        row[col] = _v(record.get(col))
    return row


# ── Flagged-rows worklist (the dedicated review file) ─────────────────────────
# A compact, human-readable CSV of every row carrying a pending review flag, so the
# flagged rows can be triaged in bulk (in a spreadsheet, or via ``--review``). Far fewer
# columns than the full store — just what's needed to decide a category at a glance.
FLAG_COLUMNS = [
    "transaction_id", "date", "merchant_name", "name", "amount",
    "current_primary", "current_detailed", "current_confidence",
    "suggested_primary", "suggested_detailed",
    "review_source", "review_confidence", "review_reason",
]


def flag_row_fn(record: dict) -> dict:
    """Project one flagged record into a flat worklist row (``current`` vs ``suggested``)."""
    pfc = record.get("personal_finance_category") or {}
    return {
        "transaction_id": _v(record.get("transaction_id")),
        "date": _v(record.get("date")),
        "merchant_name": _v(record.get("merchant_name")),
        "name": _v(record.get("name")),
        "amount": _v(record.get("amount")),
        "current_primary": _v(pfc.get("primary")),
        "current_detailed": _v(pfc.get("detailed")),
        "current_confidence": _v(pfc.get("confidence_level")),
        "suggested_primary": _v(record.get("category_review_primary")),
        "suggested_detailed": _v(record.get("category_review_detailed")),
        "review_source": _v(record.get("category_review_source")),
        "review_confidence": _v(record.get("category_review_confidence")),
        "review_reason": _v(record.get("category_review_reason")),
    }


# ── Schema description for the LLM system prompt ──────────────────────────────
# (1) of the user-mandated system-prompt contents: what a row is + what each signal
# means. The taxonomy block (2) and output rules (3) are assembled in llm.py.
SCHEMA_PROMPT = """\
Each row is ONE bank or credit-card transaction. Re-categorize it using ALL of these
signals together (richest identity signals first):
  - merchant_name        : cleaned merchant name (most reliable identity).
  - merchant_entity_id   : Plaid's stable merchant id — same id = same merchant.
  - counterparties       : the parties involved (a type=merchant entry is a clean merchant).
  - name / original_description : the raw bank text (POS prefixes like 'TST*' = restaurant).
  - website              : the merchant's domain (a category hint).
  - payment_channel      : 'in store', 'online', or 'other'.
  - location             : city / region (context only).
  - amount               : POSITIVE = money OUT (a purchase); negative = money in / a refund.
  - current pf_category  : Plaid's existing primary/detailed (may be wrong at ANY confidence).
A 'mechanical suggestion' from a deterministic rule may also be shown; weigh it, but you
decide. Identify the transaction's real merchant/purpose, then pick the best category.\
"""
