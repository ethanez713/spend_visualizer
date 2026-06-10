"""Engine + CLI: audit & re-categorize Plaid rows (ALL confidence levels), then persist.

Pipeline (mirrors ``converter``'s architecture):
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

from .config import LLM_AUTHORITY, TRUSTED_CONFIDENCE_LEVELS
from .incremental import HASH_PENDING_LLM, SOURCE_HASH_FIELD, classify, source_hash
from .llm import CategoryLLM
from .manual import DEFAULT_EDITS, ManualIndex, apply_manual_edits, load_intents, \
    resolve_intents
from .rules import DEFAULT_MEMORY, MerchantMemory, RuleHit, apply_rules
from .schema import (
    COLUMNS,
    FLAG_COLUMNS,
    PROCESS_CONFIDENCE,
    confidence_of,
    ensure_new_columns,
    flag_row_fn,
    row_fn,
    set_provenance,
    set_review_flag,
    should_process,
)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SECRETS_DIR = os.path.join(_PROJECT_ROOT, ".secrets")

DEFAULT_INPUT = os.path.join(_PROJECT_ROOT, "..", "transactions", "data", "transactions.jsonl")
DEFAULT_OUT_JSONL = os.path.join(_PROJECT_ROOT, "data", "transactions_categorized.jsonl")
DEFAULT_OUT_CSV = os.path.join(_PROJECT_ROOT, "data", "transactions_categorized.csv")
DEFAULT_FLAGS_CSV = os.path.join(_PROJECT_ROOT, "data", "flagged_for_review.csv")
DEFAULT_LOG = os.path.join(_SECRETS_DIR, "category_log.jsonl")

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


def _decide(record: dict, mech: RuleHit | None, llm_decision,
            *, authority: str = LLM_AUTHORITY) -> Decision:
    """Apply the tiered authority model (see ``config``) to one row.

    Order of authority:
      1. A mechanical ``trust="auto"`` rule (entity-id memory, specific COT* prefixes) that
         differs from the current category → APPLY in place.
      2. Otherwise, take the best DISAGREEING suggestion — the LLM's first (it saw the most
         context), else a loose mechanical ``trust="flag"`` rule.
      3. The LLM may AUTO-APPLY its suggestion only on an UNTRUSTED row (Plaid confidence not
         HIGH/VERY_HIGH) and only as far as ``LLM_AUTHORITY`` allows
         (``"final"`` always; ``"apply_when_high"`` only when the LLM is HIGH-confidence).
         Mechanical ``flag`` rules and trusted rows never auto-apply.
      4. Anything else with a disagreeing suggestion → FLAG for review.
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
    sugg: Decision | None = None
    if llm_decision is not None:
        if (llm_decision.primary, llm_decision.detailed) != cur:
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

    # 4. Otherwise flag for human review.
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


# ── Drive divergence gate ─────────────────────────────────────────────────────

def check_drive_divergence(prior: dict[str, dict], *, force_push: bool = False,
                           secrets_dir: str = _SECRETS_DIR) -> None:
    """Stop before any write if the Drive copy of the categorized store has diverged.

    Invariant: every Drive-enabled run ends by pushing the full local store, so at the
    START of a run the remote must hold nothing the local prior store doesn't already
    have (equal, or behind after ``--no-drive`` runs). Divergence — content conflicts,
    or remote-only ids — means the Drive copy was edited externally or the local store
    was lost/reset; pushing now would clobber the remote audit history (including human
    review decisions). Unlike the raw store there is no golden source to repair from
    (the corrections ARE the value), so a human must arbitrate: restore the local store,
    or re-run with ``--force-push`` to declare the local store authoritative.

    A missing remote (nothing pushed yet, or Drive unreachable) passes the gate — the
    later push degrades the same soft way.
    """
    from persister import DriveSync
    remote = persister.load_jsonl_bytes(
        DriveSync(DRIVE_JSONL_NAME, folder_name=DRIVE_FOLDER,
                  secrets_dir=secrets_dir).pull())
    if not remote:
        return
    report = persister.reconcile(prior, remote)
    if not report.conflicts and not report.remote_only:
        return
    sample = ", ".join((report.conflicts + report.remote_only)[:10])
    print(f"  drive gate: categorized store diverged from the Drive copy — "
          f"{len(report.conflicts)} conflict(s), {len(report.remote_only)} remote-only "
          f"row(s) (e.g. {sample}).", file=sys.stderr)
    if force_push:
        print("  drive gate: --force-push — treating the LOCAL store as authoritative; "
              "the Drive copy will be overwritten (as a new revision; history survives).",
              file=sys.stderr)
        return
    sys.exit(
        "  drive gate: STOP — refusing to overwrite a diverged Drive copy. Nothing was "
        "audited or written. If the local store is the correct one (e.g. after --no-drive "
        "runs), re-run with --force-push; to inspect the remote, use persister's "
        "DriveSync.pull()/list_revisions(), or run with --no-drive to stay local."
    )


# ── Orchestration (I/O) ───────────────────────────────────────────────────────

def run(*, input_path: str, out_jsonl: str, out_csv: str, flags_csv: str, log_path: str,
        levels: set[str], memory_path: str | None, do_drive: bool,
        no_llm: bool, debug: bool, full: bool = False, force_push: bool = False,
        edits_path: str | None = None) -> None:
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

    # Before any work (the LLM stage is expensive) or any write: make sure pushing at the
    # end would not clobber a Drive copy that has drifted from what this store last knew.
    if do_drive:
        check_drive_divergence(prior, force_push=force_push)
    delta = classify(input_store, prior, full=full)
    print(f"  Delta vs prior store: {len(delta.new)} new, {len(delta.changed)} changed, "
          f"{len(delta.carryover)} unchanged (carried forward), "
          f"{len(delta.removed)} removed.")

    memory = MerchantMemory(memory_path) if memory_path else None
    llm = None if no_llm else CategoryLLM(debug=debug)

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
    # next run. An explicit --no-llm run stamps normally (rules-only was deliberate).
    llm_skipped = llm is not None and not llm.ran_ok
    if llm_skipped:
        print("  ⚠ LLM stage did not run — rows from this run will be re-audited "
              "(with the LLM) on the next run.")
    for tid, rec in delta.to_process.items():
        rec[SOURCE_HASH_FIELD] = HASH_PENDING_LLM if llm_skipped else hashes[tid]

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
    review session keeps local and remote in lock-step — otherwise the next
    Drive-enabled run would trip the divergence gate on the locally-applied decisions.
    The same gate runs first (before any human effort is spent) for the same reason
    as ``run``: never clobber a remote copy that drifted.
    """
    from .review import DEFAULT_REVIEW_LOG, run_review, write_review_log

    if not os.path.isfile(out_jsonl):
        sys.exit(f"ERROR: no categorized store to review at {out_jsonl}\n"
                 "Run the audit first (categorize.py) to produce flagged rows.")
    store = persister.load_jsonl(out_jsonl)
    if do_drive:
        check_drive_divergence(store, force_push=force_push)
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
    Drive revisions exactly like a review session, behind the same divergence gate.
    """
    from .manual import run_edit_session

    if not os.path.isfile(out_jsonl):
        sys.exit(f"ERROR: no categorized store to edit at {out_jsonl}\n"
                 "Run the audit first (categorize.py).")
    store = persister.load_jsonl(out_jsonl)
    if do_drive:
        check_drive_divergence(store, force_push=force_push)

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


def main():
    ap = argparse.ArgumentParser(
        description="Audit & re-categorize Plaid transactions (all confidence levels by "
                    "default) within Plaid's PFC taxonomy (mechanical rules + local LLM), "
                    "preserving originals and persisting via persister.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--input", default=DEFAULT_INPUT, metavar="PATH",
                    help="input store: JSONL (persister) or .xz raw store "
                         "(default: ../transactions/data/transactions.jsonl)")
    ap.add_argument("--out-jsonl", default=DEFAULT_OUT_JSONL, metavar="PATH",
                    help="output categorized JSONL (default: data/transactions_categorized.jsonl)")
    ap.add_argument("--out-csv", default=DEFAULT_OUT_CSV, metavar="PATH",
                    help="output categorized CSV (default: data/transactions_categorized.csv)")
    ap.add_argument("--flags-csv", default=DEFAULT_FLAGS_CSV, metavar="PATH",
                    help="dedicated worklist of rows pending review "
                         "(default: data/flagged_for_review.csv)")
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
    ap.add_argument("--no-llm", action="store_true",
                    help="skip the LLM stage (mechanical rules only)")
    ap.add_argument("--no-drive", action="store_true",
                    help="do not push results to Google Drive (stay fully offline)")
    ap.add_argument("--force-push", action="store_true",
                    help="override the Drive divergence gate: treat the LOCAL categorized "
                         "store as authoritative and overwrite a diverged Drive copy "
                         "(pushed as a new revision; old revisions survive)")
    ap.add_argument("--review", action="store_true",
                    help="interactively adjudicate flagged rows in --out-jsonl "
                         "(accept/reject/re-pick); does not re-run the audit")
    ap.add_argument("--edit", action="store_true",
                    help="interactively capture manual category edits (search a row, "
                         "pick a category, transaction- or merchant-scope) as intents "
                         "in --edits, applied immediately and replayed every run")
    ap.add_argument("--edits", default=DEFAULT_EDITS, metavar="PATH",
                    help="manual-edit intents JSONL, replayed as the final stage every "
                         "run (default: data/manual_edits.jsonl)")
    ap.add_argument("--log", default=DEFAULT_LOG, metavar="PATH",
                    help="JSONL change log (default: .secrets/category_log.jsonl)")
    ap.add_argument("--debug", action="store_true",
                    help="verbose LLM-stage debug output")
    args = ap.parse_args()

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

    if not os.path.isfile(args.input):
        sys.exit(f"ERROR: input not found: {args.input}\n"
                 "Run the transactions fetcher / persister first, or pass --input.")

    levels = {lv.strip().upper() for lv in args.confidence.split(",") if lv.strip()}
    run(
        input_path=args.input,
        out_jsonl=args.out_jsonl,
        out_csv=args.out_csv,
        flags_csv=args.flags_csv,
        log_path=args.log,
        levels=levels,
        memory_path=None if args.no_memory else args.memory,
        do_drive=not args.no_drive,
        no_llm=args.no_llm,
        debug=args.debug,
        full=args.full,
        force_push=args.force_push,
        edits_path=args.edits,
    )


if __name__ == "__main__":
    main()
