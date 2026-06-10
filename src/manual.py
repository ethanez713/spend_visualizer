"""Stage 3 — manual edit intents: a durable, replayable log of human category edicts.

A manual edit is never written into the categorized store directly. It is appended to
``data/manual_edits.jsonl`` as an INTENT, and this stage re-applies every intent on EVERY
run, after the mechanical rules and the LLM. That makes edits sticky by construction: a
``--full`` re-audit, a changed upstream row, or a rebuilt store cannot clobber them — the
replay re-asserts each intent over whatever the earlier stages decided. Rows covered by an
intent skip Stages 1–2 entirely (the verdict is predetermined; no LLM call is wasted).

Two scopes in v1 (the ``match`` block is the extensibility point for richer predicates):
  * ``transaction`` — pin one row by ``transaction_id``;
  * ``merchant``    — pin every row of a merchant: ``merchant_entity_id`` when Plaid
    provides one, plus a ``normalize_merchant`` name fallback for rows without it.
    Conflicting entity ids veto a name match (same name ≠ same merchant).

Precedence is SPECIFICITY first (a transaction intent beats a merchant intent on its row —
a hand-marked one-off must survive a later merchant-wide edict), then RECENCY (the latest
appended intent wins within a scope). A ``revoke`` entry retires an earlier intent; a row
that loses its (only) covering intent is restored to its pre-manual category and stamped
``HASH_PENDING_REVOKED`` so the next run re-audits it from scratch — the pipeline decides
again. Replay validates every entry and skips unknown shapes with a warning (forward
compatibility: an older pipeline must never apply a newer intent more broadly than meant).

The log is append-only and lives under ``data/`` (committed + Drive-pushed) like the store
itself: it is SOURCE data the store is rebuilt from, not a runtime log. Each entry also
snapshots the row's signals and what the machines believed at edit time, so accumulated
intents double as labeled examples for the periodic rule/LLM analysis. This stage never
touches merchant memory or the rule tables — rules change only via that periodic,
human-driven analysis (``tools/analyze_edits.py``), not automatically per run.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import datetime

from .incremental import HASH_PENDING_REVOKED, SOURCE_HASH_FIELD
from .pfc_taxonomy import is_valid
from .rules import normalize_merchant
from .schema import clear_review_flag, set_provenance

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_EDITS = os.path.join(_PROJECT_ROOT, "data", "manual_edits.jsonl")

# The match keys each scope understands. An intent whose match block carries any OTHER
# key was written by a future version with richer predicates — skip it (with a warning)
# rather than applying a broader match than the author intended.
_KNOWN_MATCH_KEYS = {
    "transaction": {"transaction_id"},
    "merchant": {"merchant_entity_id", "merchant_name_normalized"},
}


def _warn(msg: str) -> None:
    print(f"  manual: {msg}", file=sys.stderr)


# ── Intent construction ───────────────────────────────────────────────────────

def snapshot_of(record: dict) -> dict:
    """Freeze the row's signals + what the machines believed, at edit time.

    This is what makes each intent a self-contained labeled example for the periodic
    analysis: ``before_*`` is the value being overridden and which stage authored it;
    ``plaid_*`` is Plaid's true original; ``review_pending_*`` is any not-yet-adjudicated
    suggestion (so the LLM can be scored: did it flag the right answer before the human
    stepped in?).
    """
    pfc = record.get("personal_finance_category") or {}
    return {
        "merchant_name": record.get("merchant_name"),
        "merchant_entity_id": record.get("merchant_entity_id"),
        "name": record.get("name"),
        "original_description": record.get("original_description"),
        "website": record.get("website"),
        "amount": record.get("amount"),
        "payment_channel": record.get("payment_channel"),
        "before_primary": pfc.get("primary"),
        "before_detailed": pfc.get("detailed"),
        "before_confidence": pfc.get("confidence_level"),
        "before_step": record.get("category_update_step") or "",
        "before_reason": record.get("category_update_reason") or "",
        "plaid_primary": record.get("original_pf_category_primary") or pfc.get("primary"),
        "plaid_detailed": record.get("original_pf_category_detailed") or pfc.get("detailed"),
        "plaid_confidence": (record.get("original_pf_category_confidence")
                             or pfc.get("confidence_level")),
        "review_pending_primary": record.get("category_review_primary") or "",
        "review_pending_detailed": record.get("category_review_detailed") or "",
        "review_pending_source": record.get("category_review_source") or "",
    }


def build_intent(*, scope: str, primary: str, detailed: str, record: dict,
                 note: str = "", source: str = "cli") -> dict:
    """Build one edit intent from a representative record. Raises ValueError on bad input.

    ``record`` supplies the match keys (transaction id, or the merchant's entity id +
    normalized name) and the analysis snapshot. The caller appends the result via
    ``append_intent``; nothing is applied until the next replay.
    """
    if not is_valid(primary, detailed):
        raise ValueError(f"{primary}/{detailed} is not a valid PFC category")
    if scope == "transaction":
        tid = record.get("transaction_id")
        if not tid:
            raise ValueError("transaction-scope intent needs a transaction_id")
        match: dict = {"transaction_id": tid}
    elif scope == "merchant":
        ent = record.get("merchant_entity_id")
        norm = normalize_merchant(record.get("merchant_name") or "")
        if not ent and not norm:
            raise ValueError("merchant-scope intent needs a merchant_entity_id "
                             "or a merchant_name")
        match = {}
        if ent:
            match["merchant_entity_id"] = ent
        if norm:
            match["merchant_name_normalized"] = norm
    else:
        raise ValueError(f"unknown scope {scope!r} (expected transaction|merchant)")
    return {
        "id": uuid.uuid4().hex[:8],
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "action": "edit",
        "source": source,
        "scope": scope,
        "match": match,
        "set": {"primary": primary, "detailed": detailed},
        "note": note,
        "snapshot": snapshot_of(record),
    }


def build_revoke(revokes_id: str, *, note: str = "", source: str = "cli") -> dict:
    """A tombstone retiring an earlier intent: the row reverts and re-audits next run."""
    return {
        "id": uuid.uuid4().hex[:8],
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "action": "revoke",
        "source": source,
        "revokes": revokes_id,
        "note": note,
    }


# ── The log ───────────────────────────────────────────────────────────────────

def append_intent(path: str, intent: dict) -> dict:
    """Append one intent to the JSONL log (creating it on first use)."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(intent) + "\n")
    return intent


def load_intents(path: str) -> list[dict]:
    """Read the raw log in file order; a corrupt line is skipped LOUDLY, never fatal."""
    if not path or not os.path.isfile(path):
        return []
    out: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                _warn(f"skipping corrupt line {lineno} in {path}")
    return out


def resolve_intents(entries: list[dict]) -> list[dict]:
    """Validate raw log entries into the applicable intents, in append order.

    Drops: revoke tombstones and the intents they retire; entries with an unknown
    action/scope or unknown match keys (future predicates — warned, never guessed at);
    matches missing their required key; and ``set`` pairs that fail the vendored
    taxonomy. Append order is preserved — it is the recency tiebreak at apply time.
    """
    revoked = {e.get("revokes") for e in entries if e.get("action") == "revoke"}
    out: list[dict] = []
    for e in entries:
        if e.get("action") == "revoke":
            continue
        eid = e.get("id", "?")
        if e.get("action", "edit") != "edit":
            _warn(f"intent {eid}: unknown action {e.get('action')!r} — skipped")
            continue
        if eid in revoked:
            continue
        scope = e.get("scope")
        known = _KNOWN_MATCH_KEYS.get(scope)
        match = e.get("match") or {}
        if known is None:
            _warn(f"intent {eid}: unknown scope {scope!r} — skipped")
            continue
        unknown = set(match) - known
        if unknown:
            _warn(f"intent {eid}: unrecognized match field(s) {sorted(unknown)} "
                  "(written by a newer version?) — skipped")
            continue
        if scope == "transaction" and not match.get("transaction_id"):
            _warn(f"intent {eid}: transaction scope without a transaction_id — skipped")
            continue
        if scope == "merchant" and not (match.get("merchant_entity_id")
                                        or match.get("merchant_name_normalized")):
            _warn(f"intent {eid}: merchant scope without an entity id or name — skipped")
            continue
        tgt = e.get("set") or {}
        if not is_valid(tgt.get("primary"), tgt.get("detailed")):
            _warn(f"intent {eid}: {tgt.get('primary')}/{tgt.get('detailed')} is not a "
                  "valid PFC pair — skipped")
            continue
        out.append(e)
    return out


# ── Matching ──────────────────────────────────────────────────────────────────

class ManualIndex:
    """Resolved intents indexed for O(1) per-row matching.

    ``match`` precedence: a transaction-scope intent on the row's id always wins
    (specificity); otherwise the LATEST-appended merchant-scope intent whose entity id
    or normalized name matches — except that a name match is vetoed when both sides
    carry entity ids that differ (same normalized name, demonstrably different merchant).
    """

    def __init__(self, intents: list[dict]):
        self.intents = intents
        self.by_txn: dict[str, dict] = {}
        self._by_ent: dict[str, tuple[int, dict]] = {}
        self._by_name: dict[str, tuple[int, dict]] = {}
        for seq, it in enumerate(intents):
            m = it.get("match") or {}
            if it["scope"] == "transaction":
                self.by_txn[m["transaction_id"]] = it          # last wins
            else:
                if m.get("merchant_entity_id"):
                    self._by_ent[m["merchant_entity_id"]] = (seq, it)
                if m.get("merchant_name_normalized"):
                    self._by_name[m["merchant_name_normalized"]] = (seq, it)

    def __len__(self) -> int:
        return len(self.intents)

    def match(self, record: dict) -> dict | None:
        it = self.by_txn.get(record.get("transaction_id"))
        if it is not None:
            return it
        candidates: list[tuple[int, dict]] = []
        ent = record.get("merchant_entity_id")
        if ent and ent in self._by_ent:
            candidates.append(self._by_ent[ent])
        norm = normalize_merchant(record.get("merchant_name") or "")
        if norm and norm in self._by_name:
            seq, name_it = self._by_name[norm]
            it_ent = (name_it.get("match") or {}).get("merchant_entity_id")
            if not (it_ent and ent and it_ent != ent):       # conflicting ids veto
                candidates.append((seq, name_it))
        if not candidates:
            return None
        return max(candidates, key=lambda c: c[0])[1]        # latest appended wins


# ── Replay (the stage) ────────────────────────────────────────────────────────

def apply_manual_edits(store: dict[str, dict], index: ManualIndex) -> dict:
    """Replay the resolved intents over the FULL store (mutates in place).

    Idempotent: a row already at its intended category is a no-op (``set_provenance``
    declines equal values), so steady-state runs cause no churn. A covered row's pending
    review flag is cleared — the human has spoken with higher authority. A row whose
    manual override lost its covering intent (revoked, or the log was rewound) is
    REVERTED to its pre-manual category and stamped for a full re-audit next run.

    Returns a summary: ``applied`` (category-log entries), ``already`` (covered, value
    already right), ``reverted`` (transaction ids), ``orphans`` (transaction-scope intent
    ids whose row is gone — kept in the log for analysis, inert at replay).
    """
    applied: list[dict] = []
    reverted: list[str] = []
    n_already = 0
    for tid, rec in store.items():
        it = index.match(rec)
        if it is None:
            if rec.get("category_update_step") == "manual":
                _revert_manual(rec)
                reverted.append(tid)
            continue
        tgt = it["set"]
        pfc = rec.get("personal_finance_category") or {}
        before = (pfc.get("primary"), pfc.get("detailed"))
        if set_provenance(rec, tgt["primary"], tgt["detailed"], "manual",
                          f"manual:{it['scope']}:{it['id']}", "HIGH"):
            applied.append({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "transaction_id": tid,
                "original_primary": before[0], "original_detailed": before[1],
                "new_primary": tgt["primary"], "new_detailed": tgt["detailed"],
                "step": "manual", "reason": f"manual:{it['scope']}:{it['id']}",
            })
        else:
            n_already += 1
        if rec.get("category_review_flag"):
            clear_review_flag(rec)
    orphans = [it["id"] for txn_id, it in index.by_txn.items() if txn_id not in store]
    return {"applied": applied, "already": n_already,
            "reverted": reverted, "orphans": orphans}


# ── Interactive edit session (the --edit CLI) ────────────────────────────────

def search_rows(store: dict[str, dict], query: str,
                limit: int = 10) -> list[tuple[str, dict]]:
    """Find rows by ``id:<transaction_id>`` (exact) or a case-insensitive substring
    of merchant_name / name / original_description."""
    q = query.strip()
    if q.lower().startswith("id:"):
        tid = q[3:].strip()
        rec = store.get(tid)
        return [(tid, rec)] if rec else []
    q = q.lower()
    out: list[tuple[str, dict]] = []
    for tid, rec in store.items():
        text = " ".join(str(rec.get(f) or "") for f in
                        ("merchant_name", "name", "original_description")).lower()
        if q in text:
            out.append((tid, rec))
            if len(out) >= limit:
                break
    return out


def _render_row(rec: dict, out) -> None:
    pfc = rec.get("personal_finance_category") or {}
    out(f"  merchant_name : {rec.get('merchant_name')}")
    out(f"  name          : {rec.get('name')}")
    out(f"  original_desc : {rec.get('original_description')}")
    out(f"  amount        : {rec.get('amount')}    date: {rec.get('date')}")
    out(f"  current       : {pfc.get('primary')} / {pfc.get('detailed')} "
        f"({pfc.get('confidence_level')})")


def run_edit_session(store: dict[str, dict], edits_path: str, *,
                     input_fn=input, out=print) -> int:
    """Interactive intent capture: search → pick row → pick category → scope → note.

    Appends to the intent log only — the caller replays the full log afterwards so the
    session's edits land in the store immediately. Also supports ``list`` (current
    intents) and ``revoke <id>``. Returns the number of entries appended. No-ops off a
    TTY so a piped run can't block; ``input_fn``/``out`` are injectable for tests.
    """
    from .review import prompt_pick_category

    if not sys.stdin.isatty():
        out("stdin is not a TTY — skipping the interactive edit session.")
        return 0
    out("Manual edit session. Enter a merchant/text search, 'id:<transaction_id>', "
        "'list' (current intents), 'revoke <id>', or 'q' to finish.")
    appended = 0
    while True:
        cmd = input_fn("edit> ").strip()
        if not cmd:
            continue
        if cmd.lower() == "q":
            return appended
        if cmd.lower() == "list":
            for it in resolve_intents(load_intents(edits_path)):
                tgt = (it["match"].get("transaction_id")
                       or (it.get("snapshot") or {}).get("merchant_name")
                       or it["match"].get("merchant_name_normalized"))
                out(f"  {it['id']}  [{it['scope']}] {tgt} → "
                    f"{it['set']['primary']}/{it['set']['detailed']}  {it.get('note', '')}")
            continue
        if cmd.lower().startswith("revoke "):
            rid = cmd.split(None, 1)[1].strip()
            if rid not in {it["id"] for it in resolve_intents(load_intents(edits_path))}:
                out(f"  no applicable intent with id {rid!r} (see 'list').")
                continue
            append_intent(edits_path, build_revoke(rid, source="cli"))
            appended += 1
            out(f"  revoked {rid} — the row reverts and re-audits on the next run.")
            continue

        matches = search_rows(store, cmd)
        if not matches:
            out("  no matching rows.")
            continue
        for i, (tid, rec) in enumerate(matches):
            pfc = rec.get("personal_finance_category") or {}
            out(f"  {i:2}. {rec.get('date')}  {rec.get('merchant_name') or rec.get('name')}"
                f"  {rec.get('amount')}  [{pfc.get('primary')}/{pfc.get('detailed')}]")
        raw = input_fn("  row # (or blank to cancel): ").strip()
        if not raw.isdigit() or not (0 <= int(raw) < len(matches)):
            continue
        tid, rec = matches[int(raw)]
        _render_row(rec, out)
        picked = prompt_pick_category(input_fn, out)
        if picked is None:
            out("  (cancelled)")
            continue
        scope = "merchant" if input_fn(
            "  scope — [t]his transaction only / [m]erchant-wide: "
        ).strip().lower().startswith("m") else "transaction"
        note = input_fn("  note (why?): ").strip()
        try:
            intent = build_intent(scope=scope, primary=picked[0], detailed=picked[1],
                                  record=rec, note=note, source="cli")
        except ValueError as e:
            out(f"  not saved: {e}")
            continue
        append_intent(edits_path, intent)
        appended += 1
        out(f"  saved intent {intent['id']} ({scope}) → {picked[0]}/{picked[1]}")


def _revert_manual(rec: dict) -> None:
    """Roll a no-longer-covered manual override back to the pre-correction category.

    Restores from ``original_*`` (Plaid's true original — written exactly once even
    across re-corrections) and stamps ``HASH_PENDING_REVOKED``: this stage runs after
    the audit, so it cannot re-run Stages 1–2 itself; the stamp forces the next run to
    re-derive the row from pristine input (any auto rule will simply re-apply then).
    """
    pfc = dict(rec.get("personal_finance_category") or {})
    pfc["primary"] = rec.get("original_pf_category_primary")
    pfc["detailed"] = rec.get("original_pf_category_detailed")
    pfc["confidence_level"] = rec.get("original_pf_category_confidence")
    rec["personal_finance_category"] = pfc
    for col in ("original_pf_category_primary", "original_pf_category_detailed",
                "original_pf_category_confidence"):
        rec[col] = None
    rec["category_update_step"] = ""
    rec["category_update_reason"] = ""
    rec["category_update_confidence"] = ""
    rec[SOURCE_HASH_FIELD] = HASH_PENDING_REVOKED
