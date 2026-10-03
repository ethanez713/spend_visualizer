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
- Config split: `config/app.yaml` + `config/taxonomy.yaml` + `config/budget_model.yaml`
  are project config (repo); `accounts.yaml` + `budget.yaml` are personal (data root,
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
- Budget categories are a second layer over Plaid's: `budget_model.py` computes
  `budget_cat` for every row (tier-1 folded, pinned merchants, atom exceptions, PFC
  codes dropped whatever the merchant), and `budget_edits.py` lets the household
  override it per transaction, merchant or phrase, exclude a row, or add a note. Both
  kinds of budget edit, and `budget_adjust` rows (budget-only entries with no bank
  transaction), are intents in the SAME log the transformer replays; the transformer
  ignores them, so a budget edit never changes a Plaid category. Adjustments are
  applied in the Budget tab only, never to the cube. The Budget tab's `budget_amount`
  is the signed amount of any row that has a `budget_cat`, whatever its flow, which is
  how an edit counts a transfer.
- Review sign-offs are `budget_review` intents in the same log (`signoff.py`): each
  records the budget category every row had when confirmed. A row needs review until a
  live sign-off covers it with its current category, so a rule change or re-audit that
  moves a confirmed row brings it back as "changed"; the household's own newer budget
  edit does not. Review covers budget rows plus the off-budget atoms in `decide_atoms`
  (payment apps) — never card payments or own-account transfers, which would double
  count. Fixes made there are transaction budget edits; a merchant-wide rule is written
  only when the user accepts the offer.
- The Rules tab and provenance explainers are read-only over the transformer's rule
  tables, its merchant memory, and the optional converter's Sheet mapping (loaded
  fail-soft via `$SPEND_VISUALIZER_CONVERTER` / `<data_root>/converter_root`).
  A rule the user writes in the UI is a description-scope intent in the manual-edit
  log, never an edit to those tables; its form shows the rows it would cover before
  it saves, because a phrase one token short sweeps in other payees.
- UI: the look and behaviour follow the Ethan design system (`~/ethan_design_system`,
  start at its `readme.md`); `theme.py` is the only place tokens are mapped, and views
  take colours and chart styling from it as `theme.<TOKEN>`, which resolves against
  the viewer's active theme (light or dark) — never a hex, never a theme branch. Sentence case, no emoji, Markdown money via
  `viz.money_md` (two bare `$` in one caption render as LaTeX).
- Only the active view renders (`app._current_view`), so a UI test opens the view it
  asserts on (`tests/ui/_harness.go`). Don't move views back into `st.tabs`: the
  hierarchy chart drops its labels when mounted inside a hidden tab panel.
- The Budget tab merges imported sheet history (`legacy.py`; files written by
  `finance_pipeline/tools/import_sheet_history.py`) for dates before the archive's first
  transaction. Those rows carry no facets, so only the date and person filters apply to
  them, and only the Budget tab reaches back that far: widening the sidebar range would
  make every other view average over empty months.
- Tie-out invariant: every grouping must sum to the grand total (no double count);
  `tests/test_cube.py` checks it on the real archive.
