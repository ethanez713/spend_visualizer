"""Which transaction ids the collector's CSV mirror still carries."""
from __future__ import annotations

import csv
from pathlib import Path


def load_live_ids(path: str | Path) -> set[str] | None:
    """Ids in the mirror's ``transaction_id`` column; ``None`` when the file is missing,
    unreadable, or has no such column, so a fresh clone filters nothing."""
    try:
        with Path(path).open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            if "transaction_id" not in (reader.fieldnames or []):
                return None
            return {row["transaction_id"] for row in reader if row.get("transaction_id")}
    except OSError:
        return None
