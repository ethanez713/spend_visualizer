# Spend Analyzer — Build Plan

> Handoff doc. A fresh Claude Code instance should be able to execute from this
> without re-deriving decisions. Written in June 2026.

## 0. One-paragraph summary
A **standalone local web app** that reads the rich Plaid transaction archive
produced by the separate `transactions` collector and turns it into interactive
spending views. The signature feature is a **category-granularity control**
(discrete tiers in v1, a continuous top-K slider later). Categorization uses a
**faceted tag model** so a transaction can be placed many different ways without
ever being recategorized. No Google Sheets, no coupling to the `converter` repo.

**Stack:** Python · Streamlit · DuckDB · pandas · pyyaml. Local, single user.

---

## 1. Scope & non-goals
**In scope (v1):** ingest the raw archive → normalize/dedupe → faceted
categorization → 3 view groups (Drilldown core, Merchants & recurring, Cash
flow), all driven by a granularity selector + filters.

**Non-goals:** no Plaid calls (the collector owns that), no Google Sheets, no
writing back to the collector, no investment/balance/net-worth tracking, no
multi-user/auth/deploy. Keep it a simple local tool.

---

## 2. Architecture & component boundaries

Three **logical stages**, loosely coupled, each replaceable:

```
 ┌────────────────┐   raw archive file    ┌──────────────────┐   canonical store   ┌────────────────┐
 │  COLLECT       │ ───────────────────►  │  INGEST          │ ─────────────────►  │  ANALYZE       │
 │ (transactions  │  transactions_raw     │ load · dedupe ·  │  typed, source-     │ tags · cube ·  │
 │  repo, Plaid)  │  .jsonl.xz            │ normalize ·      │  agnostic rows      │ rollup · views │
 │  cron: any     │  (atomic write)       │ exclude/flag     │                     │ (Streamlit)    │
 └────────────────┘                       └──────────────────┘                     └────────────────┘
        owns Plaid                          owns the pipeline ops                    owns presentation
```

**Coupling rules (non-negotiable):**
- ANALYZE/INGEST never import collector code and never call Plaid.
- The **only** contract with the collector is the archive **file** (path +
  format). Read-only.
- The collector writes atomically (temp + rename). The analyzer reads read-only
  and caches by file mtime/size. ⇒ **the two run on totally independent
  schedules** (collector hourly/daily/monthly; analyzer on demand) with no
  locks, no IPC, no coordination. This is the answer to "don't tightly couple."

**Where does INGEST live?** For v1 it is an **isolated module inside this repo**
(`ingest/`), not a separate repo — but written behind a clean interface
(`sources → CanonicalTransaction[]`) so it can be **extracted into its own
`spend_pipeline` component later** with zero analyzer changes. Extract it when
either of these becomes true:
- you add a **second source** (another Plaid login, a manual CSV, a different
  aggregator), or
- a **second consumer** needs the canonical data (not just this app).

Until then, a separate repo would be premature. The seam is the
`CanonicalTransaction` schema below — keep it stable and source-agnostic.

---

## 3. Input contract (raw archive)
- Path: configurable; default `../transactions/transactions_raw.jsonl.xz`.
- Format: xz-compressed JSONL; one line per transaction = the full Plaid
  transaction object (`to_dict()`), keyed by `transaction_id`.
- Lossless: contains every field Plaid returns (incl. `location`,
  `counterparties`, `personal_finance_category`, `merchant_entity_id`,
  `authorized_date`, etc.). Treat this as the source of truth; never assume the
  flat CSV.
- Account metadata (name/mask/type/subtype/institution) is **not** in the
  transaction object. v1: read it from a small sidecar the collector can emit,
  or re-derive a `account_id → {mask,type,subtype,institution}` map. (If absent,
  the collector’s `/accounts/get` output can be exported once.)

---

## 4. Ingestion & normalization (the "pipeline ops")
Output = a list of **CanonicalTransaction** rows (source-agnostic, typed):

```
CanonicalTransaction
  transaction_id        account_id          institution
  posted_date           authorized_date     amount (float, signed)
  currency              name                merchant_name   merchant_entity_id
  pfc_primary  pfc_detailed  pfc_confidence
  counterparties[]      location{...}       payment_channel  website  logo_url
  account_name account_mask account_type account_subtype
  pending  pending_transaction_id
  # ingest-computed:
  flow: spend | income | excluded          dedupe_key
  raw: <the original object, retained>
```

Pipeline operations (all live here, documented + unit-tested):
1. **Load** all configured source files; decompress; parse.
2. **Dedupe by `transaction_id`** (re-assert uniqueness; the collector already
   does added/modified/removed, but never trust a single source).
3. **Pending vs posted:** drop pending rows that have a posted counterpart
   (match via `pending_transaction_id`); for aggregates exclude `pending=true`.
   Keep them only for an optional "recent/pending" strip.
4. **Sign & flow:** Plaid `amount` `+` = money **out**. Set
   `flow=spend` for outflows, `flow=income` for inflows on depository
   (paychecks / PFC `INCOME_*`).
5. **Exclusions (internal money movement):** `flow=excluded` for PFC
   `TRANSFER_IN_*`, `TRANSFER_OUT_*`, and `LOAN_PAYMENTS_CREDIT_CARD_PAYMENT`.
   ⚠️ **Keep** `LOAN_PAYMENTS_MORTGAGE_PAYMENT` — real spend. This is the #1
   double-count trap (CC payment from checking + the card’s purchases).
6. **(Future) transfer-pair netting:** match transfer-out/in between your own
   accounts and mark both excluded; **(future) cross-source dedupe** when a 2nd
   source is added.

---

## 5. Data model — the "cube"
- **Measures:** `spend`, `count`, `avg_ticket`, `income`, `net`.
- **Hierarchical dimensions** (ordered, strictly-nested *paths*): `category`,
  `geo`, `time`. Each can be rolled up to any depth by the rollup engine.
- **Flat facets** (single-valued ⇒ safe to group & sum): `person`, `channel`,
  `account_type`, `recurrence`, `necessity`, `confidence`.

This is an OLAP cube: one rollup engine answers "spend by `<any dims>` filtered
by `<any facets>`," which powers every view and both Sheets-style tables and
interactive charts.

---

## 6. Categorization — the rich faceted tag model

### 6.1 Two layers
- **Signals (raw, retained):** every field that could inform categorization —
  `pfc_primary/detailed/confidence`, `merchant_entity_id`, `merchant_name`,
  `counterparties[]`, `website`, `location`, amount sign, `account_type`,
  `payment_channel`. Never discarded; tags are always recomputed *from* signals,
  so overrides/regrouping are lossless and never require recategorizing data.
- **Tags (derived, structured):** what the renderer groups by.

### 6.2 The per-transaction tag object
```
TransactionTags
  # measures
  spend  count  flow

  # HIERARCHICAL dims — ordered, strictly-nested paths
  category: [Discretionary, Entertainment, Games]   # necessity = path[0]
  category_atom: ENTERTAINMENT_VIDEO_GAMES          # PFC detailed = stable leaf
  merchant: {id, name, brand}                        # deepest category node
  geo:  [International, MX, CDMX]                     # scope → country → city
  time: [2026, Q2, 2026-05, W21, Tue]

  # FLAT facets — single-valued ⇒ safe to group & sum
  person  channel  account_type  recurrence  necessity  confidence

  # OPTIONAL labels — multi-valued, FILTER-ONLY (see 6.4)
  labels: [gift, reimbursable, business]

  signals: { ...Layer 1... }
```

### 6.3 The atom & the tree
- **Atom = `personal_finance_category.detailed`** (100% populated, ~104 values),
  with `merchant_entity_id` (fallback normalized `merchant_name`) as the finer
  merchant leaf. Each transaction resolves to an atom **once**.
- The category path is **strictly nested by construction**: every atom maps to
  exactly one `tier2 → tier1 → necessity`. Strict nesting ⇒ clean rollup, no
  double-counting along the spine.
- **5 category levels:** `necessity (tier0) → tier1 (~dozen) → tier2 (~30) →
  atom/detailed (~104) → merchant (hundreds)`.

### 6.4 Multi-bucket placement WITHOUT double counting (the key rule)
A transaction participates in **every facet at once** (place it many ways), but
**within any single chosen lens it has exactly one value** (one category path,
one `person`, etc.) ⇒ sums always tie out.
- Group/sum **only** by exclusive dims (category path + flat facets).
- `labels` are multi-valued and **filter-only**; never a sum key — unless you
  opt into fractional allocation (split the amount across labels). v1:
  filter-only.

### 6.5 Synthesized categories (more powerful than a fixed tree)
Distinctions like "International Travel," "delivery vs dine-in," "streaming
subscriptions" are **NOT** hardcoded category levels. They are **compositions**
the renderer makes on demand by splitting a category node by a facet:
- International Travel = `category=Travel × geo=International`
- Streaming subs = `category=Entertainment × recurrence=recurring`
- Rideshares stay a real `tier2` (a true category refinement).

So keep `geo / recurrence / channel / person` as first-class facets — do **not**
bake them into `taxonomy.yaml`’s category tree.

### 6.6 `taxonomy.yaml` (auto-generated starter, hand-tuned)
```yaml
exclude_detailed: [TRANSFER_IN_*, TRANSFER_OUT_*, LOAN_PAYMENTS_CREDIT_CARD_PAYMENT]
atoms:                       # auto-generated for all ~104 PFC detailed codes
  FOOD_AND_DRINK_GROCERIES:            {tier0: Necessary,     tier1: Groceries,      tier2: Groceries}
  ENTERTAINMENT_VIDEO_GAMES:           {tier0: Discretionary, tier1: Entertainment,  tier2: Games}
  TRANSPORTATION_TAXIS_AND_RIDE_SHARES:{tier0: Necessary,     tier1: Transportation, tier2: Rideshares}
merchant_overrides:          # like the converter's PINNED_RULES (Uber Eats vs Uber)
  "<entity_id or name regex>": {tier1: ..., tier2: ...}
```
- **Bootstrap:** generate this file from Plaid’s published PFC taxonomy CSV so
  no one hand-writes 104 rows; the human then tunes labels/groupings.
- **Editing never recategorizes transactions** — atoms are stable; only the
  rollup changes. Hot-reload on file change (or a "reload taxonomy" button).
- **QC:** surface any atom missing from `taxonomy.yaml` (unmapped) and the
  `% of spend in "Other"`.

---

## 7. The rollup engine & granularity control

One **parameterized** engine — no per-view group-by code.

### 7.1 Grouping spec
```
# v1 discrete tier
group_by: [ (category, level: tier2) ]
filters:  { flow: spend, person: Alice, time.year: 2026 }

# future top-K (same data, different assigner)
group_by: [ (category, mode: topk, k: 12, expand_dims: [category, geo, recurrence]) ]
```

### 7.2 v1: discrete tiers
Selector picks `category` level ∈ {necessity, tier1, tier2, atom, merchant}.
Engine does `GROUP BY <level column>` in DuckDB. Trivial and fast.

### 7.3 Future: continuous top-K (already supported by the data model)
```
1. Build a tree from every txn's category path; node value = Σ spend of descendants.
2. frontier ← root's children
3. while len(frontier) < K and some frontier node is expandable:
       pop the largest-spend expandable node; replace with its children
4. each txn renders into its frontier-ancestor; non-expandable leaves stay as-is
```
- Needs only `category_path + spend` ⇒ **provably covered** by the schema.
- **Faceted expansion (advanced):** allow `expand_dims` to include `geo` /
  `recurrence`, so a node can split multiple alternative ways; pick the split by
  spend share / balance, or let the user choose. Needs the flat facets ⇒ also
  covered.
- **Stable bucket identity** across slider positions: identify a bucket by its
  path prefix / facet tuple so the UI can animate expand/contract.

### 7.4 Storage to enable all of the above
Store category **both** ways:
- flat **level columns** (`tier0..tier2`, `atom`, `merchant_id`) → fast group-by;
- a **`category_path` LIST column** → tree walking / top-K.
Same for `geo` and `time` (level columns + path). Materialize the enriched table
as DuckDB in-memory (cached Parquet).

---

## 8. Enrichment / derived dimensions
- **person** ← `accounts.yaml` (`account_id/mask → person`; `account_owner` is
  null in the data, so the human supplies it).
- **geo** ← `location.country` (+ non-USD currency) → scope (Domestic/Intl) →
  country → city.
- **recurrence** ← per-merchant cadence detection (regular interval + similar
  amount). Later: Plaid recurring-transactions product.
- **channel** ← `payment_channel`; **account_type** ← account meta.
- **time** ← `authorized_date` (fallback `posted_date`) → year/quarter/month/
  week/day-of-week.

---

## 9. Config files (human-owned, all YAML)
- `taxonomy.yaml` — atoms → tiers, merchant overrides, exclusions.
- `accounts.yaml` — account → person + friendly name + include?.
- `app.yaml` — archive path(s), home metro (future geo), trailing-avg window.
- **(future, for Sheet-parity view)** `budget.yaml` (per-period goals per tier-1
  category), `params.yaml` (inflation, salaries, take-home, savings),
  `history.yaml` or a converter-CSV importer for pre-Plaid actuals. See §14.

---

## 10. v1 Views (granularity selector + global filters: date, person, account_type, flow, channel, necessity)
**A. Drilldown core**
- *Budget/Spend table* — rows = categories at selected level; cols = Actual,
  trailing-N-mo running avg, (Goal / % / diff if `budget.yaml` present);
  conditional-format heatmap. Derived rows: Total, Annualized, Less-Mortgage,
  Less-Mortgage-and-Home.
- *Sunburst/treemap* — hierarchical spend, click-to-zoom (visual slider).
- *Trends* — spend over time (monthly/weekly), stacked by selected level,
  rolling-average overlay, optional YoY.

**B. Merchants & recurring**
- *Top merchants* — `logo_url` + name + total + visit count + avg + sparkline +
  first/last seen; filter by category/level.
- *Recurring/subscriptions* — detected cadence, monthly burden, last/next-expected.

**C. Cash flow**
- Monthly income vs spend vs net; savings = income − spend; cumulative net line.

---

## 11. Stack & module layout
```
ingest/        # sources → CanonicalTransaction[] (extractable later)
  load.py  normalize.py  dedupe.py
taxonomy.py    # load yaml + resolve atom→tags
enrich.py      # facets, geo, recurrence, time
cube.py        # DuckDB table + rollup engine (grouping spec)
views/         # drilldown.py  merchants.py  cashflow.py
app.py         # Streamlit shell: selector + filters + view tabs
config/        # taxonomy.yaml accounts.yaml app.yaml
```
- Cache `load+normalize+enrich` with `@st.cache_data` keyed on archive
  mtime/size; re-resolving tags after a `taxonomy.yaml` edit is cheap (no
  re-ingest).
- DuckDB over the enriched table for all group-bys.

---

## 12. Correctness / QC panel
- Unmapped atoms (atoms not in `taxonomy.yaml`).
- `% of spend in "Other"` (taxonomy coverage health).
- Count + sum of `flow=excluded` rows (transfers/CC payments).
- Totals tie-out: Σ(spend) == Σ over any single grouping (catches double-count).
- (Optional one-time trust check) reconcile tier-1 monthly totals vs the
  converter’s sheet — same kind of 1:1 check used to validate the collector.

---

## 13. Build phases
1. Scaffold (Streamlit + DuckDB + pyyaml) · config loading · read archive · "loaded N txns" page.
2. Ingest/normalize module + CanonicalTransaction + exclusions/flow + dedupe + QC counts.
3. Auto-gen `taxonomy.yaml` from PFC list · resolver · faceted tag object · unmapped-atom QC.
4. Enrich (person, geo, recurrence, channel, time) · materialize cube.
5. Rollup engine (grouping spec, discrete levels) + global filters.
6. Drilldown core (table + sunburst + trends).
7. Merchants & recurring.
8. Cash flow.
9. Polish: caching, taxonomy hot-reload, QC panel.

---

## 14. Future enhancements (designed-for, not v1)
- **Continuous top-K slider** + faceted expansion (§7.3) — renderer change only.
- **Trips & geo view** (map, trip auto-detect, domestic/intl) — uses `geo` dim +
  `home_metro`.
- **In-app taxonomy editor** (edit tiers/overrides in the UI).
- **Plaid recurring-transactions** product for better subscription detection.
- **Insights/anomalies**, pace-to-goal, projections, calendar heatmap,
  per-person comparisons.
- **Budget Summary view (Google-Sheet parity)** — *low lift, compatible.* The
  sheet’s main 2026 table is essentially Drilldown @ tier1 + goals + the derived
  cuts we already plan. To reach parity you’d add:
  - `budget.yaml` — per-period goals per tier-1 category (powers Goal / % /
    diff / heatmap).  *Low lift.*
  - `params.yaml` — inflation, salaries, take-home, est. savings (manual inputs,
    not derivable from transactions).  *Config only.*
  - **Pre-Plaid history** (2024/2025 actuals) — **cannot** come from Plaid (our
    data only goes back ~90 days / 24 mo max). Import the converter’s historical
    `_converted.csv` files or store summarized `history.yaml`.  *Importer, medium
    lift.*
  - **Out of scope:** the sheet’s **Asset Allocation** block is balances/holdings
    (equity/retirement/cash by person), a different data domain — investment
    accounts aren’t even linked. Keep it manual/separate; not a spend-analyzer
    concern.
  Verdict: the architecture is **not incompatible** — the budget table is a
  special case of the drilldown view. Adding it is additive config + one view +
  a history importer, not a redesign.

---

## 15. Human-provided inputs
`accounts.yaml` (which account = which person) · taxonomy tuning passes · (for
the future budget view) `budget.yaml`, `params.yaml`, and pre-Plaid history.
