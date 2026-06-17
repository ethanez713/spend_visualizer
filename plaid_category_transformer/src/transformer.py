"""Engine + CLI: audit & re-categorize Plaid rows (ALL confidence levels), then persist.

Pipeline:
  1. **Select** rows to audit by ``personal_finance_category.confidence_level``. The
     default (``config.AUDIT_CONFIDENCE_LEVELS``) audits EVERY row — even HIGH / VERY_HIGH,
     which have been observed wrong.
  2. **Stage 1 — mechanical rules** (``rules``): deterministic, uses all signals. A
     ``trust="auto"`` hit (entity-id memory, specific COT* prefixes) overwrites in place; a
     ``trust="flag"`` hit (loose keyword/website/TST*, name-memory) is only a suggestion.
  3. **Stage 2 — local LLM** (``llm``): a REVIEWER, not an author. It runs on all selected
     rows and sees the schema + vendored taxonomy + signals + the mechanical suggestion, but
     by default it never overwrites — it only flags disagreements. Skips if Ollama is down.
  4. **Authority** (``_decide`` + ``config.LLM_AUTHORITY``): most-trusted signal wins.
     Trusted Plaid labels (HIGH/VERY_HIGH) are never auto-changed by the LLM, only flagged.
  5. **Provenance / review** (``schema``): an APPLIED change saves originals to
     ``original_*``, overwrites in place, sets the ``CORRECTED`` sentinel and the stage; a
     FLAG writes the ``category_review_*`` columns and leaves the category untouched.
  6. **Persist** via ``persister``: ``save_jsonl`` + ``derive_csv`` + optional Drive push.

Flagged rows are adjudicated later by the interactive ``review`` session (accept → applies
the suggestion + teaches merchant memory so it's a ``trust="auto"`` hit next run; reject;
or re-pick). The ``category_update_step`` of an applied change is ``"mechanical"`` (a rule),
``"llm"`` (an LLM auto-apply), ``"review"`` (a human-accepted flag), or ``"manual"`` (a
replayed edit intent).

A final stage (``manual``) replays the human edit intents from ``data/manual_edits.jsonl``
over the full store every run — the highest authority. Rows covered by an intent skip
Stages 1–2 entirely (the verdict is predetermined; no LLM call is wasted), and the replay
makes manual edits survive ``--full`` re-audits and upstream row changes.
"""
from __future__ import annotations

import argparse
import json
import lzma
import os
import sys
from collections import namedtuple
from datetime import datetime

import persister

from .claude_audit import apply_verdicts, export_bundle, load_verdicts
from .config import (
    FLAG_INTRA_PRIMARY_LATERALS,
    INFLOW_PRIMARIES,
    LLM_AUTHORITY,
    LLM_ENABLED_BY_DEFAULT,
    TRUSTED_CONFIDENCE_LEVELS,
)
from .incremental import HASH_PENDING_LLM, SOURCE_HASH_FIELD, classify, source_hash
from .llm import CategoryLLM
from .manual import DEFAULT_EDITS, ManualIndex, apply_manual_edits, load_intents, \
    resolve_intents
from .paths import DATA_DIR as _DATA_DIR, DATA_ROOT as _DATA_ROOT
from .rules import DEFAULT_MEMORY, MerchantMemory, RuleHit, apply_rules
from .schema import (
    COLUMNS,
    FLAG_COLUMNS,
    PROCESS_CONFIDENCE,
    VALID_CONFIDENCE_LEVELS,
    confidence_of,
    ensure_new_columns,
    flag_row_fn,
    row_fn,
    set_provenance,
    set_review_flag,
    should_process,
    stamp_audited_at,
)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SECRETS_DIR = os.path.join(_PROJECT_ROOT, ".secrets")

# Data lives OUTSIDE the repo (paths.DATA_ROOT mirrors the monorepo layout);
# only secrets/state stay in .secrets/.
DEFAULT_INPUT = str(_DATA_ROOT / "transactions" / "data" / "transactions.jsonl")
DEFAULT_OUT_JSONL = str(_DATA_DIR / "transactions_categorized.jsonl")
DEFAULT_OUT_CSV = str(_DATA_DIR / "transactions_categorized.csv")
DEFAULT_FLAGS_CSV = str(_DATA_DIR / "flagged_for_review.csv")
DEFAULT_LOG = os.path.join(_SECRETS_DIR, "category_log.jsonl")
# Claude-ritual scratch files (local-only, gitignored, 0600): the queue Claude reads and
# the verdicts it writes back. Financial data — kept in .secrets, never the data root.
DEFAULT_CLAUDE_QUEUE = os.path.join(_SECRETS_DIR, "claude_audit_queue.jsonl")
DEFAULT_CLAUDE_SCAN = os.path.join(_SECRETS_DIR, "claude_audit_scan.json")
DEFAULT_CLAUDE_VERDICTS = os.path.join(_SECRETS_DIR, "claude_audit_verdicts.jsonl")

DRIVE_FOLDER = "transactions_archive"
DRIVE_JSONL_NAME = "transactions_categorized.jsonl"


# ── Input loading ──────────────────────────────────────────────────────────────

def load_input(path: str) -> dict[str, dict]:
    """Load the input store into ``{transaction_id: record}``.

    ``.xz`` → the ``transactions`` raw store (xz-compressed JSONL); anything else →
    plain JSONL via ``persister.load_jsonl`` (the canonical persister store).
    """
    if path.endswith(".xz"):
        store: dict[str, dict] = {}
        with lzma.open(path, "rt", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                store[rec["transaction_id"]] = rec
        return store
    return persister.load_jsonl(path)


# ── Decision ────────────────────────────────────────────────────────────────
# A resolved outcome for one row. ``action`` is one of:
#   "apply" — overwrite the category in place (record provenance).
#   "flag"  — leave the category; record a suggestion for human review.
#   "none"  — no disagreement; nothing to do.
Decision = namedtuple("Decision", "action primary detailed source reason confidence")


def _sign_violation(amount, primary: str | None) -> bool:
    """True when categorizing a row with this ``amount`` under ``primary`` is sign-impossible.

    Plaid's amount convention: a POSITIVE amount is money LEAVING the account (a
    debit — purchase/payment/transfer-out); a NEGATIVE amount is money ARRIVING (a
    credit — refund/deposit/income). An INFLOW primary (``INCOME`` / ``TRANSFER_IN``,
    per ``config.INFLOW_PRIMARIES``) is money-in ONLY, so suggesting one for a positive
    amount cannot be right — the local model did exactly this, labelling outgoing
    brokerage BUY purchases ``INCOME_WAGES`` ("positive amount, indicating income"). We
    drop such suggestions before they reach the worklist.

    Deliberately one-directional: a NEGATIVE amount on a spend primary is NOT a
    violation (a refund legitimately keeps the merchant's spend category), so it is
    never suppressed here. A missing/non-numeric amount can't be judged → not a
    violation (let the suggestion flag and a human decide).
    """
    try:
        amt = float(amount)
    except (TypeError, ValueError):
        return False
    return amt > 0 and primary in INFLOW_PRIMARIES


def _decide(record: dict, mech: RuleHit | None, llm_decision,
            *, authority: str = LLM_AUTHORITY) -> Decision:
    """Apply the tiered authority model (see ``config``) to one row.

    Order of authority:
      1. A mechanical ``trust="auto"`` rule (entity-id memory, specific COT* prefixes) that
         differs from the current category → APPLY in place.
      2. Otherwise, take the best DISAGREEING suggestion — the LLM's first (it saw the most
         context), else a loose mechanical ``trust="flag"`` rule. A sign-impossible LLM
         suggestion (``_sign_violation``: an INFLOW primary on a positive/outgoing amount)
         is dropped here, never flagged.
      3. The LLM may AUTO-APPLY its suggestion only on an UNTRUSTED row (Plaid confidence not
         HIGH/VERY_HIGH) and only as far as ``LLM_AUTHORITY`` allows
         (``"final"`` always; ``"apply_when_high"`` only when the LLM is HIGH-confidence).
         Mechanical ``flag`` rules and trusted rows never auto-apply.
      4. A disagreeing suggestion that stays within the current tier-1 primary (an
         intra-primary lateral) is, by default, NOT flagged — it doesn't move tier-1 spend
         analysis (``config.FLAG_INTRA_PRIMARY_LATERALS``). Auto-applies (step 1/3) are
         exempt; only flags are suppressed.
      5. Anything else with a disagreeing suggestion → FLAG for review.
    """
    pfc = record.get("personal_finance_category") or {}
    cur = (pfc.get("primary"), pfc.get("detailed"))
    trusted = confidence_of(record) in TRUSTED_CONFIDENCE_LEVELS

    # 1. Trusted mechanical rule — overwrite in place.
    if mech is not None and mech.trust == "auto" and (mech.primary, mech.detailed) != cur:
        return Decision("apply", mech.primary, mech.detailed, "mechanical",
                        mech.rule_name, mech.confidence)

    # 2. Best disagreeing suggestion. When the LLM ran it is the reviewer of record (it saw
    #    the mechanical suggestion), so its verdict governs: a disagreement is the
    #    suggestion; a concurrence with the current category means NO flag — even if a loose
    #    mechanical rule disagrees, the LLM already weighed and rejected it. The mechanical
    #    'flag' rule is the suggestion only when the LLM gave nothing (no-LLM run, or the
    #    model dropped this row).
    #    A sign-impossible LLM suggestion (e.g. INCOME on a positive/outgoing amount) is
    #    dropped here — treated like a concurrence, so it raises no flag and (per the
    #    LLM-of-record rule above) does not fall through to a mechanical suggestion either.
    sugg: Decision | None = None
    if llm_decision is not None:
        if ((llm_decision.primary, llm_decision.detailed) != cur
                and not _sign_violation(record.get("amount"), llm_decision.primary)):
            sugg = Decision("flag", llm_decision.primary, llm_decision.detailed, "llm",
                            llm_decision.reason, llm_decision.confidence)
    elif mech is not None and (mech.primary, mech.detailed) != cur:
        sugg = Decision("flag", mech.primary, mech.detailed, "mechanical",
                        mech.rule_name, mech.confidence)

    if sugg is None:
        return Decision("none", cur[0], cur[1], "", "", "")

    # 3. Optionally let the LLM auto-apply on an untrusted row.
    if sugg.source == "llm" and not trusted:
        if authority == "final" or (
                authority == "apply_when_high" and str(sugg.confidence).upper() == "HIGH"):
            return sugg._replace(action="apply")

    # 4. Intra-tier-1 lateral: the suggestion keeps the current primary and only changes the
    #    detailed (e.g. FAST_FOOD↔RESTAURANT). It doesn't move tier-1 spend analysis, so by
    #    default it isn't worth a human review — suppress the flag. Auto-applies returned
    #    above are unaffected (an 'auto' grocery-phrase fix is intra-FOOD_AND_DRINK and
    #    must still apply); only FLAGS reach here. See config.FLAG_INTRA_PRIMARY_LATERALS.
    if not FLAG_INTRA_PRIMARY_LATERALS and sugg.primary == cur[0]:
        return Decision("none", cur[0], cur[1], "", "", "")

    # 5. Otherwise flag for human review.
    return sugg


def _build_item(idx: int, record: dict, mech: RuleHit | None) -> dict:
    """Assemble the per-row signal dict handed to the LLM (GLOBAL row_index = ``idx``)."""
    pfc = record.get("personal_finance_category") or {}
    item = {
        "row_index": idx,
        "merchant_name": record.get("merchant_name"),
        "name": record.get("name"),
        "original_description": record.get("original_description"),
        "counterparties": record.get("counterparties") or [],
        "website": record.get("website"),
        "payment_channel": record.get("payment_channel"),
        "amount": record.get("amount"),
        "current_primary": pfc.get("primary"),
        "current_detailed": pfc.get("detailed"),
        "current_confidence": pfc.get("confidence_level"),
    }
    if mech is not None:
        item["suggested_primary"] = mech.primary
        item["suggested_detailed"] = mech.detailed
    return item


# ── Engine (I/O-free; testable) ───────────────────────────────────────────────

def transform(store: dict[str, dict], *, levels=PROCESS_CONFIDENCE,
              memory: MerchantMemory | None = None, llm=None, authority=LLM_AUTHORITY,
              manual: ManualIndex | None = None):
    """Audit ``store`` in place. Returns ``(store, changes, flags)``.

    ``changes`` — rows whose category was APPLIED (mechanical 'auto' rule, or an LLM
    auto-apply when ``LLM_AUTHORITY`` permits). ``flags`` — rows left untouched but carrying
    a pending suggestion for human review (the LLM, or a loose mechanical rule, disagreed).
    ``llm`` is any object with ``categorize(items) -> {row_index: decision}`` (tests inject a
    fake); ``None`` means "no LLM stage" (mechanical rules only). A row covered by a manual
    edit intent (``manual``) skips the audit entirely — its verdict is predetermined, so an
    LLM call (and a flag that would be instantly cleared) would be wasted; the manual stage
    applies the intent after the audit.
    """
    selected = [(tid, rec) for tid, rec in store.items()
                if should_process(rec, levels)
                and (manual is None or manual.match(rec) is None)]

    # Stage 1 — mechanical (build the LLM batch with suggestions attached).
    items: list[dict] = []
    mech_by_index: dict[int, RuleHit] = {}
    for idx, (_tid, rec) in enumerate(selected):
        mech = apply_rules(rec, memory)
        if mech is not None:
            mech_by_index[idx] = mech
        items.append(_build_item(idx, rec, mech))

    # Stage 2 — LLM (reviewer) on all selected rows.
    decisions = llm.categorize(items) if llm is not None else {}

    # Resolve under the authority model: apply, flag, or skip.
    changes: list[dict] = []
    flags: list[dict] = []
    for idx, (tid, rec) in enumerate(selected):
        mech = mech_by_index.get(idx)
        pfc = rec.get("personal_finance_category") or {}
        orig_primary, orig_detailed = pfc.get("primary"), pfc.get("detailed")
        dec = _decide(rec, mech, decisions.get(idx), authority=authority)

        if dec.action == "apply":
            if set_provenance(rec, dec.primary, dec.detailed, dec.source, dec.reason,
                              dec.confidence):
                if memory is not None:
                    memory.remember(rec, dec.primary, dec.detailed)  # → HIGH hit next run
                changes.append({
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "transaction_id": tid,
                    "original_primary": orig_primary, "original_detailed": orig_detailed,
                    "new_primary": dec.primary, "new_detailed": dec.detailed,
                    "step": dec.source, "reason": dec.reason,
                })
        elif dec.action == "flag":
            if set_review_flag(rec, dec.primary, dec.detailed, dec.reason, dec.confidence,
                               dec.source):
                flags.append({
                    "transaction_id": tid,
                    "current_primary": orig_primary, "current_detailed": orig_detailed,
                    "suggested_primary": dec.primary, "suggested_detailed": dec.detailed,
                    "source": dec.source, "reason": dec.reason, "confidence": dec.confidence,
                })

    # Uniform schema: every output record carries the provenance + review columns.
    for rec in store.values():
        ensure_new_columns(rec)

    return store, changes, flags


# ── Logging ────────────────────────────────────────────────────────────────

def write_category_log(path: str, changes: list[dict]) -> None:
    """Append one JSONL line per change to ``.secrets/category_log.jsonl`` (0600)."""
    if not changes:
        return
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, mode=0o700, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        for entry in changes:
            f.write(json.dumps(entry) + "\n")
    try:
        os.chmod(path, 0o600)  # financial data — owner-only
    except OSError:
        pass


def write_flags_file(path: str, store: dict[str, dict]) -> int:
    """Write the dedicated worklist CSV of EVERY row pending review. Returns the count.

    Cumulative, not per-run: it lists every record in ``store`` whose
    ``category_review_flag`` is still set — carried-over flags from past runs plus any
    raised this run — so it is a complete to-do list for bulk corrections (open it in a
    spreadsheet, or adjudicate via ``--review``). A run that clears the last flag rewrites
    the file with just a header. Goes under ``data/`` (committed; formula-injection guarded).
    """
    flagged = {tid: rec for tid, rec in store.items()
               if rec.get("category_review_flag") == "1"}
    persister.derive_csv(flagged, path, FLAG_COLUMNS, row_fn=flag_row_fn)
    return len(flagged)


# ── Drive head adoption (two-writer safety) ──────────────────────────────────

def _adopt_remote_edits(edits_path: str, secrets_dir: str = _SECRETS_DIR) -> None:
    """Union-merge the Drive copy of the manual-edits intent log into the local one.

    Replay reverts any manually-corrected row whose covering intent is missing from
    the LOCAL log (``apply_manual_edits``) — so a machine replaying a stale log would
    silently undo corrections made on the other machine. Adopting the remote log
    first closes that hole: remote entries keep their order (the trunk), local
    entries the remote lacks (by intent ``id``) are re-appended after, preserving
    local order — a rebase. Revoke tombstones merge like any entry; entries without
    an id are kept. The union is what this run replays and pushes, so both
    machines' logs converge.
    """
    from persister import DrivePullError, DriveSync
    try:
        raw = DriveSync("manual_edits.jsonl", folder_name=DRIVE_FOLDER,
                        secrets_dir=secrets_dir).pull()
    except DrivePullError as e:
        sys.exit(
            f"  drive head: STOP — could not read the remote intent log ({e}). "
            "Replaying a possibly-stale local log could revert the other machine's "
            "corrections. Retry when Drive is reachable, or run --no-drive to stay "
            "local."
        )
    if not raw:
        return
    remote_entries = [json.loads(line)
                      for line in raw.decode("utf-8").splitlines() if line.strip()]
    local_entries = load_intents(edits_path)
    remote_ids = {e.get("id") for e in remote_entries if e.get("id")}
    merged = remote_entries + [e for e in local_entries
                               if not e.get("id") or e["id"] not in remote_ids]
    if merged == local_entries:
        return
    tmp = edits_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for entry in merged:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    os.replace(tmp, edits_path)
    print(f"  drive head: adopted {len(merged) - len(local_entries)} remote intent "
          f"entr{'y' if len(merged) - len(local_entries) == 1 else 'ies'} "
          f"into {edits_path}")


def _conflict_log_path(out_jsonl: str) -> str:
    """The adopt-conflict audit log lives next to the categorized store — in the
    data root and OUTSIDE any logs/ dir (logs/ is gitignored; this file must ride
    the data repo's daily git push so losing versions are durable off-machine)."""
    return os.path.join(os.path.dirname(os.path.abspath(out_jsonl)),
                        "adopt_conflicts.jsonl")


def _audit_content_equal(a: dict, b: dict) -> bool:
    """True when two copies of a row carry identical audit content — i.e. they
    differ at most in the recency stamp and collector metadata."""
    strip = ("category_audited_at", "claude_audited_at", "txn_owner")
    return (json.dumps({k: v for k, v in a.items() if k not in strip},
                       sort_keys=True, default=str)
            == json.dumps({k: v for k, v in b.items() if k not in strip},
                          sort_keys=True, default=str))


def _newer_audit_wins(local_rec: dict, remote_rec: dict) -> dict:
    """Conflict policy: the side whose audit content changed more recently wins.

    ``category_audited_at`` is stamped only when audit content actually changes
    (see ``schema.stamp_audited_at``), so "newer stamp" means "newer real work" —
    a local store that is ahead of the Drive head (offline review session, crash
    between save and push, lost push race) keeps its work. Missing stamps sort
    oldest; a tie keeps the REMOTE value (pre-stamp rows: the old behavior).
    ISO-8601 UTC strings compare correctly as strings.
    """
    local_t = local_rec.get("category_audited_at") or ""
    remote_t = remote_rec.get("category_audited_at") or ""
    return local_rec if local_t > remote_t else remote_rec


def _log_adopt_conflicts(conflict_log: str | None, report, prior: dict,
                         remote: dict) -> None:
    """Append BOTH versions of every conflict to a JSONL audit log (data root).

    Nothing is ever silently discarded: even when the resolver picks wrong, the
    losing record's full content is recoverable here — and the data repo's daily
    git push makes the log durable off-machine.
    """
    if not conflict_log or not report.conflicts:
        return
    now = datetime.now().isoformat(timespec="seconds")
    with open(conflict_log, "a", encoding="utf-8") as f:
        for tid in report.conflicts:
            f.write(json.dumps({
                "ts": now,
                "transaction_id": tid,
                "kept": "local" if report.merged[tid] is prior[tid] else "remote",
                "local": prior[tid],
                "remote": remote[tid],
            }, ensure_ascii=False) + "\n")
    print(f"  drive head: both versions of {len(report.conflicts)} conflict(s) "
          f"logged to {conflict_log}")


def adopt_drive_head(prior: dict[str, dict], *, edits_path: str | None = None,
                     force_push: bool = False, conflict_log: str | None = None,
                     secrets_dir: str = _SECRETS_DIR) -> dict[str, dict]:
    """Rebase this machine onto the Drive head before any audit or write.

    The categorized store has TWO legitimate writers — a scheduled server run and
    occasional desktop Claude audit/review runs — serialized through the Drive copy:
    every Drive-enabled run STARTS by adopting the remote head and ENDS by pushing the
    full local store. Adoption is ``persister.reconcile``'s preserved union:

    - remote-only rows are taken (the other machine audited them; also restores a
      lost/reset local store from Drive),
    - conflicts are resolved by ``_newer_audit_wins`` — the more recently audited
      side keeps its work, whichever machine it came from; BOTH versions are
      appended to ``conflict_log`` first, so no version is ever silently lost,
    - local-only rows are kept (work not yet pushed survives).

    The manual-edits intent log is adopted first (see ``_adopt_remote_edits``) —
    store and log must move together or replay reverts the other machine's
    corrections.

    A pull FAILURE still stops the run: pushing blind over an unreadable remote
    could clobber the other writer. ``--force-push`` skips adoption entirely — the
    human has declared the LOCAL store (and log) authoritative; it bypasses a pull
    failure the same way. External tamper of the Drive copy, which the old
    stop-on-divergence gate existed to catch, is now adopted rather than stopped:
    with a second writer, "remote is ahead" is routine, and tamper forensics moved
    to the append-only Drive revisions and the data repo's git history.
    """
    from persister import DrivePullError, DriveSync
    if force_push:
        print("  drive head: --force-push — skipping adoption; the LOCAL store is "
              "authoritative and the Drive copy will be overwritten (as a new "
              "revision; history survives).", file=sys.stderr)
        return prior
    try:
        remote = persister.load_jsonl_bytes(
            DriveSync(DRIVE_JSONL_NAME, folder_name=DRIVE_FOLDER,
                      secrets_dir=secrets_dir).pull())
    except DrivePullError as e:
        sys.exit(
            f"  drive head: STOP — could not read the Drive remote ({e}), so the "
            "other machine's work cannot be adopted and the end-of-run push might "
            "clobber it. Nothing was audited or written. Retry when Drive is "
            "reachable, run --no-drive to stay local, or --force-push to declare "
            "the local store authoritative."
        )
    if edits_path is not None:
        _adopt_remote_edits(edits_path, secrets_dir)
    if not remote:
        return prior
    # txn_owner is collector metadata; category_audited_at is the resolver's recency
    # signal — neither is audit content, so neither may READ as a conflict (rows equal
    # modulo these fields stay in_sync, local copy kept).
    report = persister.reconcile(
        prior, remote,
        metadata_fields=("txn_owner", "category_audited_at", "claude_audited_at"),
        conflict_resolver=_newer_audit_wins)
    if not report.conflicts and not report.remote_only:
        return prior        # in sync, or local-ahead — nothing to adopt
    _log_adopt_conflicts(conflict_log, report, prior, remote)
    kept_local = sum(1 for k in report.conflicts if report.merged[k] is prior[k])
    print(f"  drive head: adopting remote work — {len(report.remote_only)} "
          f"remote-only row(s) taken, {len(report.conflicts)} conflict(s) resolved "
          f"by newest audit stamp ({kept_local} kept local, "
          f"{len(report.conflicts) - kept_local} took remote), "
          f"{len(report.local_only)} local-only row(s) kept.")
    return report.merged


# ── Orchestration (I/O) ───────────────────────────────────────────────────────

def run(*, input_path: str, out_jsonl: str, out_csv: str, flags_csv: str, log_path: str,
        levels: set[str], memory_path: str | None, do_drive: bool,
        no_llm: bool, debug: bool, full: bool = False, force_push: bool = False,
        edits_path: str | None = None, defer_llm: bool = False) -> None:
    input_store = load_input(input_path)
    if not input_store:
        # Guard against mass deletion: an empty input (e.g. a failed upstream fetch) must
        # NOT be read as "every categorized row was removed". Leave the stores untouched.
        print(f"No transactions found in {input_path} — nothing to do "
              "(stores left intact; no pruning).")
        return
    print(f"Loaded {len(input_store)} transaction(s) from {input_path}")

    # Incremental delta: diff the input against the prior categorized store so we audit only
    # new/changed rows, carry the rest forward, and prune rows gone upstream.
    prior = persister.load_jsonl(out_jsonl) if os.path.isfile(out_jsonl) else {}

    # Before any work (the LLM stage is expensive) or any write: rebase onto the Drive
    # head so the other machine's audits/corrections are carried forward, not clobbered.
    if do_drive:
        prior = adopt_drive_head(prior, edits_path=edits_path, force_push=force_push,
                                 conflict_log=_conflict_log_path(out_jsonl))
    delta = classify(input_store, prior, full=full)
    print(f"  Delta vs prior store: {len(delta.new)} new, {len(delta.changed)} changed, "
          f"{len(delta.carryover)} unchanged (carried forward), "
          f"{len(delta.removed)} removed.")

    # Prune-legitimacy gate: in the durable input store the ONLY rows that ever
    # legitimately disappear are settled pendings — posted rows are never deleted
    # upstream (stale ones are only FLAGGED, see transactions/overfetch). A
    # "removed" POSTED row therefore means the input is stale or truncated (e.g.
    # categorize run against an old raw store), and pruning it would shrink the
    # shared store. Stop before any audit, write, or push.
    bad_prunes = [tid for tid in delta.removed if not prior[tid].get("pending")]
    if bad_prunes:
        sys.exit(
            f"  prune gate: STOP — the input is missing {len(bad_prunes)} POSTED "
            f"row(s) the store already has (e.g. {', '.join(sorted(bad_prunes)[:5])}). "
            "Posted rows never vanish upstream, so the input looks stale or "
            "truncated: run the fetch first (./run.py runs it for you) or check "
            f"{input_path}. Nothing was audited, written, or pushed. If the "
            "removal is genuinely intentional, delete the rows from the "
            "categorized store by hand, then re-run."
        )

    memory = MerchantMemory(memory_path) if memory_path else None
    llm = None if (no_llm or defer_llm) else CategoryLLM(debug=debug)

    # Manual edit intents (Stage 3): resolved up front so covered rows can skip the audit;
    # replayed over the FULL store after assembly. ``None`` edits_path disables the stage.
    manual_index = (ManualIndex(resolve_intents(load_intents(edits_path)))
                    if edits_path is not None else None)

    # Capture the source hash of each row to audit BEFORE transform mutates it (a correction
    # overwrites the category in place); stamp it back on afterwards for next run's diff.
    hashes = {tid: source_hash(rec) for tid, rec in delta.to_process.items()}

    # Count selection BEFORE transform: a corrected row's confidence becomes the CORRECTED
    # sentinel, which would otherwise drop it from a post-hoc count. Rows covered by a
    # manual intent are counted separately — they skip the audit (and its LLM cost).
    n_covered = (sum(1 for r in delta.to_process.values()
                     if manual_index.match(r) is not None)
                 if manual_index is not None else 0)
    n_selected = sum(1 for r in delta.to_process.values()
                     if should_process(r, levels)
                     and (manual_index is None or manual_index.match(r) is None))

    _, changes, flags = transform(delta.to_process, levels=levels, memory=memory, llm=llm,
                                  manual=manual_index)

    # Stamp each processed row's source hash — UNLESS the LLM stage was requested but
    # didn't actually run (Ollama down / crashed): stamping then would mark the rows as
    # fully audited and silently skip LLM review forever. The sentinel forces a re-audit
    # next run. An explicit --no-llm run stamps normally (rules-only was deliberate);
    # --llm-defer stamps the SAME sentinel on purpose — rules now, LLM on whichever
    # future run has Ollama (the scheduled-server / desktop-LLM split).
    llm_skipped = llm is not None and not llm.ran_ok
    llm_pending = llm_skipped or defer_llm
    if llm_skipped:
        print("  ⚠ LLM stage did not run — rows from this run will be re-audited "
              "(with the LLM) on the next run.")
    elif defer_llm:
        print("  LLM deferred (--llm-defer) — rows from this run stay pending and "
              "will be audited by the next LLM-enabled run.")
    # Audit-recency stamping: processed rows are rebuilt from RAW input, so the
    # stages above re-stamp them even when they reproduce last run's result (e.g.
    # a nightly --llm-defer re-running rules over an unchanged pending row). That
    # reprocessing is NOT new work: if the final content matches the prior store's
    # copy, inherit the prior stamp — otherwise a stale machine's no-op re-runs
    # would outrank the other machine's real work in the adopt-time resolver.
    for tid, rec in delta.to_process.items():
        rec[SOURCE_HASH_FIELD] = HASH_PENDING_LLM if llm_pending else hashes[tid]
        prev = prior.get(tid)
        if (prev is not None and prev.get("category_audited_at")
                and _audit_content_equal(rec, prev)):
            rec["category_audited_at"] = prev["category_audited_at"]
        elif not rec.get("category_audited_at"):
            stamp_audited_at(rec)       # new/changed row the stages didn't stamp

    if memory is not None:
        memory.save()

    # Reassemble the full store: carried-over audited rows + freshly audited rows. Rows that
    # vanished upstream are in neither, so they're dropped; dedupe_supersede then drops any
    # pending row a posted row superseded within this same store.
    store = persister.dedupe_supersede({**delta.carryover, **delta.to_process})

    # Stage 3 — replay the manual edit intents over the FULL store (highest authority;
    # idempotent), so human edicts survive --full re-audits and upstream row changes.
    manual_summary = (apply_manual_edits(store, manual_index)
                      if manual_index is not None else None)
    n_auto = len(changes)
    if manual_summary is not None:
        changes = changes + manual_summary["applied"]

    persister.save_jsonl(out_jsonl, store)
    persister.derive_csv(store, out_csv, COLUMNS, row_fn=row_fn)
    write_category_log(log_path, changes)
    n_flagged = write_flags_file(flags_csv, store)

    print(f"  Audited {n_selected} row(s); auto-applied {n_auto}; "
          f"flagged {len(flags)} new this run.")
    if manual_summary is not None and (len(manual_index) or manual_summary["reverted"]):
        ms = manual_summary
        print(f"  Manual edits: {len(manual_index)} intent(s) → applied "
              f"{len(ms['applied'])}, already satisfied {ms['already']}, reverted "
              f"{len(ms['reverted'])} (revoked); skipped the audit on {n_covered} "
              f"covered row(s).")
        if ms["orphans"]:
            print(f"  Manual edits: {len(ms['orphans'])} transaction-scope intent(s) "
                  f"point at rows no longer in the store (inert): "
                  f"{', '.join(ms['orphans'][:5])}")
    print(f"  Wrote {out_jsonl} ({len(store)} rows)")
    print(f"  Wrote {out_csv}")
    print(f"  Wrote {flags_csv} ({n_flagged} row(s) pending review)")
    if changes:
        print(f"  Logged {len(changes)} change(s) → {log_path}")
    if delta.removed:
        where = "local + Drive stores" if do_drive else "local store"
        print(f"  Pruned {len(delta.removed)} row(s) gone upstream "
              f"(hard-deleted or settled pending) from the {where}.")
    if n_flagged:
        print(f"  {n_flagged} row(s) pending review — see {flags_csv}, or run --review.")

    if do_drive:
        _drive_push_outputs(out_jsonl, out_csv, flags_csv, edits_path)


def _drive_push_outputs(out_jsonl: str, out_csv: str, flags_csv: str,
                        edits_path: str | None = None) -> None:
    """Push the output files to Drive as new revisions (shared by run/review).

    The manual-edits intent log rides along when present: it is SOURCE data the store is
    rebuilt from, so it gets the same off-machine durability as the store itself. It is
    locally authored and append-only, so it needs no divergence gate — a plain push.
    """
    print("  ⚠ Drive sync ON — the categorized store will leave this machine "
          "(Google Drive). Use --no-drive to keep it local.")
    from persister import DriveSync
    link = DriveSync(DRIVE_JSONL_NAME, folder_name=DRIVE_FOLDER,
                     secrets_dir=_SECRETS_DIR).push(out_jsonl, mime="application/x-ndjson")
    if link:
        print(f"  Drive: pushed JSONL → {link}")
    DriveSync("transactions_categorized.csv", folder_name=DRIVE_FOLDER,
              secrets_dir=_SECRETS_DIR).push(out_csv, mime="text/csv")
    DriveSync("flagged_for_review.csv", folder_name=DRIVE_FOLDER,
              secrets_dir=_SECRETS_DIR).push(flags_csv, mime="text/csv")
    if edits_path and os.path.isfile(edits_path):
        DriveSync("manual_edits.jsonl", folder_name=DRIVE_FOLDER,
                  secrets_dir=_SECRETS_DIR).push(edits_path, mime="application/x-ndjson")


def review_run(*, out_jsonl: str, out_csv: str, flags_csv: str,
               memory_path: str | None, do_drive: bool = True,
               force_push: bool = False, edits_path: str | None = None) -> None:
    """Adjudicate the flags in an already-categorized store, then re-persist it.

    Re-persists locally AND (unless ``--no-drive``) pushes new Drive revisions, so a
    review session keeps local and remote in lock-step. The Drive head is adopted
    first (before any human effort is spent), same as ``run``: the flags being
    adjudicated should be the freshest copy, and the push at the end must not
    clobber the other machine's work.
    """
    from .review import DEFAULT_REVIEW_LOG, run_review, write_review_log

    if not os.path.isfile(out_jsonl):
        sys.exit(f"ERROR: no categorized store to review at {out_jsonl}\n"
                 "Run the audit first (categorize.py) to produce flagged rows.")
    store = persister.load_jsonl(out_jsonl)
    if do_drive:
        store = adopt_drive_head(store, edits_path=edits_path, force_push=force_push,
                                 conflict_log=_conflict_log_path(out_jsonl))
    memory = MerchantMemory(memory_path) if memory_path else None

    summary = run_review(store, memory)
    if summary["accepted"] or summary["repicked"] or summary["rejected"]:
        if memory is not None:
            memory.save()
        persister.save_jsonl(out_jsonl, store)
        persister.derive_csv(store, out_csv, COLUMNS, row_fn=row_fn)
        n_flagged = write_flags_file(flags_csv, store)  # shrink the worklist
        write_review_log(DEFAULT_REVIEW_LOG, summary["log"])
        print(f"  Updated {out_jsonl}, {out_csv}, and {flags_csv} "
              f"({n_flagged} row(s) still pending).")
        if do_drive:
            _drive_push_outputs(out_jsonl, out_csv, flags_csv, edits_path)


def edit_run(*, out_jsonl: str, out_csv: str, flags_csv: str, log_path: str,
             edits_path: str, do_drive: bool = True, force_push: bool = False) -> None:
    """Capture manual edit intents interactively, then replay them into the store.

    The session only APPENDS to the intent log; afterwards the full log is replayed
    over the store (same stage as a normal run), so the new edits land immediately —
    and, being intents, they re-apply on every future run too. Re-persists and pushes
    Drive revisions exactly like a review session, behind the same head adoption.
    """
    from .manual import run_edit_session

    if not os.path.isfile(out_jsonl):
        sys.exit(f"ERROR: no categorized store to edit at {out_jsonl}\n"
                 "Run the audit first (categorize.py).")
    store = persister.load_jsonl(out_jsonl)
    if do_drive:
        store = adopt_drive_head(store, edits_path=edits_path, force_push=force_push,
                                 conflict_log=_conflict_log_path(out_jsonl))

    n_new = run_edit_session(store, edits_path)
    index = ManualIndex(resolve_intents(load_intents(edits_path)))
    summary = apply_manual_edits(store, index)

    if not (n_new or summary["applied"] or summary["reverted"]):
        print("No changes — store left untouched.")
        return
    persister.save_jsonl(out_jsonl, store)
    persister.derive_csv(store, out_csv, COLUMNS, row_fn=row_fn)
    n_flagged = write_flags_file(flags_csv, store)
    write_category_log(log_path, summary["applied"])
    print(f"  Replayed {len(index)} intent(s): applied {len(summary['applied'])}, "
          f"already satisfied {summary['already']}, reverted {len(summary['reverted'])}.")
    print(f"  Updated {out_jsonl}, {out_csv}, and {flags_csv} "
          f"({n_flagged} row(s) pending review).")
    if do_drive:
        _drive_push_outputs(out_jsonl, out_csv, flags_csv, edits_path)


def claude_export_run(*, out_jsonl: str, queue_path: str, scan_path: str,
                      memory_path: str | None = None, full: bool = False) -> None:
    """Export one ritual's inputs locally (no Drive, no network): the review queue (rows
    Claude hasn't seen) AND the deterministic store-wide scan, read together in one pass.

    Read-only over the categorized store + merchant memory; writes only the 0600 scratch
    files in .secrets. The matching ``--claude-apply`` is where verdicts land.
    """
    if not os.path.isfile(out_jsonl):
        sys.exit(f"ERROR: no categorized store to export at {out_jsonl}\n"
                 "Run the audit first (categorize.py).")
    store = persister.load_jsonl(out_jsonl)
    memory = (MerchantMemory(memory_path, read_only=True)
              if memory_path and os.path.isfile(memory_path) else None)
    for p in (queue_path, scan_path):
        os.makedirs(os.path.dirname(os.path.abspath(p)), mode=0o700, exist_ok=True)
    n, scan = export_bundle(store, queue_path, scan_path, memory=memory, full=full)
    for p in (queue_path, scan_path):
        try:
            os.chmod(p, 0o600)  # financial data — owner-only
        except OSError:
            pass
    sweep_hits = {k: len(v) for k, v in scan.items() if v}
    print(f"  Claude audit: {n} row(s) to review → {queue_path}")
    print(f"  Claude audit: store-wide scan → {scan_path}"
          + (f"  (flagged: {sweep_hits})" if sweep_hits else "  (no sweep findings)"))
    if n == 0 and not sweep_hits:
        print(f"  Nothing to review and a clean scan — every posted row already carries a "
              f"claude_audited_at stamp. Use --full to re-export all {len(store)} row(s).")
        return
    print('  Next: have Claude read BOTH files in one pass and write verdicts (one JSON per '
          'line: {"transaction_id":…, "verdict":"flag"|"ok", "primary":…, "detailed":…, '
          '"reason":…}), then run --claude-apply.')


def claude_apply_run(*, out_jsonl: str, out_csv: str, flags_csv: str, verdicts_path: str,
                     edits_path: str | None = None, do_drive: bool = True,
                     force_push: bool = False) -> None:
    """Apply Claude's verdicts as review flags, then re-persist (and push Drive).

    Claude is a REVIEWER: each verdict either raises an ordinary ``category_review_*`` flag
    (``source="claude"``) for the human to adjudicate via ``--review``, or just records that
    Claude looked (``claude_audited_at``). Mirrors ``review_run``: the Drive head is adopted
    first (two-writer safety), and the push at the end keeps local and remote in lock-step.
    """
    if not os.path.isfile(out_jsonl):
        sys.exit(f"ERROR: no categorized store to audit at {out_jsonl}\n"
                 "Run the audit first (categorize.py).")
    if not os.path.isfile(verdicts_path):
        sys.exit(f"ERROR: no Claude verdicts at {verdicts_path}\n"
                 "Produce them from --claude-export output first.")
    store = persister.load_jsonl(out_jsonl)
    if do_drive:
        store = adopt_drive_head(store, edits_path=edits_path, force_push=force_push,
                                 conflict_log=_conflict_log_path(out_jsonl))

    summary = apply_verdicts(store, load_verdicts(verdicts_path))
    n_reviewed = summary["flagged"] + summary["ok"]
    if n_reviewed:
        persister.save_jsonl(out_jsonl, store)
        persister.derive_csv(store, out_csv, COLUMNS, row_fn=row_fn)
        n_flagged = write_flags_file(flags_csv, store)
        print(f"  Claude audit: reviewed {n_reviewed} row(s) — flagged {summary['flagged']} "
              f"new, cleared {summary['ok']} as ok. {n_flagged} row(s) now pending review "
              f"(adjudicate with --review).")
        if do_drive:
            _drive_push_outputs(out_jsonl, out_csv, flags_csv, edits_path)
    else:
        print("  Claude audit: no recognised verdicts — store left untouched.")
    if summary["invalid"]:
        print(f"  ⚠ {len(summary['invalid'])} verdict(s) had an unknown primary and were "
              f"skipped: {', '.join(str(t) for t in summary['invalid'][:5])}")
    if summary["unknown"]:
        print(f"  ⚠ {len(summary['unknown'])} verdict(s) referenced rows not in the store: "
              f"{', '.join(str(t) for t in summary['unknown'][:5])}")


def _resolve_llm_mode(*, llm: bool, no_llm: bool, llm_defer: bool,
                      default_on: bool = LLM_ENABLED_BY_DEFAULT) -> tuple[bool, bool]:
    """Map the (mutually-exclusive) CLI flags + config default to ``(no_llm, defer_llm)``.

    The local LLM reviewer is OFF by default (``config.LLM_ENABLED_BY_DEFAULT``) — the 7B
    was too noisy (LLM_ASSESSMENT.md). Deterministic rules + the sign guard run regardless;
    the strong review is the out-of-band Claude ritual. ``--llm`` turns it on, ``--llm-defer``
    runs rules-only now but keeps rows pending for a later ``--llm`` run, ``--no-llm`` is the
    explicit off (== default). No flag → the config default.
    """
    if llm_defer:
        return False, True
    if llm:
        return False, False
    if no_llm:
        return True, False
    return (not default_on), False


def main():
    ap = argparse.ArgumentParser(
        description="Audit & re-categorize Plaid transactions (all confidence levels by "
                    "default) within Plaid's PFC taxonomy (mechanical rules + local LLM), "
                    "preserving originals and persisting via persister.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--input", default=DEFAULT_INPUT, metavar="PATH",
                    help="input store: JSONL (persister) or .xz raw store "
                         "(default: <data root>/transactions/data/transactions.jsonl)")
    ap.add_argument("--out-jsonl", default=DEFAULT_OUT_JSONL, metavar="PATH",
                    help="output categorized JSONL (default: under the data root)")
    ap.add_argument("--out-csv", default=DEFAULT_OUT_CSV, metavar="PATH",
                    help="output categorized CSV (default: under the data root)")
    ap.add_argument("--flags-csv", default=DEFAULT_FLAGS_CSV, metavar="PATH",
                    help="dedicated worklist of rows pending review "
                         "(default: under the data root)")
    ap.add_argument("--full", action="store_true",
                    help="re-audit EVERY input row, ignoring the incremental delta "
                         "(use after changing rules/config); still prunes removed rows")
    ap.add_argument("--confidence", default=",".join(sorted(PROCESS_CONFIDENCE)),
                    metavar="LEVELS",
                    help="comma-separated confidence levels to audit "
                         "(default: all — LOW,MEDIUM,HIGH,VERY_HIGH,UNKNOWN)")
    ap.add_argument("--memory", default=DEFAULT_MEMORY, metavar="PATH",
                    help="merchant-memory JSON path (default: .secrets/merchant_memory.json)")
    ap.add_argument("--no-memory", action="store_true",
                    help="disable merchant memory entirely (no reads or writes)")
    llm_group = ap.add_mutually_exclusive_group()
    llm_group.add_argument("--llm", action="store_true",
                           help="enable the local-LLM review stage (OFF by default since the "
                                "7B was too noisy — see LLM_ASSESSMENT.md; the Claude ritual "
                                "is the default strong reviewer)")
    llm_group.add_argument("--no-llm", action="store_true",
                           help="explicitly skip the LLM stage (this is the default now; "
                                "mechanical rules only, rows stamp as fully audited)")
    llm_group.add_argument("--llm-defer", action="store_true",
                           help="rules now, LLM later: skip the LLM stage but stamp rows "
                                "as still pending, so the next --llm run audits them")
    ap.add_argument("--no-drive", action="store_true",
                    help="do not push results to Google Drive (stay fully offline)")
    ap.add_argument("--force-push", action="store_true",
                    help="skip the Drive head adoption: treat the LOCAL categorized "
                         "store (and intent log) as authoritative and overwrite the "
                         "Drive copy (pushed as a new revision; old revisions survive)")
    ap.add_argument("--review", action="store_true",
                    help="interactively adjudicate flagged rows in --out-jsonl "
                         "(accept/reject/re-pick); does not re-run the audit")
    ap.add_argument("--edit", action="store_true",
                    help="interactively capture manual category edits (search a row, "
                         "pick a category, transaction- or merchant-scope) as intents "
                         "in --edits, applied immediately and replayed every run")
    ap.add_argument("--edits", default=DEFAULT_EDITS, metavar="PATH",
                    help="manual-edit intents JSONL, replayed as the final stage every "
                         "run (default: <data root>/…/manual_edits.jsonl)")
    ap.add_argument("--claude-export", action="store_true",
                    help="Claude audit ritual: export rows Claude hasn't reviewed to a "
                         "local queue file (no Drive, no network) and exit (use --full to "
                         "re-export every posted row)")
    ap.add_argument("--claude-apply", action="store_true",
                    help="Claude audit ritual: apply a verdicts file as review flags "
                         "(source=claude), then re-persist (and push Drive unless --no-drive)")
    ap.add_argument("--claude-queue", default=DEFAULT_CLAUDE_QUEUE, metavar="PATH",
                    help="review-queue file written by --claude-export (default: .secrets/…)")
    ap.add_argument("--claude-scan", default=DEFAULT_CLAUDE_SCAN, metavar="PATH",
                    help="store-wide scan file written by --claude-export (default: .secrets/…)")
    ap.add_argument("--claude-verdicts", default=DEFAULT_CLAUDE_VERDICTS, metavar="PATH",
                    help="verdicts JSONL read by --claude-apply (default: .secrets/…)")
    ap.add_argument("--log", default=DEFAULT_LOG, metavar="PATH",
                    help="JSONL change log (default: .secrets/category_log.jsonl)")
    ap.add_argument("--debug", action="store_true",
                    help="verbose LLM-stage debug output")
    args = ap.parse_args()

    # Validate --confidence up front (before any subcommand dispatch or filesystem
    # check) so a typo'd level fails loudly instead of silently selecting no rows.
    levels = {lv.strip().upper() for lv in args.confidence.split(",") if lv.strip()}
    unknown = levels - VALID_CONFIDENCE_LEVELS
    if not levels or unknown:
        ap.error("--confidence: "
                 + (f"unknown level(s): {','.join(sorted(unknown))}" if unknown
                    else "no levels given")
                 + f" (valid: {','.join(sorted(VALID_CONFIDENCE_LEVELS))})")

    if args.edit:
        edit_run(
            out_jsonl=args.out_jsonl,
            out_csv=args.out_csv,
            flags_csv=args.flags_csv,
            log_path=args.log,
            edits_path=args.edits,
            do_drive=not args.no_drive,
            force_push=args.force_push,
        )
        return

    if args.review:
        review_run(
            out_jsonl=args.out_jsonl,
            out_csv=args.out_csv,
            flags_csv=args.flags_csv,
            memory_path=None if args.no_memory else args.memory,
            do_drive=not args.no_drive,
            force_push=args.force_push,
            edits_path=args.edits,
        )
        return

    if args.claude_export:
        claude_export_run(out_jsonl=args.out_jsonl, queue_path=args.claude_queue,
                          scan_path=args.claude_scan,
                          memory_path=None if args.no_memory else args.memory,
                          full=args.full)
        return

    if args.claude_apply:
        claude_apply_run(
            out_jsonl=args.out_jsonl,
            out_csv=args.out_csv,
            flags_csv=args.flags_csv,
            verdicts_path=args.claude_verdicts,
            edits_path=args.edits,
            do_drive=not args.no_drive,
            force_push=args.force_push,
        )
        return

    if not os.path.isfile(args.input):
        sys.exit(f"ERROR: input not found: {args.input}\n"
                 "Run the transactions fetcher / persister first, or pass --input.")

    no_llm, defer_llm = _resolve_llm_mode(llm=args.llm, no_llm=args.no_llm,
                                          llm_defer=args.llm_defer)
    run(
        input_path=args.input,
        out_jsonl=args.out_jsonl,
        out_csv=args.out_csv,
        flags_csv=args.flags_csv,
        log_path=args.log,
        levels=levels,
        memory_path=None if args.no_memory else args.memory,
        do_drive=not args.no_drive,
        no_llm=no_llm,
        debug=args.debug,
        full=args.full,
        force_push=args.force_push,
        edits_path=args.edits,
        defer_llm=defer_llm,
    )


if __name__ == "__main__":
    main()
