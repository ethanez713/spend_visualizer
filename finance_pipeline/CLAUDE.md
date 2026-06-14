# CLAUDE.md — finance_pipeline (orchestrator)

Read `../CLAUDE.md` first. Deliberately thin: preflight + choreography ONLY, stdlib
only — domain logic stays in the components. `./run.py` runs fetch → categorize →
UI, each as a subprocess under its own venv, stopping on first non-zero exit
(components exit non-zero on unresolved raw-store conflicts or unreadable Drive
remotes — that stop-don't-persist behavior is the safety model; don't swallow
exit codes. The categorized store ADOPTS the Drive head instead of stopping:
two machines legitimately write it — see the transformer's CLAUDE.md).

- Test: `./venv/bin/python -m pytest` (house `given_*` naming; subprocesses faked
  via `fake_world`; `--push-data` tested against a local bare repo, never a remote).
- `./run.py` is a LIVE run (Plaid + Drive; + GitHub with `--push-data`) — hand it
  to the user, never execute it.
- The data steps run under `<data_root>/.pipeline.lock` (flock; same-machine
  overlap guard for timer + manual runs). It is released BEFORE the UI step —
  don't extend its scope, or a long-lived UI blocks the next day's scheduled run.
- `src/git_push.py` (the `--push-data` step) runs after categorize, inside the
  lock, only on success — stop-on-failure covers the push. It commits with an
  automated identity and must stay loud-on-failure (the deploy timer's OnFailure
  alert relies on the non-zero exit). No-remote is a warn-and-continue, not an
  error (desktop repos may have no origin).
- `tools/migrate_multiuser.py`: one-off owner back-fill for pre-multi-user data
  (scan-then-apply, idempotent, aborts on foreign owners). Operates on tokens.json
  (repo .secrets) + the stores under the data root (`--data-root` to override).
