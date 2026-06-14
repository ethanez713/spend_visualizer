"""Stage 2 — local-LLM categorizer (Ollama + instructor + pydantic).

Adapted from ``converter/src/reviewer.py`` ``LLMAuditor``: same runtime
(``instructor.from_openai`` over Ollama's OpenAI-compatible endpoint, JSON mode),
same model/sampling (``qwen2.5:7b``, temperature=0 seed=0), batching per
``config.LLM_BATCH_SIZE`` (currently 1 — see config.py for why multi-row batches
drifted), the same ``_ensure_ready``/``_ping``/``_ensure_model`` startup, and the same
TTY-aware ``_spinner``. It runs on ALL selected rows (regardless of Stage 1) and is the final
authority for them — it sees the most context. If Ollama is unavailable the whole stage
skips gracefully (returns ``{}``) and the caller falls back to the mechanical result.

The system prompt is the single source of truth for: (1) the row/signal schema
(``schema.SCHEMA_PROMPT``), (2) the full vendored PFC taxonomy
(``pfc_taxonomy.taxonomy_block``), and (3) the output rules. Integration tests build
``CategoryLLM`` from this module's constants so prod and tests can't diverge.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from contextlib import contextmanager
from typing import Optional

from pydantic import BaseModel

from . import pfc_taxonomy
from .config import LLM_BATCH_SIZE, LLM_HOST, LLM_MODEL, LLM_SAMPLING
from .schema import SCHEMA_PROMPT

# Model configuration (LLM_MODEL / LLM_HOST / LLM_SAMPLING / LLM_BATCH_SIZE) lives in
# config.py — the single source of truth for prod AND the integration tests. Re-exported
# here so callers/tests can keep importing them from ``src.llm``.
__all__ = ["LLM_MODEL", "LLM_HOST", "LLM_SAMPLING", "LLM_BATCH_SIZE", "CategoryLLM",
           "CategoryDecision", "CategoryAudit"]

_DEBUG = False


def _dbg(msg: str):
    if _DEBUG:
        print(f"  [DEBUG] {msg}", flush=True)


# ── Spinner (copied from reviewer.py) ─────────────────────────────────────────

_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"


@contextmanager
def _spinner(label: str, eta_s: Optional[float] = None):
    """Show a spinner + elapsed/ETA while a blocking call runs (no-op animation off-TTY)."""
    stop = threading.Event()
    t0 = time.monotonic()
    tty = sys.stdout.isatty()
    if not tty:
        print(f"  … {label}", flush=True)

    def _spin():
        i = 0
        while not stop.wait(0.12):
            elapsed = time.monotonic() - t0
            if eta_s and elapsed < eta_s:
                timing = f"{elapsed:.0f}s elapsed  ~{eta_s - elapsed:.0f}s remaining"
            else:
                timing = f"{elapsed:.0f}s elapsed"
            frame = _SPINNER_FRAMES[i % len(_SPINNER_FRAMES)]
            print(f"\r\033[K  {frame}  {label}  ({timing})", end="", flush=True)
            i += 1

    t = None
    if tty:
        t = threading.Thread(target=_spin, daemon=True)
        t.start()
    try:
        yield
    finally:
        stop.set()
        if t:
            t.join()
        elapsed = time.monotonic() - t0
        if tty:
            print(f"\r\033[K  ✓  {label}  ({elapsed:.1f}s)")
        else:
            print(f"  ✓ {label} ({elapsed:.1f}s)", flush=True)


# ── Pydantic response models ──────────────────────────────────────────────────

class CategoryDecision(BaseModel):
    row_index: int
    primary: str       # must be in pfc_taxonomy.PRIMARY
    detailed: str      # must be in pfc_taxonomy.DETAILED[primary]
    changed: bool      # did you change it from the current value?
    confidence: str    # LOW | MEDIUM | HIGH (model's self-rating; anti-calibrated — see config)
    reason: str        # structured: "<signal>=<value> => <PRIMARY>; sign: <debit|credit> consistent"


class CategoryAudit(BaseModel):
    decisions: list[CategoryDecision]


# ── System prompt (schema + taxonomy + output rules) ──────────────────────────

_SYSTEM_PROMPT = """\
You re-categorize bank/credit-card transactions into Plaid's Personal Finance Category
(PFC) taxonomy. Each transaction shown has an existing category that may be wrong (even
high-confidence ones); assign the single best category from the taxonomy below.

{schema}

THE TAXONOMY — you MUST choose one primary and one detailed value listed under it. The
detailed value MUST belong to the chosen primary. Use these exact strings verbatim:
{taxonomy}

OUTPUT RULES:
  - Treat each row INDEPENDENTLY. Use only that row's own signals; never carry a merchant,
    website, or amount from one row into another. Return exactly one decision per row_index.
  - For every row, return its row_index, the chosen primary, a detailed that belongs to
    that primary, changed (true only if your choice differs from the row's current value),
    confidence (your self-rating: LOW/MEDIUM/HIGH), and reason.
  - AMOUNT SIGN (decisive — check it on every row): a POSITIVE amount is money LEAVING the
    account (a debit: purchase, payment, or transfer OUT); a NEGATIVE amount is money
    ARRIVING (a credit: refund, deposit, paycheck, or transfer IN). NEVER assign an
    INCOME_* or TRANSFER_IN_* category to a positive amount — that is money going out, so it
    cannot be income or an inbound transfer. A negative amount is most often a REFUND that
    KEEPS the merchant's normal spend category (a returned purchase stays
    GENERAL_MERCHANDISE, not INCOME); choose INCOME_*/TRANSFER_IN_* only for a genuine
    deposit, paycheck, or inbound transfer, never for a refund.
  - reason: ONE concise line of the form
    "<signal>=<value> => <PRIMARY>; sign: <debit if positive | credit if negative> consistent"
    — e.g. "merchant_name=Acme Brokerage => TRANSFER_OUT; sign: debit consistent". Name the single
    most decisive signal, then state the sign check. No preamble.
  - Identify the merchant's PRIMARY business as a whole phrase — do not be misled by a
    single word in a longer name. When the current category is already correct, repeat it
    with changed=false.
""".format(schema=SCHEMA_PROMPT, taxonomy=pfc_taxonomy.taxonomy_block())

_USER_TEMPLATE = """\
Categorize these {n} transactions. For each, pick the best PFC primary+detailed.

{table}
"""


def _fmt_signal(item: dict, key: str, width: int) -> str:
    return str(item.get(key) or "")[:width]


def _build_prompt(items: list[dict]) -> str:
    """Render a batch of rows as a labelled block, using GLOBAL row_index values so the
    model's returned row_index maps straight back to the caller's list."""
    blocks = []
    for it in items:
        cps = it.get("counterparties") or []
        cp_names = ", ".join(str(c.get("name")) for c in cps if c.get("name"))[:60]
        sug = ""
        if it.get("suggested_primary"):
            sug = f"    mechanical suggestion : {it['suggested_primary']} / {it['suggested_detailed']}\n"
        blocks.append(
            f"[row {it['row_index']}]\n"
            f"    merchant_name  : {_fmt_signal(it, 'merchant_name', 60)}\n"
            f"    name           : {_fmt_signal(it, 'name', 80)}\n"
            f"    original_desc  : {_fmt_signal(it, 'original_description', 80)}\n"
            f"    counterparties : {cp_names}\n"
            f"    website        : {_fmt_signal(it, 'website', 40)}\n"
            f"    payment_channel: {_fmt_signal(it, 'payment_channel', 20)}\n"
            f"    amount         : {it.get('amount')}\n"
            f"    current pf     : {it.get('current_primary')} / {it.get('current_detailed')} "
            f"({it.get('current_confidence')})\n"
            f"{sug}"
        )
    return _USER_TEMPLATE.format(n=len(items), table="\n".join(blocks))


class CategoryLLM:
    """Categorize selected rows with a local Ollama model. Safe when Ollama is down."""

    def __init__(self, model: Optional[str] = None, host: Optional[str] = None,
                 debug: bool = False):
        self.model = model or LLM_MODEL
        self.host = host or LLM_HOST
        # Security baseline: a non-loopback host means transaction text leaves this
        # machine — that must never happen silently.
        if not any(h in self.host for h in ("localhost", "127.0.0.1", "[::1]")):
            print(f"  ⚠ LLM host {self.host!r} is NOT local — transaction data will "
                  "leave this machine on every LLM call.", file=sys.stderr)
        # True once categorize() completed a full pass (every batch executed). The caller
        # uses this to distinguish "the LLM reviewed these rows" from "the stage skipped"
        # (Ollama down / crash) — skipped rows must not be stamped as audited.
        self.ran_ok = False
        global _DEBUG
        _DEBUG = debug

    # --- Ollama lifecycle (copied from reviewer.py) ---------------------------

    def _ping(self) -> bool:
        try:
            urllib.request.urlopen(f"{self.host}/api/tags", timeout=3)
            return True
        except Exception:
            return False

    def _ensure_model(self):
        try:
            resp = json.loads(
                urllib.request.urlopen(f"{self.host}/api/tags", timeout=5).read()
            )
            available = [m["name"] for m in resp.get("models", [])]
            present = (self.model in available or
                       (":" not in self.model and
                        any(m.split(":")[0] == self.model for m in available)))
            if not present:
                print(f"  LLM: pulling {self.model} (first run, may take a few minutes)...")
                subprocess.run(["ollama", "pull", self.model], check=True)
        except Exception as e:  # noqa: BLE001
            print(f"  LLM: could not verify/pull model ({e})")

    def _ensure_ready(self) -> bool:
        """Start Ollama if needed, pull the model if missing. False if unrecoverable."""
        if self._ping():
            self._ensure_model()
            return True
        if not shutil.which("ollama"):
            print("  LLM: ollama not found in PATH — skipping LLM stage.")
            return False
        print("  LLM: starting ollama server...", end=" ", flush=True)
        subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(20):
            time.sleep(1)
            if self._ping():
                print("ready.")
                self._ensure_model()
                return True
        print("timed out — skipping LLM stage.")
        return False

    # --- public API -----------------------------------------------------------

    def categorize(self, items: list[dict]) -> dict[int, CategoryDecision]:
        """Return ``{row_index: CategoryDecision}`` for valid decisions only.

        ``items`` are dicts with the signal fields + ``row_index`` + ``current_*`` +
        optional ``suggested_*``. Decisions whose primary/detailed aren't in the taxonomy
        (or whose detailed doesn't belong to its primary) are dropped. Returns ``{}`` if
        Ollama is unavailable or any unexpected error occurs (caller falls back).
        """
        self.ran_ok = False
        if not items:
            self.ran_ok = True  # nothing to review is a complete (trivial) pass
            return {}
        try:
            return self._categorize_inner(items)
        except Exception as e:  # noqa: BLE001 — LLM stage must never crash the pipeline.
            # Exception, NOT BaseException: a Ctrl+C (KeyboardInterrupt) must ABORT the
            # run — swallowing it would let the audit "complete" with no LLM review while
            # still stamping rows as audited, silently skipping them on future runs.
            print(f"  LLM: unexpected error ({type(e).__name__}: {e}) — skipping LLM stage.")
            if _DEBUG:
                traceback.print_exc()
            return {}

    def _categorize_inner(self, items: list[dict]) -> dict[int, CategoryDecision]:
        if not self._ensure_ready():
            return {}
        try:
            import instructor
            from openai import OpenAI
        except ImportError:
            print("  LLM: 'instructor'/'openai' not installed — skipping LLM stage.")
            return {}

        client = instructor.from_openai(
            OpenAI(base_url=f"{self.host}/v1", api_key="ollama", timeout=240),
            mode=instructor.Mode.JSON,
        )

        valid_index = {it["row_index"] for it in items}
        out: dict[int, CategoryDecision] = {}
        n = len(items)
        n_batches = (n + LLM_BATCH_SIZE - 1) // LLM_BATCH_SIZE
        for b in range(n_batches):
            batch = items[b * LLM_BATCH_SIZE:(b + 1) * LLM_BATCH_SIZE]
            prompt = _build_prompt(batch)
            eta = (len(prompt) / 4 + 400) / 25
            tag = f" (batch {b + 1}/{n_batches})" if n_batches > 1 else ""
            with _spinner(f"Categorizing {len(batch)} row(s){tag}", eta_s=eta):
                res: CategoryAudit = client.chat.completions.create(
                    model=self.model,
                    response_model=CategoryAudit,
                    messages=[
                        {"role": "system", "content": _SYSTEM_PROMPT},
                        {"role": "user", "content": prompt},
                    ],
                    **LLM_SAMPLING,
                    max_retries=2,
                )
            for d in res.decisions:
                if d.row_index not in valid_index:
                    _dbg(f"dropping decision with unknown row_index {d.row_index}")
                    continue
                if not pfc_taxonomy.is_valid(d.primary, d.detailed):
                    # Salvage a valid primary with a bad/hallucinated detailed by snapping
                    # to that primary's OTHER bucket — keeps the (usually correct) primary
                    # rather than discarding the row. Only a fully-unknown primary is dropped.
                    other = pfc_taxonomy.primary_other(d.primary)
                    if other is None:
                        _dbg(f"dropping unknown primary {d.primary} for row {d.row_index}")
                        continue
                    _dbg(f"snapping {d.primary}/{d.detailed} → {other} for row {d.row_index}")
                    d.detailed = other
                out.setdefault(d.row_index, d)
            _dbg(f"batch {b + 1}/{n_batches}: {len(res.decisions)} decision(s)")
        self.ran_ok = True  # every batch executed — a complete pass
        return out
