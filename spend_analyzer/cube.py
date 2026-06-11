"""The OLAP cube + the single parameterized rollup engine (PLAN.md §5, §7).

One engine answers "spend by <any dims> filtered by <any facets>" — every view
calls this; there is no per-view group-by code. Backed by DuckDB over the
enriched DataFrame.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

import duckdb
import pandas as pd

# Hierarchical dimensions -> ordered level columns (shallow -> deep).
CATEGORY_LEVELS = ["tier0", "tier1", "tier2", "atom", "merchant"]
GEO_LEVELS = ["geo_scope", "geo_country", "geo_city"]
TIME_LEVELS = ["year", "quarter", "month", "week", "dow"]

# Friendly selector label -> column for the category granularity control.
CATEGORY_LEVEL_BY_NAME = {
    "necessity": "tier0",
    "tier1": "tier1",
    "tier2": "tier2",
    "atom": "atom",
    "merchant": "merchant",
}

# Flat facets safe to group & sum.
FACETS = ["person", "channel", "account_type", "recurrence", "necessity", "confidence"]

MEASURES_SQL = {
    "spend": "SUM(spend)",
    "income": "SUM(income)",
    "count": "SUM(count)",
    "net": "SUM(income) - SUM(spend)",
    "abs_amount": "SUM(abs_amount)",
}


@dataclass
class GroupingSpec:
    """Declarative rollup request (PLAN.md §7.1)."""
    group_by: list[str] = field(default_factory=list)        # columns
    filters: dict[str, Any] = field(default_factory=dict)    # col -> value | list
    date_from: Optional[str] = None                          # inclusive ISO
    date_to: Optional[str] = None                            # inclusive ISO
    measures: list[str] = field(default_factory=lambda: ["spend", "count"])
    order_by: Optional[str] = "spend"
    descending: bool = True
    limit: Optional[int] = None
    include_hidden: bool = False    # if False, rows flagged hidden are excluded


class Cube:
    def __init__(self, df: pd.DataFrame):
        self.df = df.copy()
        self.has_hidden = "hidden" in self.df.columns
        self.con = duckdb.connect(database=":memory:")
        self.con.register("txns", self.df)

    # ------------------------------------------------------------------ #
    def _where(self, spec: GroupingSpec) -> tuple[str, list]:
        clauses, params = [], []
        if self.has_hidden and not spec.include_hidden:
            clauses.append("hidden = FALSE")
        for col, val in spec.filters.items():
            if val is None or val == "" or val == "All":
                continue
            if isinstance(val, (list, tuple, set)):
                vals = [v for v in val if v not in (None, "All")]
                if not vals:
                    continue
                placeholders = ", ".join(["?"] * len(vals))
                clauses.append(f'"{col}" IN ({placeholders})')
                params.extend(vals)
            else:
                clauses.append(f'"{col}" = ?')
                params.append(val)
        if spec.date_from:
            clauses.append("date_resolved >= ?")
            params.append(spec.date_from)
        if spec.date_to:
            clauses.append("date_resolved <= ?")
            params.append(spec.date_to)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return where, params

    def rollup(self, spec: GroupingSpec) -> pd.DataFrame:
        measures = [m for m in spec.measures if m in MEASURES_SQL]
        if not measures:
            measures = ["spend", "count"]
        select_parts = [f'"{c}"' for c in spec.group_by]
        for m in measures:
            select_parts.append(f"{MEASURES_SQL[m]} AS {m}")
        # always expose count for avg_ticket convenience
        if "count" not in measures:
            select_parts.append("SUM(count) AS count")

        where, params = self._where(spec)
        sql = f"SELECT {', '.join(select_parts)} FROM txns{where}"
        if spec.group_by:
            sql += " GROUP BY " + ", ".join(f'"{c}"' for c in spec.group_by)
        order = spec.order_by if spec.order_by in (measures + ["count"]) else (measures[0])
        sql += f" ORDER BY {order} {'DESC' if spec.descending else 'ASC'}"
        if spec.limit:
            sql += f" LIMIT {int(spec.limit)}"

        out = self.con.execute(sql, params).df()
        if "spend" in out.columns and "count" in out.columns:
            out["avg_ticket"] = out.apply(
                lambda r: (r["spend"] / r["count"]) if r["count"] else 0.0, axis=1
            )
        return out

    def total(self, spec: GroupingSpec) -> dict[str, float]:
        """Grand totals (no group_by) for the same filters — for tie-out QC."""
        flat = GroupingSpec(
            group_by=[], filters=spec.filters,
            date_from=spec.date_from, date_to=spec.date_to,
            measures=["spend", "income", "count", "net"], order_by=None,
        )
        where, params = self._where(flat)
        sql = (
            "SELECT SUM(spend) spend, SUM(income) income, SUM(count) count, "
            "SUM(income)-SUM(spend) net FROM txns" + where
        )
        row = self.con.execute(sql, params).df().iloc[0].to_dict()
        return {k: (0.0 if pd.isna(v) else float(v)) for k, v in row.items()}

    def filtered(self, spec: GroupingSpec) -> pd.DataFrame:
        """Return raw filtered rows (for merchant/recurring tables)."""
        where, params = self._where(spec)
        return self.con.execute(f"SELECT * FROM txns{where}", params).df()

    def distinct(self, column: str) -> list:
        vals = self.con.execute(
            f'SELECT DISTINCT "{column}" FROM txns WHERE "{column}" IS NOT NULL '
            f'ORDER BY "{column}"'
        ).df()[column].tolist()
        return vals


# ---------------------------------------------------------------------- #
# Future top-K frontier (PLAN.md §7.3): provably covered by category_path.
# Implemented now because it powers click-to-zoom depth on the sunburst.
# ---------------------------------------------------------------------- #
def topk_frontier(df: pd.DataFrame, k: int, path_col: str = "category_path",
                  value_col: str = "spend") -> list[tuple]:
    """Greedy top-K: expand the largest expandable node until K buckets.

    Returns a list of path-prefix tuples identifying each frontier bucket.
    """
    # build tree node -> total value (sum over descendants)
    totals: dict[tuple, float] = {}
    children: dict[tuple, set] = {}
    for path, val in zip(df[path_col], df[value_col]):
        prefix: tuple = ()
        for node in path:
            parent = prefix
            prefix = prefix + (node,)
            totals[prefix] = totals.get(prefix, 0.0) + float(val)
            children.setdefault(parent, set()).add(prefix)

    frontier = list(children.get((), set()))
    if not frontier:
        return []
    while len(frontier) < k:
        # pick largest expandable node
        expandable = [n for n in frontier if children.get(n)]
        if not expandable:
            break
        biggest = max(expandable, key=lambda n: totals[n])
        frontier.remove(biggest)
        frontier.extend(children[biggest])
    frontier.sort(key=lambda n: totals[n], reverse=True)
    return frontier
