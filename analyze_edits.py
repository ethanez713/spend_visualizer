"""Periodic analysis of the manual-edit intent log → markdown report (stdout).

    ./.venv/bin/python analyze_edits.py [--edits PATH] [--min-count N]

Run this OCCASIONALLY (not per pipeline run) after manual edits have accumulated; read
the report; then make targeted, deliberate changes to ``src/config.py`` rules or the
LLM golden set. Nothing is changed automatically. See ``src/edit_analysis.py``.
"""
import argparse

from src.edit_analysis import report_markdown
from src.manual import DEFAULT_EDITS, load_intents


def main():
    ap = argparse.ArgumentParser(
        description="Mine data/manual_edits.jsonl for rule-promotion/demotion "
                    "candidates and an LLM scorecard (markdown to stdout).")
    ap.add_argument("--edits", default=DEFAULT_EDITS, metavar="PATH",
                    help="intent log to analyze (default: data/manual_edits.jsonl)")
    ap.add_argument("--min-count", type=int, default=2, metavar="N",
                    help="edits per merchant before suggesting a rule (default: 2)")
    args = ap.parse_args()
    print(report_markdown(load_intents(args.edits), min_count=args.min_count))


if __name__ == "__main__":
    main()
