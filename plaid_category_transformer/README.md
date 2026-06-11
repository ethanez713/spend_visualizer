# plaid_category_transformer

Audits **every** Plaid transaction **within Plaid's own Personal Finance Category (PFC)
taxonomy**, preserving the originals, recording which stage changed each row (or flagging
it for human review), and persisting the result via [`persister`](../persister).

Plaid tags every transaction with a PFC primary/detailed and a `confidence_level`, and
even **HIGH/VERY_HIGH** rows are sometimes wrong (e.g. Capital One Travel flights, `COT*FLT`,
landing in *postage & shipping*). So this tool audits **all** rows, but never lets the noisy
local model silently clobber a good category: **high-precision deterministic rules apply
in place; the LLM is a reviewer that flags disagreements for you to adjudicate.** It mirrors
[`converter`](../converter)'s flag-don't-overwrite pipeline.

**All categorization policy lives in [`src/config.py`](src/config.py)** — selection, the
hardcoded rule tables (with a per-rule `auto`/`flag` trust level), and the LLM knobs — so
what is auto-applied vs. flagged is readable at a glance.

> ⚠ **This repo commits real financial data** under `data/` on purpose (git + Drive dual
> audit history), so the GitHub repo **must be private**. Secrets stay in `.secrets/`
> (gitignored). The core path is **offline**; Drive sync and the LLM are local/opt-in.

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
3. **Stage 2 — local LLM** (`llm.py`) — Ollama `qwen2.5:7b` via `instructor` (JSON mode),
   `temperature=0, seed=0`, one row per call. It sees the schema, the full vendored taxonomy,
   every signal, and the mechanical suggestion — but it is a **reviewer, not an author**: by
   default it never overwrites, it only **flags** rows where it disagrees. Skips gracefully
   if Ollama is down. Tune with `config.LLM_AUTHORITY` (`"flag"` | `"apply_when_high"` |
   `"final"`). Trusted Plaid labels (HIGH/VERY_HIGH) are **never** auto-changed by the LLM.
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
7. **Persist** (`persister`) — `data/transactions_categorized.{jsonl,csv}` (committed),
   `data/flagged_for_review.csv` (the review worklist), `data/manual_edits.jsonl` (the
   intent log), optional Drive push of all four (default ON, `--no-drive`),
   `.secrets/{category_log,review_log}.jsonl`.

## Manual edit intents (sticky human edits)

A manual category edit is never written into the store directly — it is appended to
**`data/manual_edits.jsonl`** as an **intent**, and the final pipeline stage replays every
intent on every run. That makes edits sticky by construction: they survive `--full`
re-audits, upstream row changes, even a rebuilt store. Capture surfaces:

- **Spend Analyzer UI** (`../spend_analyzer`) — 🚩 a transaction → *Recategorize (PFC)*
  (it appends to this repo's intent log directly; nothing applies until the next run);
- **`--edit`** — interactive CLI: search a row, pick a category, pick scope, note. Applies
  immediately (and re-persists + pushes Drive, behind the same divergence gate).

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

When Drive sync is on, a **divergence gate** runs first: the remote categorized store is
pulled and reconciled against the local prior store. Content conflicts or remote-only rows
(an externally edited Drive copy, or a lost/reset local store) **stop the run before any
audit or write** — there is no golden source to repair the corrections from, so a human
must arbitrate. If the local store is the correct one (e.g. after `--no-drive` runs),
re-run with `--force-push`.

## Incremental processing & pruning

The audit is expensive (a local LLM call per row), so it does **not** re-process the whole
Plaid history every run. `src/incremental.py` diffs the current input (Plaid's truth) against
the prior categorized store and audits **only the delta**:

- **new** rows (unseen ids) and **changed** rows are audited;
- **unchanged** rows are carried forward verbatim — corrections *and* pending review flags
  survive untouched, so the model never re-flags a row you've already adjudicated;
- **removed** rows — a Plaid **hard delete**, or a **pending row that settled** (Plaid drops
  the pending id and issues a new posted one) — are **pruned from both** the local committed
  file and the Drive copy.

Change detection is a stable content hash of each row's *raw Plaid fields only* (our own
provenance/review columns excluded), stamped on the record as `source_content_hash`. A safety
guard: an **empty input** (e.g. a failed upstream fetch) is treated as a no-op, never as
"everything was deleted". Use `--full` to force a complete re-audit (e.g. after editing the
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

The derived CSV = the 54 base columns (mirroring `transactions`) + these 12. The JSONL also
carries a `source_content_hash` bookkeeping field (used for incremental change detection; not
in the CSV).

## Setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt        # installs -e ../persister too
pip install -r requirements-dev.txt    # pytest (for the test suite)
```

Local LLM (optional but recommended):

```bash
ollama serve            # start the server
ollama pull qwen2.5:7b  # one-time model download
```

## Usage

```bash
# Default: read ../transactions/data/transactions.jsonl (the collector's durable store),
# audit ALL rows, write data/*, push to Drive.
./.venv/bin/python categorize.py

# Fully offline (no Drive egress):
./.venv/bin/python categorize.py --no-drive

# Mechanical rules only (skip the LLM):
./.venv/bin/python categorize.py --no-llm --no-drive

# Force a complete re-audit (e.g. after editing the rules in config.py):
./.venv/bin/python categorize.py --full --no-drive

# Standalone run against the transactions xz raw store:
./.venv/bin/python categorize.py --input ../transactions/data/transactions_raw.jsonl.xz --no-drive

# Adjudicate the rows the audit flagged (accept / reject / re-pick), then re-persist:
./.venv/bin/python categorize.py --review --no-drive

# Capture manual category edits (search a row → category → transaction/merchant scope):
./.venv/bin/python categorize.py --edit --no-drive

# Mine the accumulated manual edits for rule/LLM improvements (markdown to stdout):
./.venv/bin/python analyze_edits.py
```

Key flags: `--input`, `--out-jsonl`, `--out-csv`, `--flags-csv`, `--full`,
`--confidence LOW,MEDIUM,HIGH,VERY_HIGH,UNKNOWN`, `--memory PATH` / `--no-memory`,
`--no-llm`, `--no-drive`, `--force-push` (override the Drive divergence gate),
`--review`, `--edit`, `--edits PATH`, `--log PATH`, `--debug`.

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
