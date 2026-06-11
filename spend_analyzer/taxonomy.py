"""Load taxonomy.yaml and resolve a transaction's atom -> category tags.

Tags are always recomputed *from* signals (PLAN.md §6.1), so editing the YAML
never requires recategorizing data. Resolution is pure and cheap.
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

DEFAULT_PATH = Path(__file__).resolve().parent / "config" / "taxonomy.yaml"

# Fallback necessity per primary, used when an atom is missing from the YAML.
_PRIMARY_TIER0 = {
    "INCOME": "Income",
    "TRANSFER_IN": "Transfer",
    "TRANSFER_OUT": "Transfer",
}


@dataclass
class ResolvedTags:
    tier0: str
    tier1: str
    tier2: str
    atom: str                      # PFC detailed — stable leaf
    category_path: list[str]       # [tier0, tier1, tier2]
    excluded: bool
    unmapped: bool                 # atom absent from taxonomy.yaml (QC signal)


@dataclass
class Taxonomy:
    atoms: dict[str, dict] = field(default_factory=dict)
    exclude_detailed: list[str] = field(default_factory=list)
    merchant_overrides: dict[str, dict] = field(default_factory=dict)
    _override_res: list[tuple[re.Pattern, dict]] = field(default_factory=list)

    def is_excluded(self, detailed: Optional[str]) -> bool:
        if not detailed:
            return False
        return any(fnmatch.fnmatchcase(detailed, pat) for pat in self.exclude_detailed)

    def _merchant_override(
        self, merchant_name: Optional[str], merchant_entity_id: Optional[str]
    ) -> Optional[dict]:
        for pat, spec in self._override_res:
            if merchant_entity_id and pat.fullmatch(merchant_entity_id):
                return spec
            if merchant_name and pat.search(merchant_name):
                return spec
        return None

    def resolve(
        self,
        detailed: Optional[str],
        primary: Optional[str] = None,
        merchant_name: Optional[str] = None,
        merchant_entity_id: Optional[str] = None,
    ) -> ResolvedTags:
        atom = detailed or "OTHER_OTHER"
        spec = self.atoms.get(atom)
        unmapped = spec is None
        if spec is None:
            # derive a sane default so the renderer never crashes on a new code
            tier0 = _PRIMARY_TIER0.get(primary or "", "Discretionary")
            tier1 = (primary or "OTHER").replace("_", " ").title()
            tier2 = "Other"
        else:
            tier0 = spec.get("tier0", "Discretionary")
            tier1 = spec.get("tier1", "Other")
            tier2 = spec.get("tier2", "Other")

        # merchant overrides refine tier1/tier2 only (atom + tier0 stay stable)
        ov = self._merchant_override(merchant_name, merchant_entity_id)
        if ov:
            tier1 = ov.get("tier1", tier1)
            tier2 = ov.get("tier2", tier2)

        return ResolvedTags(
            tier0=tier0,
            tier1=tier1,
            tier2=tier2,
            atom=atom,
            category_path=[tier0, tier1, tier2],
            excluded=self.is_excluded(detailed),
            unmapped=unmapped,
        )

    def known_atoms(self) -> set[str]:
        return set(self.atoms.keys())


def load_taxonomy(path: str | Path = DEFAULT_PATH) -> Taxonomy:
    p = Path(path)
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    overrides = data.get("merchant_overrides") or {}
    compiled = []
    for pat, spec in overrides.items():
        try:
            compiled.append((re.compile(pat), spec))
        except re.error:
            # fail-soft on a bad regex rather than abort taxonomy load
            continue
    return Taxonomy(
        atoms=data.get("atoms") or {},
        exclude_detailed=data.get("exclude_detailed") or [],
        merchant_overrides=overrides,
        _override_res=compiled,
    )
