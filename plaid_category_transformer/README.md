# plaid_category_transformer

Audits **every** Plaid transaction **within Plaid's own Personal Finance Category (PFC)
taxonomy**, preserving the originals, recording which stage changed each row (or flagging
it for human review), and persisting the result via [`persister`](../persister).

Plaid tags every transaction with a PFC primary/detailed and a `confidence_level`, and
even **HIGH/VERY_HIGH** rows are sometimes wrong (e.g. Capital One Travel flights, `COT*FLT`,
landing in *postage & shipping*). So this tool audits **all** rows, but never lets the noisy
local model silently clobber a good category: **high-precision deterministic rules apply
in place; the LLM is a reviewer that flags disagreements for you to adjudicate.**

**All categorization policy lives in [`src/config.py`](src/config.py)** — selection, the
hardcoded rule tables (with a per-rule `auto`/`flag` trust level), and the LLM knobs — so
what is auto-applied vs. flagged is readable at a glance.

> All data (input store, categorized outputs, the manual-edits intent log) lives
> OUTSIDE this repo under the shared **data root** (`<monorepo>/data_root`, default
> `~/finance_data` — ideally its own private git repo, giving the same git + Drive
> dual audit history). Secrets stay in `.secrets/` (gitignored). The core path is
> **offline**; Drive sync is local/opt-in.

> **The local LLM reviewer is OFF by default** (`config.LLM_ENABLED_BY_DEFAULT = False`).
> The 2026-06 audit found `qwen2.5:7b` too noisy to be worth the review time (23% flag
> precision; it couldn't reliably read the amount-sign convention — see
> [`../LLM_ASSESSMENT.md`](../LLM_ASSESSMENT.md)). A bare run is **deterministic rules +
> the sign guard** only. The strong periodic review is now the **[Claude audit
> ritual](#claude-audit-ritual)** (a `--claude-export` / `--claude-apply` loop). Re-enable
> the local model with `--llm` (or flip the config flag) once a bigger/better one is
> available — the sign guard is model-agnostic and will protect it too.

## Pipeline

```
load ─▶ select (ALL rows by default) ─▶ Stage 1 mechanical rules ─▶ Stage 2 local LLM (reviewer) ─▶ apply / flag ─▶ Stage 3 manual overrides ─▶ persist
                                          auto: overwrite                disagreement: flag           (replayed human edit intents;
                                          flag: suggest                  concurrence: leave            highest authority, sticky)
```

1. **Select** (`schema.py`, `config.AUDIT_CONFIDENCE_LEVELS`) — by default **every** row,
   regardless of confidence (narrow via `--confidence` to run faster).
2. **Stage 1 — mechanical rules** (`rules.py`, tables in `config.py`) — deterministic, uses
   *all* signals (entity-id memory → name memory → POS/booking prefix → website → keyword).
   Each rule has a **trust**: `auto` rules (entity-id memory, the specific `COT*FLT`/`COT*HTL`
   prefixes) **overwrite in place**; `flag` rules (loose keyword/website/`TST*`, name-memory)
   are only **suggestions**.
3. **Stage 2 — local LLM** (`llm.py`, **OFF by default** — opt in with `--llm`) — Ollama
   `qwen2.5:7b` via `instructor` (JSON mode), `temperature=0, seed=0`, one row per call. It
   sees the schema, the full vendored taxonomy, every signal, and the mechanical suggestion —
   but it is a **reviewer, not an author**: by default it never overwrites, it only **flags**
   rows where it disagrees. Skips gracefully if Ollama is down. A deterministic **sign guard**
   (`config.INFLOW_PRIMARIES`) drops any suggestion that puts an income/inbound-transfer
   category on a positive (outgoing) amount, and **intra-tier-1 laterals** (same primary,
   different detailed) aren't flagged unless `config.FLAG_INTRA_PRIMARY_LATERALS=True`. Tune
   with `config.LLM_AUTHORITY` (`"flag"` | `"apply_when_high"` (discouraged — confidence is
   anti-calibrated) | `"final"`). Trusted Plaid labels (HIGH/VERY_HIGH) are **never**
   auto-changed by the LLM.
4. **Apply or flag** (`schema.py`) — an *applied* change saves originals → `original_*`,
   overwrites in place, sets the `CORRECTED` sentinel, and records `category_update_*`. A
   *flag* writes `category_review_*` and leaves the category untouched.
5. **Review** (`review.py`, `--review`) — walk the flagged rows interactively: **accept**
   (apply the suggestion + teach merchant memory so it's an `auto` hit next run), **reject**,
   or **re-pick**. Keeps a human in the loop exactly where the model is unsure. A review
   session re-persists locally **and pushes new Drive revisions** (unless `--no-drive`),
   so local and remote stay in lock-step.
6. **Stage 3 — manual overrides** (`manual.py`) — replays the human edit intents from
   **`data/manual_edits.jsonl`** over the full store, every run (see below).
7. **Persist** (`persister`) — under the data root:
   `transactions_categorized.{jsonl,csv}`, `flagged_for_review.csv` (the review
   worklist), `manual_edits.jsonl` (the intent log); optional Drive push of all four
   (default ON, `--no-drive`); `.secrets/{category_log,review_log}.jsonl` stay local.

## Manual edit intents (sticky human edits)

A manual category edit is never written into the store directly — it is appended to
**`data/manual_edits.jsonl`** as an **intent**, and the final pipeline stage replays every
intent on every run. That makes edits sticky by construction: they survive `--full`
re-audits, upstream row changes, even a rebuilt store. Capture surfaces:

- **Spend Analyzer UI** (`../spend_analyzer`) — 🚩 a transaction → *Recategorize (PFC)*
  (it appends to this repo's intent log directly; nothing applies until the next run);
- **`--edit`** — interactive CLI: search a row, pick a category, pick scope, note. Applies
  immediately (and re-persists + pushes Drive, behind the same head adoption).

Semantics:

- two scopes: **transaction** (one row) and **merchant** (every row of that merchant —
  entity-id match, normalized-name fallback, conflicting entity ids veto a name match;
  covers the merchant's *future* transactions too);
- precedence is **specificity first** (a transaction intent beats a merchant intent on its
  row), then **recency** (latest appended wins within a scope);
- a covered row **skips Stages 1–2** (no wasted LLM call) and any pending review flag on
  it is cleared — the human outranks the reviewer;
- **revoking** an intent (UI, or `revoke <id>` inside `--edit`) restores the row's prior
  category and stamps it for a full re-audit next run — the pipeline decides again;
- applied edits get `category_update_step = "manual"` with the intent id in the reason;
- the manual stage **never** touches merchant memory or the `config.py` rules. Instead,
  run **`analyze_edits.py`** occasionally: it mines the accumulated log (each intent
  snapshots the row's signals and what the machines believed at edit time) for
  rule-promotion candidates (paste-ready `config.py` snippets), rule-demotion candidates
  (rules humans keep overriding), an LLM scorecard, and Plaid-confidence stats — so rule
  changes stay periodic, targeted, and human-made.

When Drive sync is on, the run starts by **adopting the Drive head**: the remote
categorized store is pulled and reconciled against the local prior — remote-only rows are
taken, local-only rows are kept, and conflicting rows are resolved by **audit recency**:
every audit-content change stamps `category_audited_at`, and the newer side wins. A local
store that is *ahead* of the Drive head (an offline session, a crash between save and
push, a lost race) therefore never loses its work; ties and unstamped legacy rows fall
back to the remote value. Both versions of every conflict are first appended to
**`data/adopt_conflicts.jsonl`** (which rides the data repo's daily git push), so no
version is ever silently discarded. The manual-edits intent log is union-merged in the
same step (remote entries first, local-only entries re-appended). This is what makes
**two writers** safe — a scheduled server run and occasional desktop Claude audit/review
runs serialize through the Drive copy, and neither can clobber the other's audits or corrections (a
stale intent log alone would otherwise *revert* the other machine's manual edits at
replay). A pull failure still stops the run; `--force-push` skips adoption and declares
the local store (and log) authoritative. Simultaneous runs on both machines still race
the final push — the resolver re-converges the stores on the next runs, but overlap can
waste LLM work, so avoid it.

## Incremental processing & pruning

The audit is expensive (a local LLM call per row), so it does **not** re-process the whole
Plaid history every run. `src/incremental.py` diffs the current input (Plaid's truth) against
the prior categorized store and audits **only the delta**:

- **new** rows (unseen ids) and **changed** rows are audited;
- **unchanged** rows are carried forward verbatim — corrections *and* pending review flags
  survive untouched, so the model never re-flags a row you've already adjudicated;
- **removed** rows — a **pending row that settled** (Plaid drops the pending id and issues
  a new posted one) — are **pruned from both** the local committed file and the Drive copy.

Change detection is a stable content hash of each row's *raw Plaid fields only* (our own
provenance/review columns excluded), stamped on the record as `source_content_hash`. Two
safety guards: an **empty input** (e.g. a failed upstream fetch) is treated as a no-op,
never as "everything was deleted"; and the **prune gate** stops the run outright if a
**posted** row is missing from the input — posted rows never vanish upstream (stale ones
are only flagged), so that means a stale or truncated raw store, and pruning it would
shrink the shared store. Settled pendings are the only legitimate shrinkage. Use `--full` to force a complete re-audit (e.g. after editing the
rules in `config.py`); it still prunes removed rows. Note `--full` re-derives every row from
the pristine input, so prior in-place corrections are recomputed — decisions you **accepted
in review survive via merchant memory** (they re-apply as `auto` hits), **manual edit
intents survive via the replay stage**, but unadjudicated flags are re-raised fresh.

### The flagged-rows worklist

Every run (re)writes **`data/flagged_for_review.csv`** — a compact, spreadsheet-friendly list
of **every** row still pending review (current vs. suggested category, source, reason). It's
cumulative, not per-run, so it's a complete bulk to-do list you can work through whenever; run
`--review` to adjudicate, which shrinks the worklist as you go.

## Output schema

The full raw Plaid record (every original field) **plus 12 columns**, present on every row
(empty when unset) — provenance for *applied* changes, and review fields for *flags*:

| column | meaning |
|---|---|
| `original_pf_category_primary/detailed/confidence` | Plaid's original values (on an applied change) |
| `category_update_step` | `"mechanical"` \| `"llm"` \| `"review"` \| `"manual"` \| `""` |
| `category_update_reason` | rule name, LLM reason, review note, or manual intent id |
| `category_update_confidence` | corrector's confidence |
| `category_review_flag` | `"1"` when a suggestion is pending human review |
| `category_review_primary/detailed` | the suggested category (not yet applied) |
| `category_review_reason/confidence/source` | why it was raised, the suggester's confidence, and `"llm"`/`"mechanical"` |

The derived CSV = the 55 base columns (mirroring `transactions`, incl. `txn_owner`) + these 12. The JSONL also
carries a `source_content_hash` bookkeeping field (used for incremental change detection; not
in the CSV).

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install --require-hashes -r requirements.lock.txt   # hash-locked full tree (incl. pytest)
pip install --no-deps -e ../persister                    # editable sibling (not hash-pinnable)
```

Local LLM (optional but recommended):

```bash
ollama serve            # start the server
ollama pull qwen2.5:7b  # one-time model download
```

## Usage

```bash
# Default: read <data root>/transactions/data/transactions.jsonl (the collector's
# durable store), audit ALL rows with the DETERMINISTIC rules + sign guard (the local
# LLM is OFF by default), write the categorized outputs under the data root, push to Drive.
./.venv/bin/python categorize.py

# Fully offline (no Drive egress):
./.venv/bin/python categorize.py --no-drive

# Opt IN to the local LLM reviewer (needs Ollama; noisy — see LLM_ASSESSMENT.md):
./.venv/bin/python categorize.py --llm --no-drive

# Rules now, LLM later (rows stay pending so a later --llm run audits them):
./.venv/bin/python categorize.py --llm-defer

# Force a complete re-audit (e.g. after editing the rules in config.py):
./.venv/bin/python categorize.py --full --no-drive

# Standalone run against the collector's xz raw store:
./.venv/bin/python categorize.py --input ~/finance_data/transactions/data/transactions_raw.jsonl.xz --no-drive

# Adjudicate the rows the audit flagged (accept / reject / re-pick), then re-persist:
./.venv/bin/python categorize.py --review --no-drive

# Capture manual category edits (search a row → category → transaction/merchant scope):
./.venv/bin/python categorize.py --edit --no-drive

# Mine the accumulated manual edits for rule/LLM improvements (markdown to stdout):
./.venv/bin/python analyze_edits.py
```

Key flags: `--input`, `--out-jsonl`, `--out-csv`, `--flags-csv`, `--full`,
`--confidence LOW,MEDIUM,HIGH,VERY_HIGH,UNKNOWN`, `--memory PATH` / `--no-memory`,
`--llm` / `--no-llm` / `--llm-defer` (mutually exclusive; LLM off by default), `--no-drive`,
`--force-push` (skip the Drive head adoption: local store is authoritative),
`--review`, `--edit`, `--edits PATH`, `--claude-export` / `--claude-apply`
(+ `--claude-queue PATH` / `--claude-verdicts PATH`), `--log PATH`, `--debug`.

## Claude audit ritual

With the noisy local 7B off by default, the **periodic strong review** is done by Claude
out-of-band: export the rows it hasn't seen, let it judge them, apply its verdicts as
ordinary review flags. Claude is a **reviewer, not an author** — it only raises
`category_review_*` flags (`source="claude"`) that you adjudicate with `--review`; it never
overwrites a category. Each reviewed row is stamped `claude_audited_at`, so the next ritual
skips it (a settled pending row reappears under a new id; `--full` re-exports everything).

> ⚠ **Egress:** the ritual sends the exported rows (merchant names + amounts) to Anthropic —
> the deliberate trade for a much stronger reviewer. The always-on pipeline stays fully local.

```bash
# 1. Export the rows Claude hasn't reviewed → .secrets/claude_audit_queue.jsonl
#    (no Drive, no network; financial scratch stays in the gitignored .secrets/).
./.venv/bin/python categorize.py --claude-export

# 2. In a Claude Code session, have Claude read that queue and write verdicts to
#    .secrets/claude_audit_verdicts.jsonl — one JSON per line:
#      {"transaction_id": "...", "verdict": "flag", "primary": "FOOD_AND_DRINK",
#       "detailed": "FOOD_AND_DRINK_RESTAURANT", "reason": "..."}
#      {"transaction_id": "...", "verdict": "ok"}

# 3. Apply the verdicts as review flags, then re-persist (and push Drive unless --no-drive):
./.venv/bin/python categorize.py --claude-apply

# 4. Adjudicate the flags Claude raised (accept / reject / re-pick):
./.venv/bin/python categorize.py --review
```

## Tests

```bash
./.venv/bin/python -m pytest                       # fast, offline, deterministic (no LLM, no network)
./.venv/bin/python -m pytest integration_tests -s  # requires Ollama + qwen2.5:7b; threshold accuracy on a golden set
```

The integration suite auto-skips when Ollama isn't reachable and asserts *threshold*
accuracy (never exact-match) to tolerate minor hardware-driven LLM drift.

## The PFC taxonomy is vendored

`src/pfc_taxonomy.csv` is Plaid's published taxonomy, committed (not fetched at runtime —
offline-first). `pfc_taxonomy.py` parses it into `PRIMARY` / `DETAILED` / `GLOSS` and
validates the 16-primary invariant at import. To refresh: re-download the CSV over the
committed file and re-run the tests.

## Security

Follows the global baseline: secrets quarantined in `.secrets/` (0600 files, 0700 dir, atomic
writes); least-privilege Drive `drive.file` scope; offline-by-default with a loud notice
before any Drive egress; pinned dependencies in a dedicated venv; CSV formula-injection
guard on every derived cell. The local LLM is pinned to `localhost`.
