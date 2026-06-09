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
| `config/` | `taxonomy.yaml`, `accounts.yaml`, `app.yaml` (human-owned). |
| `tools/` | `gen_taxonomy.py`, `gen_accounts.py` bootstrap generators. |

## Setup

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt        # pinned exact versions

# bootstrap the human-owned config (idempotent; won't clobber edits)
./venv/bin/python -m tools.gen_taxonomy           # 110 PFC atoms → tiers
./venv/bin/python -m tools.gen_accounts           # account_id stubs
```

Then **edit `config/accounts.yaml`** to map each `account_id` to a `person`,
friendly `name`, and `type` (Plaid leaves `account_owner` null, so this is
manual). Optionally tune category groupings in `config/taxonomy.yaml` — editing
it **never recategorizes data**; only the rollup changes.

`config/app.yaml → archive_paths` points at the transaction archive to analyze.
Default: the **category-corrected** store from `plaid_category_transformer`
(`../plaid_category_transformer/data/transactions_categorized.jsonl`) — full raw
Plaid records with audited `personal_finance_category` values applied in place,
produced by the `../finance_pipeline` orchestrator (or by running `categorize.py`
directly). To analyze the uncorrected feed instead, point it back at the
collector's `../transactions/data/transactions_raw.jsonl.xz`.

## Run

```bash
./venv/bin/streamlit run app.py
```

Tabs: **Drilldown** (spend table with running avgs, sunburst/treemap/sankey with
click-to-zoom + transaction detail) · **Budget** (budget vs actual heatmap) ·
**Merchants & recurring** (top merchants with logos + subscription burden) ·
**Cash flow** (monthly income vs spend vs net + cumulative) · **Corrections**
(triage queue for miscategorizations: upstream vs taxonomy fixes) · **QC**
(unmapped atoms, % in "Other", excluded totals, and a double-count tie-out check).

Use the **Category level** slider (necessity → tier1 → tier2 → atom → merchant)
and the global filters (date, person, account type, channel, flow, necessity).

## Tests

```bash
./venv/bin/python -m pytest -q
```

Covers ingest dedupe/pending, taxonomy exclusions (incl. the mortgage-vs-CC
double-count trap), strict-nesting resolution, unmapped-atom handling, and the
**no-double-count tie-out invariant** on the real archive.

## Security posture

- **Offline by default.** No network egress; Streamlit telemetry disabled in
  `.streamlit/config.toml`.
- **No secrets in this repo.** Broad `.gitignore` patterns; the archive and any
  CSVs are git-ignored (never vendored in).
- **Pinned dependencies** in an isolated venv. To hash-lock the full tree:
  `pip-compile --generate-hashes requirements.txt` → `pip install --require-hashes`.

## Not in v1 (designed-for)

Continuous top-K slider (`cube.topk_frontier` already implements the algorithm),
geo/trips view, in-app taxonomy editor, Plaid recurring-transactions product,
budget-parity view (`budget.yaml` + history importer). See `PLAN.md §14`.
