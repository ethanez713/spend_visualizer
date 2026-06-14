---
name: audit-transactions
description: Run the periodic Claude audit ritual for the plaid_category_transformer — in one pass, review uncategorized/suspicious transactions, run the store-wide quality sweeps, and propose deterministic rules — then leave clean flags for the human to adjudicate. Use when asked to audit transactions, review/clean up categorization, do the monthly spending review, or check for miscategorized transactions.
disable-model-invocation: true
---

# Audit transactions (Claude ritual)

You are the periodic STRONG reviewer for the categorizer (the local 7B is off by default).
You only **flag** suggestions and **propose** rules — never overwrite a category, never
auto-edit `config.py`, never push Drive. The human adjudicates and applies.

> ⚠ Egress: the exported rows (merchants + amounts) are read by you = sent to Anthropic;
> merchant web-lookups send the merchant name to a search engine. Both are expected here.

Run everything from `plaid_category_transformer/` (`./.venv/bin/python`). Deep mechanics:
that component's `README.md` + `CLAUDE.md`. Stop if the categorized store doesn't exist
(tell the user to run `categorize.py` first).

## Steps

1. **Export** (local, no network): `./.venv/bin/python categorize.py --claude-export`.
   Default reviews only NEW/unreviewed rows. If the request says to re-review everything /
   the full history / "all" (e.g. after a category or rule change — `/audit-transactions
   full`), add `--full`. Writes the review queue + the deterministic scan to `.secrets/`
   (gitignored, 0600).

2. **One judgment pass.** Read BOTH `.secrets/claude_audit_queue.jsonl` (rows you haven't
   reviewed) and `.secrets/claude_audit_scan.json` (store-wide findings) together, and
   produce every output below at once. Do a focused second pass only if something needs
   digging (a one-merchant cluster, an unidentifiable row). Write verdicts — one JSON per
   line — to `.secrets/claude_audit_verdicts.jsonl`:
   `{"transaction_id": "...", "verdict": "flag", "primary": "...", "detailed": "...", "reason": "..."}`
   or `{"transaction_id": "...", "verdict": "ok"}`. Verdict every queue row; add verdicts
   for scan rows that warrant a category change. Apply the [judgment rules](#judging-a-row).

3. **Apply** (local only — Drive push is the user's pipeline run):
   `./.venv/bin/python categorize.py --claude-apply --no-drive`. This turns your verdicts
   into `category_review_*` flags (`source=claude`) and stamps `claude_audited_at`.

4. **Propose rules** (don't edit `config.py` — put them in the report for the user to paste).
   Look across this run's flags + `./.venv/bin/python analyze_edits.py` + the scan's
   `entity_inconsistent` / `taxonomy_invalid` for recurring corrections. See
   [rule proposals](#proposing-rules). Judgment: only propose codifying a merchant seen
   enough times to be stable — otherwise say "collect more data."

5. **Report.** Print a concise summary: **rows needing the human's call** (what you flagged
   + why), what you marked OK, the sweep findings (taxonomy-invalid, sign violations,
   inconsistent merchants, uncategorized, stale pendings, outliers), and proposed rules.
   End with the next actions: `categorize.py --review` to adjudicate, then the normal
   pipeline run to push.

## Judging a row

- **Amount sign:** a POSITIVE amount is money OUT (debit) — never `INCOME_*`/`TRANSFER_IN_*`.
  A NEGATIVE amount is money in; usually a **refund that keeps the merchant's spend category**
  (a return stays `GENERAL_MERCHANDISE`), so only call it income/transfer-in for a genuine
  deposit/paycheck/inbound transfer.
- Identify the merchant's primary business as a whole phrase (don't anchor on one word).
  **Look up unfamiliar/ambiguous merchants on the web** before deciding; cite what you found.
- **Don't flag intra-tier-1 laterals** (same primary, only the detailed differs) — they
  don't move spend analysis and match the pipeline's own policy. Flag only real tier-1 moves
  or clearly-wrong details.
- Use only that row's own signals. When the current category is right, verdict `ok`. When a
  row is genuinely unidentifiable, say so in the report rather than guessing.
- Pick exact `(primary, detailed)` pairs from the vendored taxonomy (`src/pfc_taxonomy.csv`).

## Proposing rules

Rules live in `src/config.py`: `POS_PREFIX_RULES` (booking/POS text), `WEBSITE_RULES`
(domain), `KEYWORD_RULES` (token/phrase). Trust level:
- `auto` (overwrites in place) only for an **unambiguous** identifier — a stable
  `merchant_entity_id` or a distinctive full merchant phrase (e.g. a specific co-op).
- `flag` (suggests only) for a loose single keyword that could hit a sub-brand.

Give each proposed rule as a ready-to-paste table line + which merchants/rows it fixes.
Validate the pair against the taxonomy. For legacy `taxonomy_invalid` rows, propose the
current-taxonomy replacement (e.g. `INCOME_SALARY → INCOME_WAGES`).

## Non-negotiables

- Never overwrite a category or auto-edit `config.py` — flag / propose only.
- Never push Drive (`--claude-apply --no-drive`); never hard-delete anything.
- Queue / scan / verdicts stay in `.secrets/`. If you do change code, the offline test
  suite (`./.venv/bin/python -m pytest`) must pass.
