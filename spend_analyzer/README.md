# Spend Analyzer

A **standalone local web app** that turns the rich Plaid transaction archive
produced by the separate `transactions` collector into interactive spending
views. Built to `PLAN.md`.

Signature feature: a **category-granularity control** (discrete tiers in v1,
continuous top-K later). Categorization uses a **faceted tag model** — a
transaction is placed many ways at once but has exactly one value within any
single lens, so sums always tie out.

**Stack:** Python · Streamlit · DuckDB · pandas · pyyaml · plotly. Local, single
user, **offline by default** (reads the archive read-only; never calls Plaid or
the network).

## Architecture (three loosely-coupled stages)

```
COLLECT (transactions repo, Plaid)  →  INGEST (this repo)  →  ANALYZE (this repo)
   owns Plaid                           load · dedupe ·         tags · cube ·
   the only contract is the             normalize · flow        rollup · views
   archive FILE (read-only)             CanonicalTransaction[]   (Streamlit)
```

INGEST/ANALYZE never import collector code and never call Plaid. The collector
writes atomically; the analyzer caches by file mtime/size ⇒ the two run on fully
independent schedules with no locks or coordination.

| Module | Role |
|---|---|
| `ingest/` | `sources → CanonicalTransaction[]` (load, dedupe, normalize). Extractable into its own component later. |
| `taxonomy.py` | Load `taxonomy.yaml`; resolve atom → category tags; exclusions; merchant overrides. |
| `enrich.py` | Resolve tags + facets (person, geo, recurrence, channel, time); finalize flow; build the cube DataFrame. |
| `cube.py` | DuckDB table + the single parameterized rollup engine + future top-K frontier. |
| `data.py` | Assemble configs → `Cube` + QC (pure, testable). |
| `views/` | `drilldown.py`, `merchants.py`, `cashflow.py`, `qc.py`. |
| `app.py` | Streamlit shell: granularity selector + global filters + tabs. |
| `config/` | `taxonomy.yaml`, `app.yaml` (project config). Personal config (`accounts.yaml`, `budget.yaml`) lives under the data root. |
| `tools/` | `gen_taxonomy.py`, `gen_accounts.py` bootstrap generators. |

## Setup

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt        # pinned exact versions

# bootstrap the human-owned config (idempotent; won't clobber edits)
./venv/bin/python -m tools.gen_taxonomy           # 110 PFC atoms → tiers
./venv/bin/python -m tools.gen_accounts           # account_id stubs
```

Then **edit `<data root>/spend_analyzer/config/accounts.yaml`** to map each
`account_id` to a `person`, friendly `name`, and `type` (`person` is pre-seeded
from each record's `txn_owner` stamp where present). Optionally tune category
groupings in `config/taxonomy.yaml` — editing it **never recategorizes data**;
only the rollup changes.

`config/app.yaml → archive_paths` points at the transaction archive to analyze;
relative paths resolve from the **data root**. Default: the **category-corrected**
store from `plaid_category_transformer`
(`plaid_category_transformer/data/transactions_categorized.jsonl`) — full raw
Plaid records with audited `personal_finance_category` values applied in place,
produced by the `../finance_pipeline` orchestrator (or by running `categorize.py`
directly). To analyze the uncorrected feed instead, point it at the collector's
`transactions/data/transactions_raw.jsonl.xz`.

## Run

```bash
./venv/bin/streamlit run app.py
```

Tabs: **Drilldown** (spend table with running avgs, sunburst/treemap/sankey with
click-to-zoom + transaction detail) · **Budget** (budget vs actual heatmap) ·
**Merchants & recurring** (top merchants with logos + subscription burden) ·
**Cash flow** (monthly income vs spend vs net + cumulative) · **Corrections**
(manual recategorize intents + the triage queue: upstream vs taxonomy fixes) · **QC**
(unmapped atoms, % in "Other", excluded totals, and a double-count tie-out check).

### Recategorize (PFC) — manual edits that stick

🚩 a transaction (Drilldown) or open a merchant detail (Merchants) → **Recategorize
(PFC)**: pick the correct category from the transformer's vendored taxonomy, scoped to
*just this transaction* or *ALL transactions from this merchant*. This app **never edits
records** — the edit is appended as an **intent** to the transformer's append-only log
(`<data root>/plaid_category_transformer/data/manual_edits.jsonl`), which its pipeline
replays on every categorize run: edits apply on the next run, survive full re-audits, and
merchant-scope edits cover future transactions too. Pending intents are listed (and can
be revoked) in the **Corrections** tab. The old report-only correction form remains for
merchant-name / tier-grouping fixes.

Use the **Category level** slider (necessity → tier1 → tier2 → atom → merchant)
and the global filters (date, person, account type, channel, flow, necessity).

## Tests

```bash
./venv/bin/python -m pytest -q                    # fast: unit + headless UI (~20 s)
./venv/bin/python -m pytest tests/e2e -m e2e -q   # browser e2e (~2 min, opt-in)
./venv/bin/python -m pytest -q --ignore=tests/ui  # unit tests only
```

Unit tests cover ingest dedupe/pending, taxonomy exclusions (incl. the
mortgage-vs-CC double-count trap), strict-nesting resolution, unmapped-atom
handling, and the **no-double-count tie-out invariant** on the real archive.

**UI suite** (`tests/ui/`): headless interaction tests via Streamlit's native
`streamlit.testing.v1.AppTest` (no browser, no server, no new dependencies).
Boots the real `app.py` over the real archive (read-only) and drives the
sidebar filters, granularity control, hide/unhide, drill (`wheel_root`)
navigation, the Recategorize-(PFC) intent form (save / no-op / revoke), and
the corrections queue (add / reject / delete), asserting cross-view tie-outs
and the exact intent payloads written. Safety by construction: an autouse
fixture redirects both write paths (`manual_edits.edits_path`,
`corrections.STORE`) into pytest tmp dirs, and a second fixture hashes the
live archive + intent log before/after every test and fails on any change.
(The plotly chart iframe and canvas grids are custom components AppTest cannot
click — those interactions live in the browser e2e suite below; the
click→drill-path *mapping* is unit-tested in `tests/test_drilldown.py`.)

**Browser e2e suite** (`tests/e2e/`, excluded from default runs via
`pytest.ini`): Playwright + Chromium drive the actually-served app for the
interactions AppTest cannot reach — real clicks on the plotly hierarchy chart
(click → drill lands on the clicked path, zoom-out/breadcrumb navigation that
must *stay* (the stale-click re-fire trap), treemap clicks, sankey render,
window switching) and canvas grid selections (spend-table row ticks → bulk
Hide/Flag with totals restored by Show-all, hidden categories dropped from the
chart, the 🚩 data-editor checkbox, the merchant-detail dropdown). Isolation is
environmental, since a subprocess can't be monkeypatched: the server gets
`SPEND_ANALYZER_CONFIG_DIR` / `SPEND_ANALYZER_DATA_DIR` pointed at throwaway
dirs (real archive read-only; the intent log + corrections queue land in pytest
tmp), and the same live-file hash guard wraps every test. One-time setup:
`./venv/bin/playwright install chromium`. Without sudo for the few system libs
Chromium needs, the suite auto-uses copies extracted under
`~/.cache/ms-playwright/extra-libs/`; `sudo ./venv/bin/playwright install-deps
chromium` is the cleaner permanent fix.

## Security posture

- **Offline by default.** No network egress; Streamlit telemetry disabled in
  `.streamlit/config.toml`.
- **No secrets in this repo.** Broad `.gitignore` patterns; the archive and any
  CSVs are git-ignored (never vendored in).
- **Pinned + hash-locked dependencies** in an isolated venv:
  `pip install --require-hashes -r requirements.lock.txt` (regenerate via
  `pip-compile --generate-hashes --allow-unsafe requirements.txt -o requirements.lock.txt`).

## Not in v1 (designed-for)

Continuous top-K slider (`cube.topk_frontier` already implements the algorithm),
geo/trips view, in-app taxonomy editor, Plaid recurring-transactions product,
budget-parity view (`budget.yaml` + history importer). See `PLAN.md §14`.
