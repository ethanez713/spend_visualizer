#!/usr/bin/env python3
"""Compare the Financials workbook with a ledger, or --three-way across the workbook, the plaid Sheets and the app.

The month-by-month comparison while both workflows run: which transactions only one side
has, where the categories or people differ, what the totals and monthly averages come to
on each side, and which differences look like one repeatable rule.

    python3 tools/sheet_compare.py --xlsx ~/downloads/Financials.xlsx
    python3 tools/sheet_compare.py --xlsx F.xlsx --ledger other_ledger.csv --year 2026 --top 40
    python3 tools/sheet_compare.py --three-way --app-csv budget_rows.csv
    python3 tools/sheet_compare.py --three-way --xlsx F.xlsx --plaid-dir snapshots --app-csv A.csv

Download the Sheet as .xlsx first (File > Download); nothing here touches the network. The
two-way app side is any converter-schema ledger CSV, by default the Budget tab's
``<data root>/spend_analyzer/data/budget_ledger.csv``. The comparison window starts at the
ledger's first row and ends at the sheet's last, since neither side covers more. Output is
printed and also written under ``<data root>/sheet_compare/`` (0600), one JSON line per
finding; it holds personal data, so it stays in the data root.

``--three-way`` runs the same matcher on three pairs. M is every year tab of the workbook
(``--xlsx``, else the newest ``~/downloads/Financials*.xlsx``). P is every
``<data root>/sheet_edits/snapshots/*.head.csv`` with pending and warning rows left out.
A is the analyzer's budget-row export (``--app-csv``). Each pair uses the overlap of
the two date spans, with ``SLACK_DAYS`` past either edge so a posted date and its
authorized date still pair. A pair with no overlap is skipped.
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
from tools.sheet_ledger import (Row, WARNING_MARK, budget_tab, ledger_rows,  # noqa: E402
                                year_tab_rows)
from tools.xlsx_read import sheet_names  # noqa: E402

# (day tolerance, minimum description similarity, label): tried in order, so a row takes
# its closest counterpart before a looser rule can steal it.
TIERS = ((0, 0.0, "exact"), (3, 0.25, "near"), (7, 0.45, "fuzzy"), (10, 0.0, "amount-only"))
SLACK_DAYS = 10
MASKED_SIMILARITY = 0.5
# Master workbook, household-edited plaid Sheets, app budget rows.
PAIRS = (("M-P", "M", "P"), ("M-A", "M", "A"), ("P-A", "P", "A"))
FINDING_KINDS = ("only_one_side", "category_disagreement", "total_diff")
STALE_MASTER_DAYS = 7
TOTAL_DIFF_LIMIT = 1.0
LARGEST_TOTALS = 5
DEFAULT_DOWNLOADS = Path("~/downloads")


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


def compare(sheet: list[Row], ledger: list[Row], warnings: int = 0,
            window: tuple[date, date] | None = None) -> Comparison:
    """Match ``sheet`` against ``ledger``.

    ``window`` is ``(first, last)``. Omitted, the window runs from the ledger's first
    row to the sheet's last — the two-way report. Rows within ``SLACK_DAYS`` of either
    edge still pair: a posted date and its authorized date can sit on opposite sides
    of the boundary. The default window already starts at the earliest ledger row and
    ends at the latest sheet row, so those extra edges are empty there.
    """
    if not sheet or not ledger:
        raise ValueError("both the sheet tab and the ledger need at least one transaction")
    if window is None:
        first, last = min(r.date for r in ledger), max(r.date for r in sheet)
    else:
        first, last = window
    slack = timedelta(days=SLACK_DAYS)
    pairs, sheet_left, ledger_left = match_rows(
        [r for r in sheet if first - slack <= r.date <= last + slack],
        [r for r in ledger if first - slack <= r.date <= last + slack])
    return Comparison(
        first, last, pairs,
        sheet_only=[r for r in sheet_left if first <= r.date <= last],
        ledger_only=[r for r in ledger_left if first <= r.date <= last],
        ledger_after_sheet=[r for r in ledger if r.date > last and r not in
                            [p[1] for p in pairs]],
        sheet_rows=[r for r in sheet if first <= r.date <= last],
        ledger_rows=[r for r in ledger if first <= r.date <= last],
        warnings=warnings)


def category_totals(c: Comparison) -> dict[str, tuple[float, float]]:
    sheet: defaultdict[str, float] = defaultdict(float)
    ledger: defaultdict[str, float] = defaultdict(float)
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
    candidates: list[dict] = []
    contested: list[dict] = []
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
    totals: defaultdict[str, float] = defaultdict(float)
    seen: set[str] = set()
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


def master_rows(workbook: Path) -> list[Row]:
    """Every year tab. The master dates a row by its posted date."""
    years = sorted(int(name) for name in sheet_names(workbook) if re.fullmatch(r"\d{4}", name))
    return [row for year in years for row in year_tab_rows(workbook, year)]


def _plaid_kept(row: Row) -> bool:
    # The converter marks an unsettled charge PENDING and a stale row with a leading ⚠.
    return row.note != "PENDING" and not row.note.startswith(WARNING_MARK)


def snapshot_rows(directory: Path) -> list[Row]:
    """Every ``*.head.csv`` under ``directory``, pending and warning rows left out."""
    if not directory.is_dir():
        return []
    rows: list[Row] = []
    for path in sorted(directory.glob("*.head.csv")):
        got, _warnings = ledger_rows(path)
        rows.extend(row for row in got if _plaid_kept(row))
    return rows


def discover_workbook(downloads: Path) -> Path | None:
    """The newest ``Financials*.xlsx`` in ``downloads``, or None."""
    if not downloads.is_dir():
        return None
    found = [path for path in downloads.glob("Financials*.xlsx") if path.is_file()]
    if not found:
        return None
    return max(found, key=lambda path: (path.stat().st_mtime, path.name))


def _row_dict(row: Row) -> dict:
    return {"date": row.date.isoformat(), "source": row.source, "description": row.desc,
            "category": row.category, "net": row.net, "note": row.note, "ref": row.ref}


def _month_totals(rows: list[Row]) -> dict[tuple[str, str], float]:
    totals: dict[tuple[str, str], float] = defaultdict(float)
    for row in rows:
        totals[(row.category, row.date.strftime("%Y-%m"))] += row.net
    return {key: round(value, 2) for key, value in totals.items()}


def findings_for(pair: str, left_name: str, right_name: str, compared: Comparison) -> list[dict]:
    """Rows only one side has, category disagreements, and category × month totals that
    differ by more than a dollar. ``compared`` is one ``compare()`` of this pair."""
    found = [{"kind": "only_one_side", "pair": pair, "side": left_name, **_row_dict(row)}
             for row in compared.sheet_only]
    found += [{"kind": "only_one_side", "pair": pair, "side": right_name, **_row_dict(row)}
              for row in compared.ledger_only]
    found += [{"kind": "category_disagreement", "pair": pair, "tier": tier,
               "left_side": left_name, "right_side": right_name,
               "left_category": left.category, "right_category": right.category,
               "net": left.net, "left": _row_dict(left), "right": _row_dict(right)}
              for left, right, tier in compared.pairs if left.category != right.category]
    left_totals, right_totals = _month_totals(compared.sheet_rows), _month_totals(compared.ledger_rows)
    for category, month in sorted(set(left_totals) | set(right_totals)):
        left_total = left_totals.get((category, month), 0.0)
        right_total = right_totals.get((category, month), 0.0)
        difference = round(left_total - right_total, 2)
        if abs(difference) > TOTAL_DIFF_LIMIT:
            found.append({"kind": "total_diff", "pair": pair, "category": category, "month": month,
                          "left_side": left_name, "right_side": right_name,
                          "left": left_total, "right": right_total, "difference": difference})
    return found


def render_three_way(found: list[dict]) -> str:
    lines = []
    for pair, _left, _right in PAIRS:
        counts = Counter(item["kind"] for item in found if item["pair"] == pair)
        lines.append(f"{pair}  " + "  ".join(f"{kind} {counts[kind]}" for kind in FINDING_KINDS))
    lines += ["", "Largest total differences"]
    ranked = sorted((item for item in found if item["kind"] == "total_diff"),
                    key=lambda item: (-abs(item["difference"]), item["pair"],
                                      item["category"], item["month"]))
    lines += [f"  {item['pair']}  {item['category']}  {item['month']}  "
              f"{item['left']:,.2f} vs {item['right']:,.2f}  ({item['difference']:+,.2f})"
              for item in ranked[:LARGEST_TOTALS]]
    return "\n".join(lines)


def write_records(directory: Path, found: list[dict]) -> Path:
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = directory / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.jsonl"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.writelines(json.dumps(r) + "\n" for r in found)
    return path


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--xlsx", type=Path, help="the downloaded Financials workbook")
    ap.add_argument("--year", type=int, default=date.today().year, help="year tab to compare")
    ap.add_argument("--ledger", type=Path, help="ledger CSV (default: the Budget tab's)")
    ap.add_argument("--data-root", type=Path, help="override the data root")
    ap.add_argument("--top", type=int, default=25, help="rows listed per section")
    ap.add_argument("--no-write", action="store_true", help="print only; write no JSONL")
    ap.add_argument("--three-way", action="store_true",
                    help="cross-check the workbook, plaid Sheet snapshots, and the app export")
    ap.add_argument("--downloads", type=Path,
                    help="where to look for Financials*.xlsx when --xlsx is omitted (default: ~/downloads)")
    ap.add_argument("--plaid-dir", type=Path,
                    help="plaid Sheet snapshots (default: <data root>/sheet_edits/snapshots)")
    ap.add_argument("--app-csv", type=Path, help="budget rows exported by the analyzer")
    return ap


def _data(args) -> Path:
    return args.data_root or _data_root(Path(__file__).resolve().parent.parent.parent)


def _overlap(left: list[Row], right: list[Row]) -> tuple[date, date] | None:
    """Later start through earlier end, or None when the spans miss."""
    first = max(min(r.date for r in left), min(r.date for r in right))
    last = min(max(r.date for r in left), max(r.date for r in right))
    if first > last:
        return None
    return first, last


def run_two_way(args) -> int:
    data_root = _data(args)
    ledger_path = args.ledger or data_root / "spend_analyzer" / "data" / "budget_ledger.csv"
    ledger, warnings = ledger_rows(ledger_path)
    published = {cat: avg for cat, avg, _ in budget_tab(args.xlsx)}
    result = compare(year_tab_rows(args.xlsx, args.year), ledger, warnings)

    print(render(result, published, args.top))
    if not args.no_write:
        print(f"\nWrote {write_records(data_root / 'sheet_compare', records(result, published))}")
    return 0


def run_three_way(args) -> int:
    if args.xlsx is not None:
        workbook = args.xlsx
    else:
        downloads = (args.downloads or DEFAULT_DOWNLOADS).expanduser()
        workbook = discover_workbook(downloads)
        if workbook is None:
            print(f"no Financials workbook in {downloads} (looked for Financials*.xlsx)")
            return 0
    if args.app_csv is None:
        print("--three-way needs --app-csv", file=sys.stderr)
        return 2
    if not args.app_csv.is_file():
        print(f"no app export at {args.app_csv}", file=sys.stderr)
        return 1

    data_root = _data(args)
    modified = datetime.fromtimestamp(workbook.stat().st_mtime).date()
    print(f"{workbook} modified {modified.isoformat()}")
    sides = {
        "M": master_rows(workbook),
        "P": snapshot_rows(args.plaid_dir or data_root / "sheet_edits" / "snapshots"),
        "A": app_rows(args.app_csv),
    }
    newest = max((row.date for rows in (sides["P"], sides["A"]) for row in rows), default=None)
    # An old download still gets compared: the household may not have saved a fresh copy.
    if newest is not None and (newest - modified).days > STALE_MASTER_DAYS:
        print(f"master is {(newest - modified).days} days old")

    found: list[dict] = []
    for pair, left_name, right_name in PAIRS:
        left, right = sides[left_name], sides[right_name]
        if not left or not right:
            print(f"{pair} skipped ({left_name} {len(left)} rows, {right_name} {len(right)} rows)")
            continue
        window = _overlap(left, right)
        if window is None:
            print(f"{pair} skipped (no overlap)")
            continue
        found.extend(findings_for(pair, left_name, right_name,
                                  compare(left, right, window=window)))
    print(render_three_way(found))
    if not args.no_write:
        print(f"\nWrote {write_records(data_root / 'sheet_compare', found)}")
    return 0


def app_rows(path: Path) -> list[Row]:
    rows, _warnings = ledger_rows(path)
    return rows


def main(argv=None) -> int:
    ap = _parser()
    args = ap.parse_args(argv)
    if args.three_way:
        return run_three_way(args)
    if args.xlsx is None:
        ap.error("--xlsx is required")
    return run_two_way(args)


if __name__ == "__main__":
    sys.exit(main())
