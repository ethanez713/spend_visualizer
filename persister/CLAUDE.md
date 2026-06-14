# CLAUDE.md — persister (durable-store library)

Read `../CLAUDE.md` first. Pure library: NO data, paths, or secrets of its own —
every consumer passes its own paths + secrets_dir. Keep it that way; domain logic
(Plaid, ownership semantics) belongs in consumers, with at most generic hooks here
(e.g. `reconcile(..., metadata_fields=...)`).

- Test: `./.venv/bin/python -m pytest` (house `given_*` naming).
- Reconcile policy: preserve everything — remote-only rows are durable history and
  are NEVER deleted; conflicts keep the remote value by default (until a golden
  re-fetch overwrites via `merge_golden`), or the caller's `conflict_resolver`
  picks the winner per conflict (domain policy stays in consumers — the
  transformer passes a newest-audit-stamp rule); `metadata_fields` are excluded
  from conflict detection with the LOCAL copy winning.
- `DriveSync` is append-only BY CONSTRUCTION (`_GuardedService` blocks delete/trash);
  scope is `drive.file`; file ids persist in the consumer's
  `.secrets/drive_state.json`. Never weaken the guard.
- Two consumers install this editable: transactions and plaid_category_transformer.
  API changes must keep both suites green.
