# LLM Categorization — Assessment & Fix Plan

**Date:** 2026-06-12 · **Component:** `plaid_category_transformer` (Stage 2, the LLM auditor)
**Model:** `qwen2.5:7b` via local Ollama (`http://localhost:11434`), `temperature=0, seed=0`,
`LLM_BATCH_SIZE=1`, `LLM_AUTHORITY="flag"` (the LLM only *suggests*; it never auto-applies).
**Config:** `src/config.py` · **Prompt + I/O:** `src/llm.py` · **Flag/authority logic:** `src/transformer.py`.

## TL;DR

The local LLM auditor is **noisy and mis-calibrated**, and one fixable bug accounts for the
biggest slice of it: **it does not understand Plaid's amount-sign convention.** The prompt
lists "the amount sign" as a signal to cite but never says what it *means* (positive =
money out / debit; negative = money in / credit). The model consistently reads it
**backwards** — e.g. it tried to label eight outgoing brokerage `BUY` *purchases* as
`INCOME_WAGES` with the reason *"positive amount, indicating income."*

## How it was measured

Sample = the **126 rows the LLM flagged** on the real 520-transaction store (i.e. rows where
it disagreed with the mechanical rules and proposed a change). A human (this session)
adjudicated every one. **This is the precision of the LLM's flags, not its global accuracy** —
the mechanical rules silently handle the bulk, and `LLM_AUTHORITY="flag"` means the LLM only
ever produces suggestions for review. So "wrong" here = "a flag that wasted a human's time."

To reproduce: re-run `categorize.py` to regenerate flags, dump the `category_review_*`
columns per flagged row, and compare the suggestion to the adjudicated outcome.

## Headline numbers (n = 126 flagged)

| Outcome | Count | % |
|---|---|---|
| **Accepted** — LLM suggestion applied | 29 | 23% |
| **Re-picked** — LLM *also* wrong, human chose a third category | 20 | 16% |
| **Rejected** — existing category kept (suggestion not an improvement) | 68 | 54% |
| **Skipped** — genuinely ambiguous | 9 | 7% |
| **→ Suggestion NOT applied** | **88** | **70%** |

So roughly **1 in 4 flags was useful**; the rest were the model crying wolf.

## Error modes (ranked by impact)

### 1. Amount-sign confusion — the headline bug (~23 false flags, biggest single cause)
The LLM suggested `INCOME/INCOME_WAGES` on **45** rows:
- **22** on negative (inflow) amounts — real payroll (e.g. `<EMPLOYER> PAYROLL`, a federal
  salary deposit). 15 were accepted. **Correct.**
- **23** on positive (outflow) amounts — **impossible** for income (brokerage `BUY` ×~14,
  outgoing P2P transfers, etc.). All rejected/re-picked. **Pure error.**

Root cause: `src/llm.py` `_SYSTEM_PROMPT` (~line 110) and the output rules (~line 128) name
"the amount sign" as a citable signal but **never state the convention**. The model is fed
the raw `amount` (line ~163) and free-associates `positive → income`. A 7B model won't infer
the convention unaided.

### 2. Over-eager `FOOD_AND_DRINK_RESTAURANT` (16 of 23 wrong)
Suggested `RESTAURANT` on 23 rows; only 7 were improvements. It demoted coffee shops,
fast-food chains, convenience stores, and a **grocery co-op** to
"restaurant" on a bare merchant-name hunch. These are intra-`FOOD_AND_DRINK` laterals that
don't change tier-1 spend analysis — i.e. **low-value flags that still cost review time.**

### 3. Confidence is anti-calibrated — do not trust it
| Self-reported confidence | n | accepted |
|---|---|---|
| HIGH | 108 | 24% |
| MEDIUM | 15 | 20% |
| LOW | 3 | 0% |

`HIGH`-confidence suggestions were wrong **76%** of the time. The model's confidence token
carries **no usable signal**; do not gate any auto-apply on it.

### 4. Thin, low-information reasons
Top "reasons" are just field names: `merchant_name` (37×), `amount sign` (19×), `amount`
(14×). The model is pattern-matching one field, not reasoning — and "amount sign" as a
reason on a *backwards* call is actively misleading.

### 5. (Not the LLM's fault) legacy invalid taxonomy values inflate the flag count
The store still holds categories the current taxonomy rejects: `INCOME_SALARY`,
`INCOME_CONTRACTOR`, `OTHER_OTHER`, `TRANSFER_{IN,OUT}_*_FROM_APPS`. Rows carrying these
disagree with the rules on every run and keep getting flagged. Many "flags" are really
"current value is invalid," not "the LLM found something."

## Recommendations (prioritized)

1. **Teach the sign convention — do both belt and suspenders.** *(kills ~18% of all noise)*
   - **Prompt** (`src/llm.py _SYSTEM_PROMPT`): add an explicit line, e.g.
     *"AMOUNT SIGN: a positive amount is money LEAVING the account (a debit/purchase/
     payment); a negative amount is money ARRIVING (a credit/refund/income/deposit). Never
     assign an `INCOME_*` or `TRANSFER_IN_*` category to a positive amount, and never assign
     a spend category (FOOD_AND_DRINK, GENERAL_MERCHANDISE, …) to a negative amount."*
   - **Deterministic guard** (`src/transformer.py`, where the LLM suggestion becomes a flag):
     reject any suggestion that moves a **positive** row into `INCOME_*`/`TRANSFER_IN_*` or a
     **negative** row into a spend primary. A guard is model-agnostic and survives prompt
     drift; the prompt line improves the *positive* suggestions too.

2. **Stop using the model's confidence as a gate.** It's noise (see table). If you want a
   confidence signal, derive it from agreement across signals, not the model's self-report.

3. **Raise the flag threshold to cut low-value laterals.** Don't flag a suggestion that stays
   within the same tier-1 primary (e.g. `FAST_FOOD`↔`RESTAURANT`, `COFFEE`↔`RESTAURANT`)
   unless you specifically care about tier-2 food granularity. That alone removes most of
   error mode #2.

4. **One-time migration for the invalid legacy categories** (#5 above) so they stop
   generating perpetual flags: `INCOME_SALARY`→`INCOME_WAGES`, `*_FROM_APPS`→
   `*_OTHER_TRANSFER_*`, and re-audit the `OTHER_OTHER` rows once.

5. **Give the model more structured context and demand a structured reason.** It already
   receives `counterparties`, `payment_channel`, `website` (`src/llm.py` ~150-163) but its
   reasons ignore them. Require a rationale of the form *"<field>=<value> ⇒ <category>;
   sign check: <debit|credit> ⇒ consistent"* so flags are auditable and the sign check is
   forced into the chain.

6. **Try a stronger local model before reaching for the cloud.** `qwen2.5:7b` is small; test
   `qwen2.5:14b`/`32b-instruct` (or a comparable instruct model) on the same 126-row set and
   compare accept-rate. Keep it local by default — a cloud model means financial data leaves
   the machine and must stay an explicit opt-in.

## Success metric for the next iteration

Re-run the audit on the same store and check:
- **Zero** `INCOME_*` / `TRANSFER_IN_*` suggestions on positive amounts (and vice-versa).
- Flag **precision** (accepted ÷ flagged) up from **23%** toward ≥ 60%.
- Total flags down (fewer low-value laterals + fewer invalid-legacy re-flags).

## Already done this session (so you don't redo it)
- Adjudicated all 126 flags (29 accept / 20 re-pick / 68 reject / 9→then 8 resolved); only
  one masked row (a `transaction_type: special` charge) remains flagged — unidentifiable
  from pipeline metadata.
- Exercised the deterministic-rule escape hatch: a fully-qualified, distinctive grocery-store
  PHRASE can be added to `src/config.py KEYWORD_RULES` as an `"auto"` rule (a multi-word
  merchant name, not a sub-string that would over-match; rules tests green). Personal/local
  merchant rules stay in your private config, never the public defaults.
- Review results are **local-only** (`--no-drive`); the Drive/GitHub push of the store is the
  user's to run.
