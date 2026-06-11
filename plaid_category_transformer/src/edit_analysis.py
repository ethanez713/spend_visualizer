"""Periodic, human-driven analysis of the manual-edit intent log → a markdown report.

Deliberately OUTSIDE the pipeline: nothing here runs during a categorize run, and
nothing is changed automatically. The intended loop is: accumulate manual edits for a
while → run ``analyze_edits.py`` → read the report → make targeted, deliberate edits to
``config.py`` rules (or the LLM prompt / golden set). Each intent's ``snapshot`` froze
the row's signals AND what the machines believed at edit time, so the log doubles as a
labeled dataset:

  * **promotion candidates** — merchants the human keeps fixing one transaction at a
    time, consistently: a deterministic rule (or one merchant-scope intent) would do it.
  * **demotion candidates** — rules/stages whose output the human keeps overriding
    (``before_step``/``before_reason`` of the overridden value).
  * **LLM scorecard** — on edited rows, did the LLM have a pending flag with the right
    answer (human just hadn't reviewed it), the wrong answer, or no flag at all? Misses
    are approximate: a row the LLM never saw also shows "no flag".
  * **Plaid confidence** — how often the original label was wrong at each confidence
    level (sanity-checks ``AUDIT_CONFIDENCE_LEVELS``).
"""
from __future__ import annotations

from collections import Counter, defaultdict

from .manual import resolve_intents
from .rules import normalize_merchant


def _merchant_label(it: dict) -> str:
    """A stable grouping key for the merchant behind an intent (name over entity id)."""
    snap = it.get("snapshot") or {}
    name = snap.get("merchant_name") or ""
    return normalize_merchant(name) or snap.get("merchant_entity_id") or "(unknown)"


def _target(it: dict) -> tuple[str, str]:
    return (it["set"]["primary"], it["set"]["detailed"])


def mine(entries: list[dict], *, min_count: int = 2) -> dict:
    """Aggregate raw log entries into the report's sections (pure; testable).

    Mines the APPLICABLE intents (revokes resolved out — a retracted edit is not
    evidence). Returns a dict of plain data; ``report_markdown`` renders it.
    """
    intents = resolve_intents(entries)
    n_edits_raw = sum(1 for e in entries if e.get("action", "edit") == "edit")

    by_merchant: dict[str, list[dict]] = defaultdict(list)
    for it in intents:
        if it["scope"] == "transaction":
            by_merchant[_merchant_label(it)].append(it)

    promote, conflicted = [], []
    for merchant, its in sorted(by_merchant.items()):
        targets = Counter(_target(i) for i in its)
        if len(its) < min_count:
            continue
        if len(targets) == 1:
            promote.append({"merchant": merchant, "count": len(its),
                            "target": its[0]["set"],
                            "website": (its[0].get("snapshot") or {}).get("website")})
        else:
            conflicted.append({"merchant": merchant,
                               "targets": {f"{p}/{d}": n for (p, d), n in targets.items()}})

    demote: Counter = Counter()
    for it in intents:
        snap = it.get("snapshot") or {}
        if snap.get("before_step"):  # a pipeline stage authored the overridden value
            demote[f"{snap['before_step']}: {snap.get('before_reason', '')}"] += 1

    scorecard = Counter()
    for it in intents:
        snap = it.get("snapshot") or {}
        pend = (snap.get("review_pending_primary"), snap.get("review_pending_detailed"))
        if not pend[0]:
            scorecard["no_flag"] += 1            # missed — or never saw the row
        elif pend == _target(it):
            scorecard["flag_right"] += 1         # the human confirmed the suggestion
        else:
            scorecard["flag_wrong"] += 1

    plaid_conf = Counter((it.get("snapshot") or {}).get("plaid_confidence") or "UNKNOWN"
                         for it in intents)

    return {
        "n_intents": len(intents),
        "n_revoked": n_edits_raw - len(intents),
        "by_scope": Counter(it["scope"] for it in intents),
        "by_source": Counter(it.get("source", "?") for it in intents),
        "promote": promote,
        "conflicted": conflicted,
        "demote": demote,
        "scorecard": scorecard,
        "plaid_confidence": plaid_conf,
    }


def report_markdown(entries: list[dict], *, min_count: int = 2) -> str:
    """Render the mined log as a markdown report with paste-ready rule snippets."""
    m = mine(entries, min_count=min_count)
    L = ["# Manual-edit analysis", ""]
    L.append(f"{m['n_intents']} applicable intent(s) ({m['n_revoked']} revoked) — "
             f"scope: {dict(m['by_scope'])}, source: {dict(m['by_source'])}.")
    L.append("")

    L.append(f"## Rule-promotion candidates (≥{min_count} consistent transaction edits)")
    L.append("")
    if not m["promote"]:
        L.append("None yet — keep collecting edits.")
    for c in m["promote"]:
        t = c["target"]
        L.append(f"- **{c['merchant']}** — fixed {c['count']}× to "
                 f"`{t['primary']}/{t['detailed']}`. One merchant-scope intent "
                 "(UI/`--edit`) makes it permanent; or paste a rule:")
        L.append("")
        L.append("  ```python")
        if c.get("website"):
            L.append(f"  # config.py → WEBSITE_RULES")
            L.append(f'  ("{c["website"]}", ("{t["primary"]}", "{t["detailed"]}"), "flag"),')
        else:
            L.append(f"  # config.py → KEYWORD_RULES (refine the keyword if too broad)")
            L.append(f'  ("{c["merchant"]}", ("{t["primary"]}", "{t["detailed"]}"), "flag"),')
        L.append("  ```")
    L.append("")

    if m["conflicted"]:
        L.append("## Inconsistent merchants (same merchant, different fixes — decide!)")
        L.append("")
        for c in m["conflicted"]:
            L.append(f"- **{c['merchant']}**: {c['targets']}")
        L.append("")

    L.append("## Rule/stage demotion candidates (their output got overridden)")
    L.append("")
    if not m["demote"]:
        L.append("None — manual edits only overrode Plaid's own labels.")
    for what, n in m["demote"].most_common():
        L.append(f"- {n}× `{what}`")
    L.append("")

    sc = m["scorecard"]
    L.append("## LLM scorecard on edited rows")
    L.append("")
    L.append(f"- flagged the **right** answer (just unreviewed): {sc.get('flag_right', 0)}")
    L.append(f"- flagged the **wrong** answer: {sc.get('flag_wrong', 0)}")
    L.append(f"- no flag at all (missed, or never ran on the row): {sc.get('no_flag', 0)}")
    L.append("")
    L.append("Wrong/missed cases are golden-set candidates (held-out eval, "
             "per the no-overfitting rule).")
    L.append("")

    L.append("## Plaid confidence of the overridden originals")
    L.append("")
    for conf, n in m["plaid_confidence"].most_common():
        L.append(f"- {conf}: {n}")
    return "\n".join(L)
