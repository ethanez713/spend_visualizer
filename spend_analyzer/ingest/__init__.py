"""INGEST stage: sources -> CanonicalTransaction[].

Designed behind a clean interface so it can be extracted into its own
``spend_pipeline`` component later (PLAN.md §2) with zero analyzer changes.
Never imports collector code; never calls Plaid. The only contract with the
collector is the archive *file*.
"""
from .pipeline import ingest, IngestResult

__all__ = ["ingest", "IngestResult"]
