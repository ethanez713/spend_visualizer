# Categorization & Manual-Override UX — Planning / Ideation

**Status:** ideation only — nothing here is committed to. Captures the current state of
the "how does a transaction get its category, and how do I override it" surfaces, the
friction in them, and a menu of features to make them cleaner and more legible.

Scope spans three components, because categorization authority is spread across all three:
- `plaid_category_transformer/` — the categorizer (rules → LLM/Claude flag → manual intents).
- `spend_analyzer/` — the UI where a human sees categories and files fixes.
- `converter/` — the separate PFC → budget-category mapping for the Google Sheet.

---

## 1. Current state — how a category is decided

Authority order (highest last), from `plaid_category_transformer/CLAUDE.md`:

1. **Mechanical rules** (`src/rules.py`) — merchant-entity-id memory → normalized
   merchant-name memory → POS prefixes → website hints → keyword/token rules. Each hit is
   a `RuleHit(primary, detailed, rule_name, confidence, trust)`. `trust="auto"` overwrites
   in place; `trust="flag"` only suggests.
2. **LLM / Claude review** — flags a suggestion for human adjudication (the local 7B is off
   by default; the Claude ritual is the strong reviewer). Never an author.
3. **Manual edit intents** (`manual_edits.jsonl`, append-only, replayed every run) — trump
   everything, survive `--full` re-audits, and (merchant-scope) cover future transactions.

Every categorized row already records **provenance** (`src/schema.py` `NEW_COLUMNS`):

| Column | Meaning |
|---|---|
| `category_update_step` | `mechanical` \| `llm` \| `review` \| `manual` \| `""` |
| `category_update_reason` | the **rule name**, LLM/review reason, or **manual intent id** |
| `category_update_confidence` | corrector's confidence |
| `original_pf_category_*` | the pre-override Plaid values (preserved) |
| `category_review_*` | a pending, not-yet-applied suggestion + its source |

**Key insight for everything below:** the data needed for a "why is this row this
category?" indicator and a rule↔row cross-reference *already exists on every row*. The UI
just doesn't surface it.

## 2. Current state — the override surfaces (and the friction)

The analyzer today offers **two different fix paths**, which is the main source of
confusion (`spend_analyzer/views/_widgets.py`):

- **Recategorize (PFC)** — `recategorize_form` → appends a manual **intent** to
  `manual_edits.jsonl`. Sticky, replayed, authoritative. This is the "real" override.
- **Other fix (report only)** — `correction_form` → appends to a **corrections report
  queue** (`corrections.jsonl`) that never changes anything; it's a triage note for later
  upstream/taxonomy work. Surfaced in the **Corrections** tab.

Plus, invisibly to the UI:
- The transformer's **rule tables** live in `plaid_category_transformer/src/config.py`
  (`KEYWORD_RULES`, `POS_PREFIX_RULES`, `WEBSITE_RULES`) + the merchant-memory JSON.
- The converter's **PFC → budget-category** rules live in `converter/src/config.py`
  (`PINNED_RULES`, `PFC_PRIMARY_MAP`, `PFC_DETAILED_MAP`, `PFC_DROP_*`) — a *fourth* place
  a "category" gets decided, only for the Google Sheet.

### Friction points
1. **Two fix paths, unclear boundary.** "Recategorize" vs "Other fix (report only)" both
   look like ways to fix a category; only one actually changes it. New-session-me forgets
   which is which.
2. **No provenance surfaced.** A row shows a category with no hint of *why* — mechanical
   rule? LLM? a manual override I made months ago? The answer is in the data, unshown.
3. **Rules are invisible & scattered.** To know what rules exist you read three `config.py`
   files across two repos. No way to see "which rule caught this row," or "what does rule X
   currently catch."
4. **Pending edits are only half-visible.** The Corrections tab shows the *report* queue;
   the actually-authoritative *manual intents* (`manual_edits.jsonl`) have a lighter
   presence (revoke exists, but there's no single "here is everything I've overridden and
   what it's doing" view).
5. **Override → rule promotion is manual.** A good one-off override (esp. merchant-scope)
   is often really a rule; today promoting it means hand-editing a transformer `config.py`.

---

## 3. Feature ideas

Grouped and tagged with rough **effort** (S/M/L) and **risk**. None are committed.

### A. Provenance indicator on every row  ·  effort S–M · risk low
Add a small badge/column to the transaction-detail and merchant tables showing
`category_update_step`: 🔧 rule · 🤖 LLM · 👁 review · ✏️ manual · (blank = Plaid default),
with the `category_update_reason` in the tooltip (rule name or intent id). Pure read of
existing columns — no new pipeline data. Immediately answers "why this category?".

*Prereq:* the analyzer's cube must carry these columns through ingest/enrich (they're on
the raw record; confirm they survive `ingest/normalize.py` or add them to the projection).

### B. Hardcoded-rule visualizer  ·  effort M · risk low
A new tab (or QC-tab section) that renders the rule tables as browsable data:
- transformer `KEYWORD_RULES` / `POS_PREFIX_RULES` / `WEBSITE_RULES` + merchant memory;
- converter `PINNED_RULES` / `PFC_*` maps (the Sheet side), clearly separated.
Show, per rule: its pattern, target category, `trust` (auto/flag), and **how many rows in
the current archive it currently matches**. Read-only first; editing is a later phase
(writing back to `config.py` is the hard part — see Open Questions).

### C. Indicator → rule cross-navigation  ·  effort M · risk med
Make the (A) badge clickable: 🔧 on a row jumps to (B) and highlights the matching rule
(join on `category_update_reason` == `rule_name`). And the reverse: from a rule in (B),
"show the N rows it caught." This is the "click the indicator, see the rule" idea. Needs a
stable rule identity shared between the row's `category_update_reason` and the rule table
(the `rule_name` on `RuleHit` already is that key — verify it's always populated).

### D. Pending / applied manual-edit visualizer  ·  effort S–M · risk low
A single "My overrides" view listing the live manual intents (`manual_edits.intents()`
already resolves revokes): scope (txn/merchant), from → to, note, when, and **what each is
currently affecting** (row count). Revoke inline (already supported). Folds the
authoritative overrides and the report queue into one mental model instead of two tabs.

### E. Unify the two fix paths  ·  effort M · risk med
Reframe the `_widgets.py` expander so the *primary* action is "Override category" (the
sticky intent) and the report path is demoted to a clearly-secondary "Flag for later
triage (doesn't change anything)". Removes the recurring "which one actually fixes it?"
confusion. Low code churn, mostly copy/IA; the risk is muscle-memory change.

### F. Promote an override to a rule  ·  effort L · risk high
From a merchant-scope override (or the rule visualizer), offer "make this a permanent
rule" that appends to the transformer's rule table. High value (closes the override→rule
loop) but high risk: it means **programmatically editing `config.py`** (or moving rules
out of `config.py` into a data file the UI can safely write). Probably gated behind
Open Questions #1.

### G. "Explain this category" popover  ·  effort S · risk low
Combine A + provenance into one hover/expander per transaction: the decision chain
(original Plaid → which stage changed it → final), the confidence, and the review status.
Cheapest way to make the whole system legible without new views.

---

## 4. Suggested phasing

1. **Phase 1 (legibility, low risk):** A (indicator) + D (override viewer) + E (unify fix
   paths). All reads/IA over existing data; makes the system self-explaining.
2. **Phase 2 (rule transparency):** B (rule visualizer, read-only) + C (cross-nav).
3. **Phase 3 (rule authoring):** F (promote override → rule) — only after deciding where
   editable rules should live (Open Questions #1).

## 5. Open questions

1. **Where should editable rules live?** Today they're Python literals in `config.py`
   (three files). A UI that *edits* rules wants them in a data file (YAML/JSON) the app can
   read+write safely (like `manual_edits.jsonl` / the taxonomy overrides). Moving them is a
   prerequisite for F and for any "edit rule" button in B. Do we want that, or keep rule
   authoring as code-only and have the UI be read-only (A–E) forever?
2. **One override model or two?** Should the report queue (`corrections.jsonl`) survive as a
   distinct concept, or collapse into "override now" + "note to self"? E assumes it stays
   but demoted.
3. **Converter rules in the same UI?** The Sheet's PFC→budget mapping is a genuinely
   separate policy (different category set, Sheet-only). Surfacing it in the same rule
   visualizer (B) risks conflating two taxonomies. Show it in a clearly-separate section,
   or leave it out of the analyzer entirely?
4. **Provenance completeness.** Confirm `category_update_step` / `_reason` are populated for
   *every* path (mechanical, LLM auto-apply, review-accept, manual replay) and survive the
   analyzer's ingest projection — A/C depend on it being reliable, not just usually present.

## 6. Grounding (where the current behavior lives)

- Authority order & flag policy — `plaid_category_transformer/CLAUDE.md`.
- Provenance columns & stamping — `plaid_category_transformer/src/schema.py` (`NEW_COLUMNS`,
  `set_provenance`).
- Mechanical rules & `RuleHit` — `plaid_category_transformer/src/rules.py`; rule tables in
  `plaid_category_transformer/src/config.py`.
- Manual-edit intents (analyzer side) — `spend_analyzer/manual_edits.py`; forms in
  `spend_analyzer/views/_widgets.py`; report queue in `spend_analyzer/corrections.py` +
  `views/corrections_view.py`.
- Sheet-side PFC → budget mapping — `converter/src/config.py`, `converter/src/plaid_bridge.py`.
