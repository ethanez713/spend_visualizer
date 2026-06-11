# Plan: decouple "necessity" from the category tree (make it an orthogonal facet)

**Status:** planned, not started (deferred 2026-06-09). Current behavior preserved:
`TRANSPORTATION_TAXIS_AND_RIDE_SHARES` stays `tier0: Necessary` until this lands.

> ⚠ **Repo note:** the necessity model lives entirely in **`../spend_analyzer`**, not in
> this repo. This plan is filed here only because that's where it was requested; the
> actual implementation + tests are all in `spend_analyzer`. Move this doc there if
> preferred.

## Problem

A user wants rideshare (Uber/Lyft) to count as **Discretionary** spend while keeping its
correct PFC category (`TRANSPORTATION_TAXIS_AND_RIDE_SHARES`, i.e. tier1 *Transportation*).
Today that's impossible:

- "Necessity" is not its own facet — it **is `tier0`**, the root of the category tree
  (`tier0 → tier1 → tier2 → atom → merchant`). `enrich.py` sets `necessity = tags.tier0`.
- The strict-nesting invariant (`tests/test_features.py::test_strict_nesting_still_holds`)
  requires **each tier1 → exactly one tier0**, so the sunburst/treemap stays a true tree
  and the no-double-count tie-out holds.
- Therefore tier1 *Transportation* cannot sit under both *Necessary* and *Discretionary*.
  Making rideshare Discretionary by editing its `tier0` breaks the invariant (verified: the
  test fails).

The user's mental model — "Transportation can be in **both** Discretionary and Necessary" —
is a **facet** model: necessity is orthogonal to the category hierarchy, exactly like the
existing person / channel / geo / recurrence facets (PLAN.md §6.5). The current design
conflates the two.

## Goal

Make **necessity an independent, per-atom facet** decoupled from the category tree, so any
atom can carry any necessity while keeping its category path. Tie-out must continue to hold
on **both** lenses independently (each atom → exactly one category path, and → exactly one
necessity value).

## Design sketch (spend_analyzer)

1. **`config/taxonomy.yaml`** — split the two concerns:
   - Give the category tree a real root that is NOT necessity. Options: (a) introduce a
     `domain` top level (e.g. *Spending* / *Income* / *Transfer*), or (b) make `tier1` the
     top and drop `tier0` from the path. (b) is the smaller change.
   - Add a per-atom `necessity` field (`Necessary | Discretionary | Income | Transfer`),
     OR a compact `necessity:` lookup keyed by atom (most atoms inherit a default per
     primary; only the exceptions like rideshare are listed). Keep it terse.
   - Rideshare → `necessity: Discretionary`, category path unchanged (Transportation/Taxis).

2. **`taxonomy.py`** — `resolve()` returns `necessity` as a **separate field** on
   `ResolvedTags`, independent of `category_path`. `category_path` no longer starts with
   necessity. Keep the unmapped-atom fallback (default necessity per primary).

3. **`enrich.py`** — emit a `necessity` column from the atom's necessity (not `tier0`).
   Set the category columns from the new root downward.

4. **`cube.py`** — `CATEGORY_LEVELS` currently is `['tier0','tier1','tier2','atom','merchant']`.
   Replace the `tier0` root with the new category root (or start at `tier1`). `necessity`
   becomes a **filter facet only**, not a drill level (it already is a sidebar multiselect
   and a `GroupingSpec` filter — that keeps working, just reading the new column).

5. **`app.py` / `views/drilldown.py`** — necessity stays a global filter (no hierarchy
   change needed there beyond the new `CATEGORY_LEVELS`). Optionally offer a
   "necessity-rooted" view as a toggle if the necessity sunburst is still wanted.

6. **`views/budget.py`** — budget goals are keyed by `tier1`; unaffected.

## Invariants / tie-out

- **Category lens:** each atom → exactly one `[root → tier1 → tier2]` path (unchanged tree;
  update `test_strict_nesting_still_holds` to assert tier1 → one *category root* parent).
- **Necessity lens:** each atom → exactly one necessity value (add a test). Both lenses sum
  to the same grand total (the existing no-double-count tie-out test should be extended to
  group by `necessity` and assert it ties out, just like the category rollup).

## Effort & rationale

- **Effort:** medium — ~5 files + test updates in spend_analyzer; no data reprocessing
  (taxonomy edits never recategorize, only re-roll).
- **Why this over the quick hack** ("give rideshare its own `tier1: Rideshare` under
  Discretionary"): the hack keeps the invariant but **removes rideshare from the
  Transportation group** in the wheel, which the user explicitly did not want ("its
  transportation/rideshare classification is correct"). The facet model is the design
  PLAN.md already endorses for every other cross-cutting dimension, so necessity should
  match.
