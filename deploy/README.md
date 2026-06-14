# deploy

Everything needed to run the stack as an unattended service on a self-owned
Linux server: systemd **user** units, their wrapper scripts, an installer, and
the step-by-step **[RUNBOOK.md](RUNBOOK.md)** (start there).

The shape of the deployment:

- **UI** — `spend-analyzer.service` keeps Streamlit running, **loopback-only**;
  it is reached over the tailnet via `tailscale serve` (HTTPS), never the public
  internet. Login = being on the tailnet; the app itself has no auth code.
- **Data** — `finance-daily.timer` runs `bin/finance-daily.sh` each morning:
  `run.py --no-ui --no-llm --push-data` (fetch → deterministic-rules categorize
  → Drive push → git push of the data repo). The ~30-day overfetch safety net
  self-triggers inside the fetch — one timer covers both cadences. Deep review is
  the occasional desktop Claude audit ritual (RUNBOOK §12); both machines rebase
  onto the Drive head at run start, so neither clobbers the other.
- **Failure visibility** — `OnFailure=` fires `bin/finance-alert.sh`, which
  appends to `<data_root>/logs/failures.log`; full output is in journald.

## Artifacts

| File | Role |
|---|---|
| `RUNBOOK.md` | Ordered server bring-up + day-2 ops |
| `systemd/spend-analyzer.service` | Persistent UI, `127.0.0.1:8501`, auto-restart |
| `systemd/finance-daily.{service,timer}` | Daily oneshot data job, 06:30 ± 10m, catch-up after downtime |
| `systemd/finance-daily-alert.service` | OnFailure hook |
| `bin/finance-daily.sh` | The job the timer runs |
| `bin/finance-alert.sh` | Failure-log appender (resolves the data root like every component) |
| `install.sh` / `uninstall.sh` | Link/enable (or remove) the user units; sanity-checks first |

`transactions/run_fetch.sh` is superseded for server use — it only runs the
fetch; this job runs the full data path plus the pushes.

## Tests

```bash
python3 -m venv .venv && ./.venv/bin/pip install --require-hashes -r requirements.lock.txt   # once
./.venv/bin/python -m pytest -q
```

Offline artifact checks: shell syntax, unit-file shape (loopback bind,
`Persistent=true`, OnFailure wiring), script wiring, and drift guards that fail
if `finance_pipeline` drops a flag the wrapper passes. Nothing touches systemd,
the network, or live data.

## Security posture

Follows the global baseline. Units are user-level (no root); the UI never binds
a non-loopback address; the only new egress is the explicit `--push-data` git
push, authenticated by a deploy key scoped to the single private data repo.
