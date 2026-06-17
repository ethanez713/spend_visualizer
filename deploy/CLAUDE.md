# CLAUDE.md — deploy (server artifacts)

Read `../CLAUDE.md` first (golden rules). This component is **static artifacts
only** — unit files, wrapper scripts, runbook. There is no app code here.

- **Don't enable/start these units or run `bin/finance-daily.sh` by default** — that
  is a LIVE run (Plaid + Drive + GitHub push). Same rule as `./run.py`: hand it to
  the user unless they explicitly authorize it in-session (see the root golden rules).
  `install.sh` mutates the user's systemd state — also user-run by default.
  `bin/finance-alert.sh` is safe **only** with `SPEND_VISUALIZER_DATA` pointed
  at a tmp dir (the tests do this).
- Test: `./.venv/bin/python -m pytest` (offline artifact checks; house `given_*`
  naming). The drift guards in `tests/test_artifacts.py` tie the wrapper's flags
  to `finance_pipeline/src/pipeline.py` — if a flag is renamed there, fix the
  wrapper, not just the test.
- Unit paths are `%h/spend_visualizer/...` by design (user-level units on a
  standard `~` clone; `install.sh` enforces the location). Don't introduce
  absolute `/home/...` paths — a test rejects them.
- The schedule lives in `finance-daily.timer` only; the monthly overfetch is NOT
  scheduled here (it self-triggers inside the fetch — see
  `transactions/src/overfetch.py`).
- RUNBOOK.md is user-facing and ordered; keep §-references in README/scripts in
  sync when renumbering.
