# CLAUDE.md — finance_pipeline (orchestrator)

Read `../CLAUDE.md` first. Deliberately thin: preflight + choreography ONLY, stdlib
only — domain logic stays in the components. `./run.py` runs fetch → categorize →
UI, each as a subprocess under its own venv, stopping on first non-zero exit
(components exit non-zero on unresolved raw-store conflicts or unreadable Drive
remotes — that stop-don't-persist behavior is the safety model; don't swallow
exit codes. The categorized store ADOPTS the Drive head instead of stopping:
two machines legitimately write it — see the transformer's CLAUDE.md).

- Test: `./venv/bin/python -m pytest` (house `given_*` naming; subprocesses faked
  via `fake_world`; the data push tested against a local bare repo, never a remote).
- `./run.py` is a LIVE run (Plaid + Drive + GitHub data push + a new Google Sheet
  when a converter is configured; the last two are default-on, opted out with
  `--no-push-data` / `--no-sheet`) — hand it
  to the user by default; execute it only on the user's explicit in-session
  go-ahead (see the root golden rules).
- The data steps run under `<data_root>/.pipeline.lock` (flock; same-machine
  overlap guard for timer + manual runs). It is released BEFORE the UI step —
  don't extend its scope, or a long-lived UI blocks the next day's scheduled run.
- `src/git_push.py` (the data-push step) runs after categorize, inside the
  lock, only on success — stop-on-failure covers the push. It commits with an
  automated identity and must stay loud-on-failure (the deploy timer's OnFailure
  alert relies on the non-zero exit). No-remote is a warn-and-continue, not an
  error (desktop repos may have no origin).
- `tools/migrate_multiuser.py`: one-off owner back-fill for pre-multi-user data
  (scan-then-apply, idempotent, aborts on foreign owners). Operates on tokens.json
  (repo .secrets) + the stores under the data root (`--data-root` to override).
- The incremental Sheet (`--sheet`, default-on when a converter is configured) is EXTRA
  egress on top of the live run: a second converter invocation with upload ON (a
  new Google Sheet) — same user-authorization rule as the rest of `./run.py`. An
  explicit `--sheet` without a converter fails preflight; the default just skips.
  It runs OUTSIDE the lock
  (read-only over the store; a slow Google API must not block the next scheduled
  run) and is non-fatal. The Sheet URL comes back via the converter's `--url-file`
  and opens as an extra browser tab together with `<data_root>/pinned_tabs` URLs.
  Invocation is one-directional (pipeline → converter): the converter's refresh.py
  no longer fetches, so never re-add a fetch flag to its invocations.
  The orchestrator passes the converter no window: what to export (everything earlier
  Sheets don't hold, pending rows and their reconciliation included) is the
  converter's `sheet_state` module's decision, tracked in
  `<data_root>/sheet_exports/manifest.jsonl`. Don't re-add window flags or pending
  handling here.
