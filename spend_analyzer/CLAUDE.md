# CLAUDE.md — spend_analyzer (Streamlit UI)

Read `../CLAUDE.md` first (golden rules). This app is READ-ONLY over the archive:
it never edits records — category fixes append INTENTS to the transformer's log.

- Tests: `./venv/bin/python -m pytest` (unit + headless AppTest UI; plain `test_*`
  naming here). Browser e2e is opt-in: `pytest tests/e2e -m e2e` (playwright).
- UI tests run the REAL app over the REAL archive read-only: write paths
  (intent log, corrections queue) are monkeypatched to tmp in tests/ui/conftest.py,
  and `tests/_liveguard.py` digests live files before/after every test. Any new
  test that presses a save/revoke button must go through those fixtures. Missing
  archive ⇒ skip, never error (fresh clones have no data).
- Config split: `config/app.yaml` + `config/taxonomy.yaml` are project config
  (repo); `accounts.yaml` + `budget.yaml` are personal (data root,
  `config_io.PERSONAL_CONFIG_DIR`). `SPEND_ANALYZER_CONFIG_DIR` redirects BOTH for
  tests. Relative `archive_paths` resolve from the data root.
- Pipeline: ingest (load → dedupe → normalize to CanonicalTransaction) → enrich
  (taxonomy tags + facets incl. `person` = record `txn_owner`, falling back to
  accounts.yaml) → cube (rollups). Streamlit caches key on archive mtime/size.
- The transformer's CODE is imported via `transformer_root` (taxonomy + intent
  validation in `manual_edits.py`; rule tables + match cascade in
  `rules_bridge.py`); its DATA paths come from the data root — don't conflate.
- The UI shows intents at once: `manual_edits.overlay` replays the live intent log
  over the loaded records with the transformer's own replay, so the app never waits
  for a categorize run. Anything new that a human decides in the UI is an intent in
  that log (overrides, review decisions, dismissals), never a second store.
- Rule surfaces are READ-ONLY by decision (docs/categorization-ux-ideation.md):
  the Rules tab and provenance explainers render the transformer's tables, its
  merchant memory, and the optional converter's Sheet mapping (loaded fail-soft
  via `$SPEND_VISUALIZER_CONVERTER` / `<data_root>/converter_root`) — never a
  write path. Rule authoring stays a code edit in the owning project.
- UI: the look and behaviour follow the Ethan design system (`~/ethan_design_system`,
  start at its `readme.md`); `theme.py` is the only place tokens are mapped, and views
  take colours and chart styling from it as `theme.<TOKEN>`, which resolves against
  the viewer's active theme (light or dark) — never a hex, never a theme branch. Sentence case, no emoji, Markdown money via
  `viz.money_md` (two bare `$` in one caption render as LaTeX).
- Only the active view renders (`app._current_view`), so a UI test opens the view it
  asserts on (`tests/ui/_harness.go`). Don't move views back into `st.tabs`: the
  hierarchy chart drops its labels when mounted inside a hidden tab panel.
- Tie-out invariant: every grouping must sum to the grand total (no double count);
  `tests/test_cube.py` checks it on the real archive.
