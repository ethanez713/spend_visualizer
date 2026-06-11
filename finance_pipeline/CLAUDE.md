# CLAUDE.md — finance_pipeline (orchestrator)

Read `../CLAUDE.md` first. Deliberately thin: preflight + choreography ONLY, stdlib
only — domain logic stays in the components. `./run.py` runs fetch → categorize →
UI, each as a subprocess under its own venv, stopping on first non-zero exit
(components exit non-zero on unresolved reconcile conflicts / Drive divergence —
that stop-don't-persist behavior is the safety model; don't swallow exit codes).

- Test: `./venv/bin/python -m pytest` (house `given_*` naming; subprocesses faked
  via `fake_world`).
- `./run.py` is a LIVE run (Plaid + Drive) — hand it to the user, never execute it.
- `tools/migrate_multiuser.py`: one-off owner back-fill for pre-multi-user data
  (scan-then-apply, idempotent, aborts on foreign owners). Operates on tokens.json
  (repo .secrets) + the stores under the data root (`--data-root` to override).
