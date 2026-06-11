# Integration tests (manual, real Google Drive)

These are **not** run in normal `pytest` (they need real OAuth credentials and hit the
network). They verify a real Drive round-trip: create → push → pull → update.

## Prereqs
1. `.secrets/client_secret.json` present (see `_SETUP_HINT` in `src/persister/drive_sync.py`).
2. Deps installed: `./.venv/bin/pip install -r requirements.txt`.

## Run (guarded — this sends data to Google Drive)
```bash
./.venv/bin/python -m pytest integration_tests -s
```

⚠ This egresses data to Google Drive. Credentials in `.secrets/` are gitignored. Ask the
project owner before running a live Drive round-trip.
