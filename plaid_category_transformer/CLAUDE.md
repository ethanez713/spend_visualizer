# CLAUDE.md — plaid_category_transformer (category auditor)

Read `../CLAUDE.md` first (golden rules: no live Drive calls without the user's
explicit in-session go-ahead, no data in-repo).

- Test: `./.venv/bin/python -m pytest` — offline; the LLM is injected as a fake
  (`tests/conftest.py FakeLLM`), Drive is stubbed.
- ALL categorization policy is data in `src/config.py` (rule tables, confidence
  selection, LLM authority). Logic lives in rules/llm/transformer/manual/schema.
- Authority order: mechanical `trust="auto"` rules overwrite → LLM flags (or
  auto-applies only per LLM_AUTHORITY on untrusted rows) → manual edit intents trump
  everything and are REPLAYED every run from the append-only intent log.
- **Flag-policy guards** (2026-06 LLM audit, `LLM_ASSESSMENT.md`), applied in
  `_decide`, suppress only review FLAGS (auto-applies are exempt): (1) **sign guard**
  `transformer._sign_violation` drops an LLM suggestion that puts a `config.INFLOW_PRIMARIES`
  category (INCOME/TRANSFER_IN) on a POSITIVE (outgoing) amount — sign-impossible;
  it's ONE-DIRECTIONAL on purpose (a negative amount on a spend primary is a normal
  refund, so it's never suppressed). (2) **lateral suppression**: an intra-tier-1
  change (same primary, only the detailed differs) isn't flagged unless
  `config.FLAG_INTRA_PRIMARY_LATERALS=True`. The LLM's self-reported confidence is
  anti-calibrated (HIGH was wrong 76% of the time) — don't gate auto-apply on it
  (`LLM_AUTHORITY="apply_when_high"` is discouraged).
- Incremental engine (`src/incremental.py`): rows are re-audited only when their
  `source_content_hash` changes; the hash excludes our provenance columns AND
  `txn_owner` (`_NON_SOURCE_FIELDS`). Adding any record field that isn't Plaid
  content REQUIRES adding it there, or every row re-audits (slow, re-flags
  adjudicated rows).
- **Local LLM is OFF by default** (`config.LLM_ENABLED_BY_DEFAULT=False`, 2026-06
  audit — the 7B was too noisy). A bare run is deterministic rules + sign guard
  only. Flags: `--llm` opts in, `--no-llm` is the explicit (and default) off,
  `--llm-defer` runs rules-only but keeps rows pending for a later `--llm` run;
  `transformer._resolve_llm_mode` maps the flags + config default to
  `(no_llm, defer_llm)`. Re-enabling later = flip the config flag / pass `--llm`
  (the sign guard is model-agnostic). `finance_pipeline` mirrors this (`--llm`
  passthrough; its Ollama preflight warning only fires under `--llm`).
- **Claude audit ritual** (`src/claude_audit.py`, `categorize.py --claude-export`
  / `--claude-apply`) replaces the noisy 7B as the periodic STRONG reviewer.
  `--claude-export` writes the rows Claude hasn't reviewed (posted, no
  `claude_audited_at` stamp; `--full` = all) to a local 0600 queue in `.secrets/`
  (no Drive, no network). Claude writes verdicts; `--claude-apply` turns them into
  ordinary `category_review_*` flags (`source="claude"`) — Claude is a REVIEWER,
  never an author; the human adjudicates via `--review`. It mirrors `review_run`
  (Drive head adopted first, push at the end unless `--no-drive`). ⚠ The ritual
  sends rows to Anthropic — opt-in egress; the pipeline stays local.
- `claude_audited_at` is a metadata column: like `txn_owner`/`category_audited_at`
  it's auto-excluded from the source hash (it's in `NEW_COLUMNS`, which
  `incremental._NON_SOURCE_FIELDS` covers), stripped in
  `transformer._audit_content_equal`, and a `reconcile(metadata_fields=…)` member —
  so it never triggers a re-audit or a two-writer conflict. Any future metadata
  column needs those same three spots.
- A run with the LLM requested but unreachable stamps `HASH_PENDING_LLM` so rows
  re-audit next run — don't "fix" that into a normal stamp. `--llm-defer` stamps
  the SAME sentinel deliberately (rules now, LLM on the next `--llm` run);
  `--no-llm` stamps normally (rules-only is final).
- `adopt_drive_head` rebases every Drive-enabled run onto the Drive head BEFORE
  any audit/write: remote-only rows taken, local-only kept, conflicts resolved by
  `_newer_audit_wins` — the side with the newer `category_audited_at` stamp wins,
  so a local store AHEAD of the head (offline session, failed push) never loses
  work; ties/missing stamps fall back to remote. BOTH versions of every conflict
  are appended to `<data>/…/adopt_conflicts.jsonl` first (git-pushed daily —
  nothing is silently discarded; deliberately NOT under logs/, which is
  gitignored). `txn_owner` and `category_audited_at` are metadata_fields (never
  read as conflicts). The manual-edits intent log is union-merged in the same
  step (`_adopt_remote_edits`); store and log MUST move together, or replay
  reverts the other machine's corrections. Pull failure still stops the run;
  `--force-push` skips adoption (local authoritative). Don't reintroduce
  stop-on-divergence — "remote ahead" is routine with two writers.
- **Prune gate**: posted rows never vanish upstream (only settled pendings do),
  so `run()` STOPS — before any audit/write/push — if the input is missing a
  POSTED row the store has. This is what keeps a stale/truncated raw store from
  shrinking the shared head. Don't weaken it; a test legitimately needing a
  prune must use a `pending=True` row.
- **Stamp invariant**: `category_audited_at` moves only when audit content
  actually changes — `stamp_audited_at` is called from `set_provenance` /
  `set_review_flag` / `clear_review_flag` on real changes only, and `run()`'s
  stamping loop INHERITS the prior stamp when reprocessing reproduced identical
  content (processed rows are rebuilt from raw, so the stages re-stamp them; the
  inherit step undoes that). Breaking this lets a stale machine's no-op re-runs
  outrank the other machine's real work at adopt time. Two-writer e2e coverage:
  `tests/test_two_writer.py` (shared in-memory Drive, both machines run the real
  `run()` path).
- Data paths come from `src/paths.py` (data root); only `.secrets/` logs/memory and
  the vendored `src/pfc_taxonomy.csv` are repo-local.
