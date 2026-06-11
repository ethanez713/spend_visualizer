# CLAUDE.md — plaid_category_transformer (category auditor)

Read `../CLAUDE.md` first (golden rules: no live Drive calls, no data in-repo).

- Test: `./.venv/bin/python -m pytest` — offline; the LLM is injected as a fake
  (`tests/conftest.py FakeLLM`), Drive is stubbed.
- ALL categorization policy is data in `src/config.py` (rule tables, confidence
  selection, LLM authority). Logic lives in rules/llm/transformer/manual/schema.
- Authority order: mechanical `trust="auto"` rules overwrite → LLM flags (or
  auto-applies only per LLM_AUTHORITY on untrusted rows) → manual edit intents trump
  everything and are REPLAYED every run from the append-only intent log.
- Incremental engine (`src/incremental.py`): rows are re-audited only when their
  `source_content_hash` changes; the hash excludes our provenance columns AND
  `txn_owner` (`_NON_SOURCE_FIELDS`). Adding any record field that isn't Plaid
  content REQUIRES adding it there, or every row re-audits (slow, re-flags
  adjudicated rows).
- A run with the LLM requested but unreachable stamps `HASH_PENDING_LLM` so rows
  re-audit next run — don't "fix" that into a normal stamp.
- `check_drive_divergence` stops a push over a drifted remote; `--force-push`
  declares local authoritative. It also ignores `txn_owner` via metadata_fields.
- Data paths come from `src/paths.py` (data root); only `.secrets/` logs/memory and
  the vendored `src/pfc_taxonomy.csv` are repo-local.
