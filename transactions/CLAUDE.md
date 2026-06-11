# CLAUDE.md — transactions (Plaid collector)

Read `../CLAUDE.md` first (golden rules: no live Plaid/Drive calls, no data in-repo).

- Test: `./venv/bin/python -m pytest` — fully offline; the `state` fixture
  (tests/conftest.py) redirects every persisted path to tmp. Fake Plaid clients are
  canned-page classes, not mocks of the SDK.
- Path constants live in `src/plaid_client.py` (DATA_DIR/CSV_FILE resolve to the data
  root; tokens/cursors helpers are there too). Tests monkeypatch these module globals —
  keep new state as patchable constants/functions there.
- `sync_item` checkpoints the raw store AND the cursor after every page; keep that
  ordering (crash → page retried, never lost).
- Stamp `txn_owner` on every record entering the store. The two stamping points are
  `sync_item` and `fetch_window._fetch_window_items` — any new fetch path must stamp
  identically, or golden overwrites will strip ownership and reconcile parity breaks.
- Records in the store are PURE Plaid objects (+`txn_owner`): account metadata
  (institution/name) is joined only at CSV projection. Baking it in causes permanent
  spurious reconcile conflicts.
- `persist_runner.run_persist` is the only Drive-touching path; `--no-persist` /
  `--no-drive` keep runs local. It passes `metadata_fields=("txn_owner",)` to
  `persister.reconcile` — new metadata fields must be added there AND to the
  transformer's exclusions.
