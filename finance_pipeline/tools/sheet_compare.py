#!/usr/bin/env python3
"""Compare the Financials sheet's year tab with the app's ledger, row by row.

The month-by-month comparison while both workflows run: which transactions only one side
has, where the categories or people differ, what the totals and monthly averages come to
on each side, and which differences look like one repeatable rule.

    python3 tools/sheet_compare.py --xlsx ~/downloads/Financials.xlsx
    python3 tools/sheet_compare.py --xlsx F.xlsx --ledger other_ledger.csv --year 2026 --top 40

Download the Sheet as .xlsx first (File > Download); nothing here touches the network. The
app side is any converter-schema ledger CSV, by default the Budget tab's
``<data root>/spend_analyzer/data/budget_ledger.csv``. The comparison window starts at the
ledger's first row and ends at the sheet's last, since neither side covers more. Output is
printed and also written under ``<data root>/sheet_compare/`` (0600), one JSON line per
finding; it holds personal data, so it stays in the data root.
"""
from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

# A script run puts tools/ on sys.path, not the package root the imports below need.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import _data_root  # noqa: E402
from tools.sheet_ledger import Row, budget_tab, ledger_rows, year_tab_rows  # noqa: E402

# (day tolerance, minimum description similarity, label): tried in order, so a row takes
# its closest counterpart before a looser rule can steal it.
TIERS = ((0, 0.0, "exact"), (3, 0.25, "near"), (7, 0.45, "fuzzy"), (10, 0.0, "amount-only"))
SLACK_DAYS = 10
MASKED_SIMILARITY = 0.5


class Comparison(NamedTuple):
    first: date
    last: date
    pairs: list[tuple[Row, Row, str]]
    sheet_only: list[Row]
    ledger_only: list[Row]
    ledger_after_sheet: list[Row]
    sheet_rows: list[Row]
    ledger_rows: list[Row]
    warnings: int


def similarity(a: str, b: str) -> float:
    """Masked descriptions (a bank hiding the merchant as ``****``) say nothing, so they
    neither match nor mismatch on text."""
    if a.count("*") >= 4 or b.count("*") >= 4:
        return MASKED_SIMILARITY
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()


def match_rows(sheet: list[Row], ledger: list[Row]):
    pairs, sheet_left, ledger_left = [], list(sheet), list(ledger)
    for tolerance, minimum, tier in TIERS:
        unmatched = []
        for s in sheet_left:
            best = None
            for l in ledger_left:
                gap = abs((l.date - s.date).days)
                if l.net != s.net or gap > tolerance:
                    continue
                score = similarity(s.desc, l.desc)
                if score >= minimum and (best is None or (score, -gap) > best[0]):
                    best = ((score, -gap), l)
            if best is None:
                unmatched.append(s)
            else:
                pairs.append((s, best[1], tier))
                ledger_left.remove(best[1])
        sheet_left = unmatched
    return pairs, sheet_left, ledger_left


def compare(sheet: list[Row], ledger: list[Row], warnings: int = 0) -> Comparison:
    if not sheet or not ledger:
        raise ValueError("both the sheet tab and the ledger need at least one transaction")
    first, last = min(r.date for r in ledger), max(r.date for r in sheet)
    slack = timedelta(days=SLACK_DAYS)
    pairs, sheet_left, ledger_left = match_rows(
        [r for r in sheet if first - slack <= r.date <= last],
        [r for r in ledger if first <= r.date <= last + slack])
    return Comparison(
        first, last, pairs,
        sheet_only=[r for r in sheet_left if r.date >= first],
        ledger_only=[r for r in ledger_left if r.date <= last],
        ledger_after_sheet=[r for r in ledger if r.date > last and r not in
                            [p[1] for p in pairs]],
        sheet_rows=[r for r in sheet if first <= r.date <= last],
        ledger_rows=[r for r in ledger if first <= r.date <= last],
        warnings=warnings)


def category_totals(c: Comparison) -> dict[str, tuple[float, float]]:
    sheet, ledger = Counter(), Counter()
    for r in c.sheet_rows:
        sheet[r.category] += r.net
    for r in c.ledger_rows:
        ledger[r.category] += r.net
    return {cat: (sheet[cat], ledger[cat]) for cat in sorted(set(sheet) | set(ledger))}


def category_diffs(c: Comparison) -> list[dict]:
    groups = defaultdict(list)
    for s, l, _ in c.pairs:
        if s.category != l.category:
            groups[(l.category, s.category)].append((s, l))
    return sorted(
        ({"ledger_category": frm, "sheet_category": to, "rows": len(rows),
          "net": round(sum(s.net for s, _ in rows), 2),
          "examples": sorted({l.desc for _, l in rows})[:4]}
         for (frm, to), rows in groups.items()),
        key=lambda g: -g["rows"])


def source_diffs(c: Comparison) -> Counter:
    return Counter((l.source, s.source) for s, l, _ in c.pairs if s.source != l.source)


def merchant_key(desc: str) -> str:
    return " ".join(re.sub(r"[^a-z ]+", " ", desc.lower()).split()[:3])


def rule_suggestions(c: Comparison, min_rows: int = 3) -> tuple[list[dict], list[dict]]:
    """Merchants whose categories differ in at least ``min_rows`` rows. A merchant the
    sheet always moves the same way, with no row agreeing with the ledger, is a candidate
    rule; one with rows both ways is contested and left to a human."""
    by_merchant = defaultdict(list)
    for s, l, _ in c.pairs:
        key = merchant_key(l.desc)
        if len(key) >= 4 and s.desc.count("*") < 4 and l.desc.count("*") < 4:
            by_merchant[key].append((s, l))
    candidates, contested = [], []
    for key, rows in by_merchant.items():
        moved = [(s, l) for s, l in rows if s.category != l.category]
        if len(moved) < min_rows:
            continue
        directions = {(l.category, s.category) for s, l in moved}
        entry = {"merchant": key, "rows": len(moved), "agreeing_rows": len(rows) - len(moved),
                 "moves": sorted(f"{a} -> {b}" for a, b in directions)}
        (candidates if len(directions) == 1 and len(moved) == len(rows) else contested).append(entry)
    return (sorted(candidates, key=lambda e: -e["rows"]),
            sorted(contested, key=lambda e: -e["rows"]))


def duplicate_suspects(c: Comparison) -> list[tuple[Row, Row]]:
    """A sheet-only row that repeats a paired row (same amount, same merchant, a week
    apart): the usual shape of a pending charge exported once and again once settled."""
    paired = [s for s, _, _ in c.pairs]
    suspects = []
    for s in c.sheet_only:
        twin = next((m for m in paired if m.net == s.net and abs((m.date - s.date).days) <= 7
                     and similarity(m.desc, s.desc) >= 0.5), None)
        if twin:
            suspects.append((s, twin))
    return suspects


def sign_flip_suspects(c: Comparison) -> list[tuple[Row, Row]]:
    free, flips = list(c.ledger_only), []
    for s in c.sheet_only:
        for l in free:
            if l.net == -s.net and abs((l.date - s.date).days) <= SLACK_DAYS:
                flips.append((s, l))
                free.remove(l)
                break
    return flips


def complete_months(first: date, last: date) -> list[str]:
    months, cursor = [], date(first.year, first.month, 1)
    while cursor <= last:
        following = date(cursor.year + cursor.month // 12, cursor.month % 12 + 1, 1)
        if cursor >= first and following - timedelta(days=1) <= last:
            months.append(cursor.strftime("%Y-%m"))
        cursor = following
    return months


def monthly_averages(rows: list[Row], months: list[str] | None) -> dict[str, float]:
    """Average monthly net per category: over ``months`` when given, else over the months
    the rows themselves occupy (how the sheet divides)."""
    totals, seen = Counter(), set()
    for r in rows:
        month = r.date.strftime("%Y-%m")
        if months is None or month in months:
            totals[r.category] += r.net
            seen.add(month)
    divisor = len(months) if months is not None else len(seen)
    return {cat: total / divisor for cat, total in totals.items()} if divisor else {}


def averages_table(c: Comparison, published: dict[str, float]) -> list[dict]:
    complete = complete_months(c.first, c.last)
    sheet_present, ledger_present = monthly_averages(c.sheet_rows, None), monthly_averages(c.ledger_rows, None)
    sheet_complete = monthly_averages(c.sheet_rows, complete)
    ledger_complete = monthly_averages(c.ledger_rows, complete)
    cats = sorted(set(sheet_present) | set(ledger_present) | set(published))
    return [{"category": cat, "sheet_published": published.get(cat),
             "sheet_months_present": sheet_present.get(cat, 0.0),
             "ledger_months_present": ledger_present.get(cat, 0.0),
             "sheet_complete_months": sheet_complete.get(cat, 0.0),
             "ledger_complete_months": ledger_complete.get(cat, 0.0)} for cat in cats]


def _money(value: float) -> str:
    return f"{value:>10,.0f}"


def _row(r: Row) -> str:
    return f"{r.date} {r.source[:5]:5} {r.desc[:34]:34} {r.category[:16]:16} {r.net:>10,.2f}"


def render(c: Comparison, published: dict[str, float], top: int) -> str:
    tiers = Counter(t for _, _, t in c.pairs)
    early = sum(1 for s, _, _ in c.pairs if s.date < c.first)
    out = [f"Window {c.first} .. {c.last}",
           f"  sheet rows {len(c.sheet_rows)}   ledger rows {len(c.ledger_rows)} "
           f"(+{len(c.ledger_after_sheet)} newer than the sheet, {c.warnings} warning rows skipped)",
           f"  paired {len(c.pairs)} ({', '.join(f'{t} {tiers[t]}' for _, _, t in TIERS)})"
           + (f", {early} of them sheet rows dated just before the window" if early else "")
           + f"   sheet-only {len(c.sheet_only)}   ledger-only {len(c.ledger_only)}", "",
           "Net spend by category (positive = money out)",
           f"  {'category':28}{'sheet':>10}{'ledger':>10}{'ledger-sheet':>14}"]
    totals = category_totals(c)
    for cat, (s, l) in sorted(totals.items(), key=lambda kv: -abs(kv[1][1] - kv[1][0])):
        out.append(f"  {cat[:27]:28}{_money(s)}{_money(l)}{_money(l - s):>14}")
    out += ["", "Category differences (ledger -> sheet)"]
    for g in category_diffs(c)[:top]:
        out.append(f"  {g['rows']:>3} rows {g['net']:>10,.0f}  {g['ledger_category']} -> "
                   f"{g['sheet_category']}   e.g. {'; '.join(g['examples'])[:70]}")
    out += ["", "Person differences (ledger, sheet)"]
    out += [f"  {n:>4}  {a or '-'} / {b or '-'}" for (a, b), n in source_diffs(c).most_common(top)]
    out += ["", f"Sheet-only rows ({len(c.sheet_only)})"] + [f"  {_row(r)}  sheet row {r.ref}"
                                                           for r in c.sheet_only[:top]]
    out += ["", f"Ledger-only rows ({len(c.ledger_only)})"] + [f"  {_row(r)}" for r in c.ledger_only[:top]]
    out += ["", "Sheet defect suspects"]
    out += [f"  repeats row {t.ref} ({t.date}): {_row(s)}  sheet row {s.ref}"
            for s, t in duplicate_suspects(c)[:top]]
    out += [f"  sign flip: sheet {_row(s).strip()}  vs ledger {l.net:+,.2f} on {l.date}"
            for s, l in sign_flip_suspects(c)[:top]]
    candidates, contested = rule_suggestions(c)
    out += ["", "Rule candidates (merchant always moved the same way, >=3 rows)"]
    out += [f"  {e['merchant']:30} {e['rows']:>3} rows  {e['moves'][0]}" for e in candidates[:top]]
    out += ["", "Contested merchants (moved some rows, agreed on others, or moved two ways)"]
    out += [f"  {e['merchant']:30} {e['rows']:>3} moved / {e['agreeing_rows']} agree  "
            f"{'; '.join(e['moves'])}" for e in contested[:top]]
    out += ["", "Monthly average per category",
            f"  {'category':22}{'published':>10}{'sh present':>11}{'led present':>12}"
            f"{'sh complete':>12}{'led complete':>13}"]
    for a in averages_table(c, published):
        pub = "" if a["sheet_published"] is None else f"{a['sheet_published']:,.0f}"
        out.append(f"  {a['category'][:21]:22}{pub:>10}{a['sheet_months_present']:>11,.0f}"
                   f"{a['ledger_months_present']:>12,.0f}{a['sheet_complete_months']:>12,.0f}"
                   f"{a['ledger_complete_months']:>13,.0f}")
    return "\n".join(out)


def records(c: Comparison, published: dict[str, float]) -> list[dict]:
    def row(r: Row) -> dict:
        return {"date": r.date.isoformat(), "source": r.source, "description": r.desc,
                "category": r.category, "net": r.net, "note": r.note, "ref": r.ref}
    candidates, contested = rule_suggestions(c)
    found = [{"kind": "summary", "first": c.first.isoformat(), "last": c.last.isoformat(),
              "sheet_rows": len(c.sheet_rows), "ledger_rows": len(c.ledger_rows),
              "paired": len(c.pairs), "tiers": dict(Counter(t for _, _, t in c.pairs))}]
    found += [{"kind": "category_total", "category": k, "sheet": s, "ledger": l}
              for k, (s, l) in category_totals(c).items()]
    found += [{"kind": "category_diff", **g} for g in category_diffs(c)]
    found += [{"kind": "person_diff", "ledger": a, "sheet": b, "rows": n}
              for (a, b), n in source_diffs(c).items()]
    found += [{"kind": "sheet_only", **row(r)} for r in c.sheet_only]
    found += [{"kind": "ledger_only", **row(r)} for r in c.ledger_only]
    found += [{"kind": "duplicate_suspect", "row": row(s), "repeats": row(t)}
              for s, t in duplicate_suspects(c)]
    found += [{"kind": "sign_flip_suspect", "sheet": row(s), "ledger": row(l)}
              for s, l in sign_flip_suspects(c)]
    found += [{"kind": "rule_candidate", **e} for e in candidates]
    found += [{"kind": "contested_merchant", **e} for e in contested]
    found += [{"kind": "monthly_average", **a} for a in averages_table(c, published)]
    return found


def write_records(directory: Path, found: list[dict]) -> Path:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(r) + "\n" for r in found)
    return path


def main(argv=None) -> int:
    root = Path(__file__).resolve().parent.parent.parent
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--xlsx", required=True, type=Path, help="the downloaded Financials workbook")
    ap.add_argument("--year", type=int, default=date.today().year, help="year tab to compare")
    ap.add_argument("--ledger", type=Path, help="ledger CSV (default: the Budget tab's)")
    ap.add_argument("--data-root", type=Path, help="override the data root")
    ap.add_argument("--top", type=int, default=25, help="rows listed per section")
    ap.add_argument("--no-write", action="store_true", help="print only; write no JSONL")
    args = ap.parse_args(argv)

    data_root = args.data_root or _data_root(root)
    ledger_path = args.ledger or data_root / "spend_analyzer" / "data" / "budget_ledger.csv"
    ledger, warnings = ledger_rows(ledger_path)
    published = {cat: avg for cat, avg, _ in budget_tab(args.xlsx)}
    result = compare(year_tab_rows(args.xlsx, args.year), ledger, warnings)

    print(render(result, published, args.top))
    if not args.no_write:
        print(f"\nWrote {write_records(data_root / 'sheet_compare', records(result, published))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
