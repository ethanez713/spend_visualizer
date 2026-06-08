# Plan: `persister` + `plaid_category_transformer` (two new sibling projects)

> **Audience:** separate execution agents with **no prior context**. This doc is
> self-contained. Read the "Shared context" section first, then your project section.
> All three pieces live in sibling dirs under `~/`:
> `transactions/` (exists), `persister/` (NEW), `plaid_category_transformer/` (NEW),
> with `converter/` and `spend_analyzer/` as existing references to copy patterns from.

---

## Context (why we're doing this)

The Plaid fetcher (`~/transactions`) pulls bank/card transactions into a local
raw store and a derived `transactions.csv`. Two gaps:

1. **Durability across Plaid's history window.** Plaid only serves a bounded history
   window per Item; once data ages out we can't re-pull it. We need a durable, deduped,
   **append-only-ish** archive that lives in **git + Google Drive** (dual audit history),
   reconciles local↔remote, and is the system of record even after data leaves Plaid's
   window. This is **`persister`** — a *generic, reusable* persistence/reconcile/sync
   library (no Plaid knowledge). The Plaid-specific re-fetch logic stays in `transactions`.

2. **Category quality.** Plaid assigns a Personal Finance Category (PFC) with a confidence
   (`pf_category_confidence`). LOW/MEDIUM-confidence rows are often wrong. We want a
   **`plaid_category_transformer`** that audits those rows with mechanical rules + a local
   LLM (mirroring `converter`'s proven pipeline), corrects them **within Plaid's own PFC
   taxonomy**, preserves the originals, flags provenance, and persists the result via
   `persister`.

**Outcome:** a durable, replicated, reconciled transaction archive, plus a corrected-category
view of it — both audited through git + Drive revision history.

---

## Decisions locked (from user)

| # | Decision | Choice |
|---|----------|--------|
| 1 | Canonical store format | **Lossless JSONL** (one full Plaid object per line, uncompressed for clean git diffs). Derive a flat CSV for human/Sheets viewing. |
| 2 | Refetch model | **`/transactions/sync` is the everyday path** (most consistent; never misses deltas; inherently avoids over-fetch). Add **windowed `/transactions/get`** as the *reconciliation/repair* path only, driven by the persister's computed date window. |
| 3 | Google Drive object | **A file in Drive updated in place** (native revision history; exact byte round-trip for reconcile). Canonical = the JSONL file; ALSO push a derived CSV file. `drive.file` scope, `file_id` remembered in `var/`. |
| 4 | Transformer taxonomy | **Plaid's PFC taxonomy** (correct `pf_category_primary`/`detailed` in-place; save originals to new nullable columns). |

**Rationale for #2 (record for the executing agent):** `/transactions/sync` is Plaid's
recommended endpoint — cursor-based, returns added/modified/removed deltas, is
resumable/idempotent, and correctly surfaces pending→posted transitions (posted txn carries
`pending_transaction_id`). The local raw store keyed by `transaction_id` already accumulates
everything durably across runs. So sync is both the most consistent AND the
least-over-fetching choice for normal operation. `/transactions/get` (date-windowed) is the
legacy endpoint with weaker initial-pull guarantees; we use it **only** to repair specific
drift the reconciler finds, over a tight window, treating Plaid as golden.

---

## Shared context (read before any project)

### House style (match `transactions/`, `converter/`, `spend_analyzer/` exactly)
- **Layout:** thin entry-point **shim at repo root** (e.g. `persist.py`, `categorize.py`)
  that does `from src.<module> import main; main()`. All real code in **`src/`**. A root
  **`conftest.py`** (or `pytest.ini` with `pythonpath=.`) puts `src/` on the import path.
- **`var/`** at repo root: gitignored, `chmod 700`; holds secrets + runtime state, each
  secret file `chmod 600`. Atomic writes (temp file + `os.replace`); re-apply `0600` on
  every secret write. Pattern to copy: `transactions/src/plaid_client.py` `_ensure_secure_dir`,
  `_save_json`, `load_raw_store`/`save_raw_store`.
- **Deps:** per-project `.venv`; `requirements.txt` pinned with `==`; optional
  `requirements-dev.txt` (pytest). Consider hash-locked `requirements.lock` (converter does).
- **Tests:** fast/offline/deterministic unit tests in `tests/`; slow LLM/network tests in
  `integration_tests/`. Isolate ALL file I/O to tmp dirs via a fixture (copy the `state`
  fixture pattern in `transactions/tests/conftest.py`). **Never touch real `var/`, real
  data files, or the network in unit tests.**
- **Self-documenting:** module-level docstring header on every file explaining its job.

### Global security baseline (`~/.claude/CLAUDE.md` — applies to all)
- **Offline by default.** Core path works with zero network. Drive sync / LLM are opt-in.
  Per user, Drive sync defaults **on** for these tools, but MUST be disable-able by a flag
  (`--no-drive` / `--no-persist`) and should print a one-line "data leaving machine" notice.
- **Least privilege:** Google scope stays `drive.file` (app sees only files it created).
- **Secrets in `var/`** (`token.json`, `client_secret.json`), `0600`; never logged/committed.
  `.gitignore` must include `var/`, `.env*` (keep `!.env.example`), `*secret*.json`,
  `token.json`, `*.tmp`, `__pycache__/`, `*.pyc`, `.pytest_cache/`, `.venv/`.
- **Formula-injection guard** on any derived CSV: prefix a `'` on text cells starting with
  `= + - @`/tab/CR; leave numerics alone. Copy `transactions/src/fetch_transactions.py`
  `_csv_safe` (or converter's).
- **Local LLM only:** Ollama at `http://localhost:11434`; warn if host is non-local.
- **⚠ PRIVACY — committed financial data:** Both new repos commit real transaction data
  (the durable store) to git **on purpose** (audit/replication). Therefore **both repos MUST
  be private**. `var/` (secrets) stays gitignored; `data/` (the store) is deliberately NOT
  gitignored. Create GitHub repos as **private** under account `<your-github-user>` (gh is
  authenticated as <your-github-user> with `repo` scope). Flag this to the user before first push.

### Reference code to reuse (concrete file:line)
- **Google OAuth + folder mgmt:** `converter/src/uploader.py`
  - `_get_credentials(client_secret, token_path)` (lines 68–102): OAuth installed-app flow,
    cached `token.json`, refresh, atomic write. Scope `GOOGLE_SCOPES =
    ["https://www.googleapis.com/auth/drive.file"]` (line 48).
  - `_get_or_create_folder(service, name)` (lines 105–121): find/create a Drive folder.
  - **Gap to fill:** uploader is *write-only* (creates a NEW Google Sheet each run, no
    `file_id` memory, no download). The persister must ADD: download (`files().get_media`),
    update-in-place (`files().update(media_body=…)` → native revisions), and `file_id`
    persistence.
- **Local-LLM audit pattern:** `converter/src/reviewer.py`
  - `LLMAuditor` (lines 362–543): `instructor.from_openai(OpenAI(base_url=f"{host}/v1",
    api_key="ollama"), mode=instructor.Mode.JSON)`; pydantic `response_model`; batched
    (`AUDIT_BATCH_SIZE=35`); `AUDIT_SAMPLING={"temperature":0,"seed":0}`;
    `AUDIT_MODEL="qwen2.5:7b"`, `AUDIT_HOST="http://localhost:11434"` (lines 192–206).
  - `_ensure_ready`/`_ping`/`_ensure_model` (lines 369–420): start Ollama, pull model,
    skip gracefully if unavailable.
  - `_spinner` (lines 50–95): progress UI for blocking LLM calls (TTY-aware).
  - System prompt construction with injected glosses (lines 211–289); batched user-table
    prompt with GLOBAL row indices (lines 422–441).
- **Pending de-dup:** `spend_analyzer/ingest/dedupe.py` `drop_settled_pending(raw_rows)`:
  drop pending rows whose `transaction_id` is referenced by some posted row's
  `pending_transaction_id`; plus keep-last-per-`transaction_id`.
- **Plaid fetch + schema:** `transactions/src/fetch_transactions.py`
  - `CSV_COLUMNS` (lines 36–97, **54 columns**) — the derived-CSV schema.
  - `txn_to_row(txn, account_meta)` — raw Plaid dict → flat row (reuse for derived CSV).
  - `get_account_meta(client, tokens)` — `account_id → {institution, account_name, …}`.
  - `sync_item(client, entry, raw_store)` (cursor sync loop) — the everyday path; unchanged.
  - `_csv_safe`, `_v`, `_g` helpers.
  - `transactions/src/plaid_client.py`: `load_raw_store()/save_raw_store()` (xz JSONL keyed
    by `transaction_id`), `get_client()`, atomic/secure write helpers, `var/` paths.

### Plaid raw record shape (the unit of persistence)
Each record = Plaid transaction `.to_dict()` (what `load_raw_store()` yields), keyed by
`transaction_id`. Categorization-relevant keys: `pending` (bool), `pending_transaction_id`,
`date` (posted, ISO `YYYY-MM-DD`), `authorized_date`, `name`, `merchant_name`,
`merchant_entity_id`, `original_description`, `website`, `counterparties` (list of dicts),
`payment_channel`, `amount` (positive = money out), `location` (dict),
`personal_finance_category` (dict: `primary`, `detailed`, `confidence_level`),
`personal_finance_category_icon_url`, `account_id`.
> Note the raw object nests `personal_finance_category.{primary,detailed,confidence_level}`;
> the 54-col CSV flattens these to `pf_category_primary/detailed/confidence`.

---

# PROJECT A — `persister` (build FIRST; the other two depend on it)

A **generic** library + CLI for durable, deduped, reconciled, Drive-synced JSONL stores.
**No Plaid knowledge** — it operates on lists/dicts of records keyed by a configurable
`key_field` (default `transaction_id`). Both `transactions` and `plaid_category_transformer`
import it.

### Repo layout
```
persister/
├── pyproject.toml            # makes `import persister` installable via `pip install -e .`
├── persist.py                # root shim → src.cli:main (standalone CLI for testing)
├── conftest.py               # pythonpath=. for tests
├── requirements.txt          # pinned: google-api-python-client, google-auth, google-auth-oauthlib
├── requirements-dev.txt      # pytest==9.0.3
├── README.md
├── .gitignore                # var/, .venv/, __pycache__, *.tmp, *secret*.json, token.json, .pytest_cache/  (NOT data/)
├── src/
│   └── persister/            # importable package (pyproject maps package dir here)
│       ├── __init__.py       # re-export public API (Store, DriveSync, reconcile, compute_window, merge_golden, dedupe_supersede)
│       ├── store.py          # JSONL load/save (atomic), derive_csv, dedupe_supersede
│       ├── reconcile.py      # diff local vs remote → ReconcileReport
│       ├── windows.py        # compute_window(store, extra_tids) → Window(start_date,end_date,pending_ids)
│       ├── merge.py          # merge_golden(base, fresh) — fresh overwrites by key; never drops non-dups
│       ├── drive_sync.py     # DriveSync: pull()/push() a Drive file in place (extends converter uploader)
│       ├── audit.py          # append-only reconcile_log.jsonl writer
│       └── csv_safe.py       # formula-injection guard (copy from transactions)
├── data/                     # ⚠ COMMITTED: the durable stores live here (NOT gitignored)
│   └── .gitkeep
├── tests/                    # unit (offline; Drive + fs stubbed/tmp)
└── integration_tests/        # optional: real Drive round-trip (manual, gitignored creds)
```

### `pyproject.toml` (minimal, setuptools)
```toml
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "persister"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = []   # Google libs are optional/lazy; pin them in requirements.txt

[tool.setuptools.packages.find]
where = ["src"]
```
> Result: `pip install -e ../persister` exposes `import persister`. Google packages are
> imported lazily inside `drive_sync.py` (so non-Drive use needs nothing extra), but list
> them in `requirements.txt` for the Drive path.

### Module specs

**`store.py`**
```python
def load_jsonl(path: str, key_field: str = "transaction_id") -> dict[str, dict]:
    """Read newline-delimited JSON into {key: record}. Missing file → {}. Fail-soft on a
    bad line (log + skip), never crash."""

def save_jsonl(path: str, store: dict[str, dict], key_field="transaction_id") -> None:
    """Atomic write (tmp + os.replace). Sort records by (date, key_field) for stable git
    diffs. One compact JSON object per line, ensure_ascii=False, dates already ISO strings.
    Create parent dir if needed."""

def derive_csv(store: dict[str, dict], csv_path: str, columns: list[str],
               row_fn, csv_safe: bool = True) -> None:
    """Project each record via row_fn(record)->dict, write CSV with given column order.
    Apply csv_safe() to every cell. (transactions supplies row_fn = txn_to_row-equivalent;
    transformer supplies its own with the extra columns.)"""

def dedupe_supersede(store: dict[str, dict]) -> dict[str, dict]:
    """Drop pending rows superseded by a posted row (posted.pending_transaction_id == pending.key).
    Port spend_analyzer/ingest/dedupe.py drop_settled_pending. Keep-last semantics already
    implied by dict keying. Only removes CLEAR duplicates — preserve everything else."""
```

**`reconcile.py`**
```python
@dataclass
class ReconcileReport:
    in_sync: list[str]        # keys identical in both
    local_only: list[str]     # keys only local  → keep (new data to push)
    remote_only: list[str]    # keys only remote → keep (history that aged out of Plaid; NEVER delete)
    conflicts: list[str]      # keys in both but content differs → Plaid golden → mark for re-fetch
    merged: dict[str, dict]   # union; for conflicts, remote value retained pending re-fetch

def reconcile(local: dict[str, dict], remote: dict[str, dict],
              key_field="transaction_id") -> ReconcileReport:
    """Classify by key membership + a stable content hash (canonical json.dumps(sort_keys=True)).
    Policy (preserve as much as possible):
      - in both & equal       → in_sync
      - in both & differ      → conflict (caller re-fetches from Plaid; golden wins)
      - local only            → keep in merged
      - remote only           → keep in merged (durable history beyond Plaid window)
    merged = union of all keys. Conflicts left as remote value until a golden re-fetch
    overwrites them via merge_golden()."""
```

**`windows.py`**
```python
@dataclass
class Window:
    start_date: str    # ISO YYYY-MM-DD
    end_date: str      # ISO (today)
    pending_ids: list[str]

def compute_window(store: dict[str, dict], extra_tids: Iterable[str] = ()) -> Window:
    """Date window for a targeted Plaid /transactions/get repair fetch.
      latest_settled = max(date for record where not record['pending'])
      start = latest_settled - 7 days                    # 1 week back past latest settled
      start = min(start, every pending record's date)    # always cover pending
      start = min(start, date of every extra_tid)        # cover conflicts / remote-only gaps
      end   = today
      pending_ids = [key for record where record['pending']]
    Bounded window → avoids over-fetch. If store empty, default to a sane backfill
    (e.g. end-730d .. today) and document it."""
```

**`merge.py`**
```python
def merge_golden(base: dict[str, dict], fresh: list[dict],
                 key_field="transaction_id") -> dict[str, dict]:
    """Plaid is golden: each fresh record OVERWRITES base[key]. Records in base but absent
    from fresh are KEPT (they may simply be outside the fetched window — never delete).
    Then dedupe_supersede() to drop settled pendings. Returns the new store."""
```

**`drive_sync.py`** (extends converter's uploader — the key new capability)
```python
class DriveSync:
    """Sync ONE logical file to a Drive file, in place, with native revision history.
    Lazy-imports google libs. Remembers file_id in var/drive_state.json (per logical name)."""
    def __init__(self, file_name: str, folder_name: str = "transactions_archive",
                 var_dir: str = "<repo>/var"): ...
    def pull(self) -> bytes | None:
        """Download current Drive file content (files().get_media). None if no remembered
        file_id / not found. Used to get the remote store for reconcile."""
    def push(self, local_path: str, mime: str = "application/x-ndjson") -> str | None:
        """If file_id known → files().update(media_body=…) (new revision, same file).
        Else → files().create(...) in the folder, store the new file_id. Returns webViewLink.
        Never raises (log + return None) — a failed push must not lose local data."""
```
- Reuse `_get_credentials`, `_get_or_create_folder` verbatim from `converter/src/uploader.py`
  (copy into this module; keep `GOOGLE_SCOPES=["…/auth/drive.file"]`).
- `var/drive_state.json` shape: `{ "<file_name>": "<file_id>" }`, `0600`.
- MIME: store the JSONL as a plain Drive file (`application/x-ndjson` or `text/plain`) — do
  **NOT** convert to a Google Sheet (conversion is lossy and breaks exact reconcile). The
  derived CSV is pushed as a SECOND Drive file (`text/csv`) for human viewing.

**`audit.py`**: `log_reconcile(path, report, *, source)` → append one JSONL line per run with
counts + conflict keys + timestamp (`var/reconcile_log.jsonl`, `0600`). Cheap audit trail to
complement git + Drive revisions.

### CLI (`src/cli.py`, shim `persist.py`)
Standalone, mostly for testing/manual ops; the real callers use the library API:
```
persist reconcile --store data/transactions.jsonl [--drive-file transactions.jsonl] [--no-drive]
persist push      --store data/transactions.jsonl [--no-drive]
persist window    --store data/transactions.jsonl     # prints computed (start,end,pending)
```

### Tests (`tests/`, all offline)
- `store.py`: JSONL round-trip; atomic write; sort order; `dedupe_supersede` drops a settled
  pending and keeps everything else; `derive_csv` applies `_csv_safe` and column order.
- `reconcile.py`: in_sync / local_only / remote_only / conflict classification on crafted
  dicts; `merged` is the full union (nothing dropped).
- `windows.py`: latest-settled − 7d; window always covers pending dates and `extra_tids`;
  empty-store default.
- `merge.py`: fresh overwrites by key; base-only keys preserved; settled pendings dropped.
- `drive_sync.py`: **stub the Drive service** (a fake object whose `files().get_media/create/
  update` record calls); assert `file_id` persisted to `var/drive_state.json`, update-path vs
  create-path chosen correctly, `pull` returns bytes, errors → `None` (never raise).
- Copy the tmp-dir isolation fixture pattern from `transactions/tests/conftest.py`.

---

# PROJECT B — changes to existing `transactions/` (build SECOND)

Adds the **windowed repair fetch** and the **persist orchestration**, wiring in `persister`.
Keeps the existing cursor-sync everyday path untouched.

### New: `src/fetch_window.py` — date-windowed `/transactions/get`
```python
def fetch_window(client, tokens, start_date: date, end_date: date) -> list[dict]:
    """Repair fetch over a bounded window using /transactions/get (NOT sync).
    For each token/item:
      req = TransactionsGetRequest(
          access_token=t['access_token'], start_date=start_date, end_date=end_date,
          options=TransactionsGetRequestOptions(include_personal_finance_category=True,
                                                count=500, offset=offset))
      paginate by offset until offset+len(transactions) >= resp['total_transactions'].
    Collect resp['transactions'], call .to_dict(), join account meta via existing
    get_account_meta(client, tokens). Return raw dicts in the SAME shape as load_raw_store()
    values (keyed downstream by transaction_id).
    On ApiException: log the error code and SKIP that item (do NOT let a Plaid error delete
    local data — Plaid is golden ONLY when it returns data, not errors)."""
```
- Imports: `from plaid.model.transactions_get_request import TransactionsGetRequest`,
  `...transactions_get_request_options import TransactionsGetRequestOptions`.
- Reuse `get_account_meta`, `_v`/`_g` from `fetch_transactions.py`.

### New: `src/persist_runner.py` — orchestration (the business logic that drives persister)
```python
def run_persist(*, do_drive=True, allow_refetch=True) -> None:
    """Called after a normal sync. Steps:
      1. local L  = load_raw_store()                      # existing xz JSONL store, as dict
      2. drive    = DriveSync("transactions.jsonl", folder_name="transactions_archive")
         remote R = persister.load_jsonl_bytes(drive.pull()) if do_drive else {}
      3. report   = persister.reconcile(L, R)
      4. merged   = report.merged
         if allow_refetch and (report.conflicts or report.remote_only_gaps_needing_check):
             win   = persister.compute_window(merged, extra_tids=report.conflicts)
             fresh = fetch_window(get_client(), load_tokens(), win.start_date, win.end_date)
             merged = persister.merge_golden(merged, fresh)
      5. merged   = persister.dedupe_supersede(merged)
      6. persister.save_jsonl("<persister>/data/transactions.jsonl" OR a configured path, merged)
         persister.derive_csv(merged, "<.>/data/transactions.csv", CSV_COLUMNS,
                              row_fn=lambda r: txn_to_row(r, account_meta))
      7. if do_drive:
             drive.push(jsonl_path)            # new Drive revision of canonical store
             DriveSync("transactions.csv", …).push(csv_path, mime="text/csv")
         persister.log_reconcile(report)
      8. (optional) git add + commit the data files in the data repo (see note below)."""
```
- **Where does `data/transactions.jsonl` live / get committed?** Recommended: in the
  **`persister` repo's `data/`** dir (the persister owns the durable store; one private repo
  holds the data + the tooling). `transactions` passes an absolute path
  (`../persister/data/transactions.jsonl`) via config. Alternative if you prefer separation:
  a dedicated private `transactions-archive` repo. **Confirm with user before committing data.**
  Auto-`git commit` is optional and should be behind a flag; default to leaving the working
  tree dirty for the user to review/commit (matches house norm: commit only when asked).

### Wiring `persister` into `transactions`
- `transactions/requirements-dev.txt` (or a new `requirements-persist.txt`): add
  `-e ../persister` and the Google libs. Install into `transactions/venv`:
  `./venv/bin/pip install -e ../persister`.
- Add a flag to the fetch entry point: `fetch_transactions.py --persist` (default ON) /
  `--no-persist`, `--no-drive`, `--no-refetch`. After `main()` runs the sync, call
  `run_persist(...)`. Keep persist OFF in unit tests.

### Tests (offline)
- `fetch_window`: feed a **FakeClient** (like `transactions/tests/test_sync.py`'s FakeClient)
  whose `transactions_get` returns canned paginated pages; assert pagination stops at
  `total_transactions`, records are `.to_dict()`-shaped, account meta joined, ApiException →
  item skipped (no crash).
- `run_persist`: stub `DriveSync` + `fetch_window`; tmp `data/` dir; assert reconcile→merge→
  save→push sequence and that conflicts trigger a windowed refetch. No network.

---

# PROJECT C — `plaid_category_transformer` (build THIRD; depends on persister)

Re-categorizes LOW/MEDIUM-confidence Plaid rows using mechanical rules + a local LLM, within
**Plaid's PFC taxonomy**, preserving originals and flagging provenance, then persists via
`persister`. Mirrors `converter`'s architecture.

### Repo layout
```
plaid_category_transformer/
├── categorize.py             # root shim → src.transformer:main
├── conftest.py
├── requirements.txt          # pinned: pandas?, instructor, openai, pydantic, -e ../persister, google libs
├── requirements-dev.txt      # pytest
├── README.md
├── .gitignore                # var/, .venv, __pycache__, *.tmp, *secret*.json, token.json, .pytest_cache/  (NOT data/)
├── src/
│   ├── transformer.py        # engine + CLI main: select rows → mechanical → LLM → write provenance → persist
│   ├── rules.py              # mechanical rules + merchant memory (keyed by merchant_entity_id, fallback normalized name)
│   ├── pfc_taxonomy.py       # VENDORED Plaid PFC taxonomy: PRIMARY list, DETAILED-by-primary map, glosses
│   ├── llm.py                # local-LLM categorizer (Ollama+instructor+pydantic) — adapt converter/reviewer.py
│   ├── schema.py             # row-schema prompt text + extra-column names + selection predicate
│   └── csv_safe.py           # copy
├── data/                     # ⚠ COMMITTED durable store of the categorized table
├── tests/                    # offline: selection, rules, provenance columns, taxonomy validation
└── integration_tests/        # requires Ollama: LLM accuracy on a tiered golden set
```

### Input / selection
- **Input:** the persister's canonical store (default `../persister/data/transactions.jsonl`);
  allow `--input <path>` (JSONL or the xz raw store) for standalone runs.
- **Select rows to process:** `personal_finance_category.confidence_level` ∈
  `{"LOW", "MEDIUM"}` (also treat `UNKNOWN`/missing as process; configurable
  `--confidence LOW,MEDIUM,UNKNOWN`). **HIGH / VERY_HIGH pass through untouched.**

### Stage 1 — Mechanical rules (`rules.py`) — use ALL signals
Deterministic, runs first. Signals to exploit (richest first):
- `merchant_entity_id` (stable Plaid merchant id) → a memory map `entity_id → (primary,detailed)`.
- `counterparties[*].name/type` (e.g. `type=="merchant"` gives a clean merchant).
- `website` domain → category hints.
- `name` / `original_description` keyword/token rules (copy converter's `contains_word`
  whole-token matcher; e.g. `TST*` prefix → `FOOD_AND_DRINK` / `FOOD_AND_DRINK_RESTAURANT`).
- `payment_channel` (`in store` / `online` / `other`), `amount` sign/magnitude.
- A `merchant_memory.json` in `var/` (keyed by `merchant_entity_id`, fallback normalized
  `merchant_name`; reuse converter `normalize_merchant`). `0600`, atomic write.
- Each rule returns a candidate `(primary, detailed, rule_name)` or `None`. First hit wins.
- Mechanical output is a **suggestion** carried into Stage 2 (the LLM sees it) AND recorded
  as provenance if it ends up being the final value.

### Stage 2 — Local LLM (`llm.py`) — runs on ALL selected rows regardless of Stage 1
Adapt `converter/src/reviewer.py` `LLMAuditor`:
- Same runtime: `instructor.from_openai(OpenAI(base_url="http://localhost:11434/v1",
  api_key="ollama"), mode=instructor.Mode.JSON)`, model `qwen2.5:7b`, `temperature=0, seed=0`,
  batched (~35 rows), `_ensure_ready`/`_ping`/`_ensure_model`, `_spinner`. Skip gracefully if
  Ollama down (then final = mechanical result, or unchanged).
- **Pydantic response model:**
  ```python
  class CategoryDecision(BaseModel):
      row_index: int
      primary: str        # must be in PFC PRIMARY
      detailed: str       # must be in DETAILED[primary]
      changed: bool       # did you change it from the current value?
      confidence: str     # LOW|MEDIUM|HIGH (model's self-rating)
      reason: str         # ONE concise sentence citing the signal used
  class CategoryAudit(BaseModel):
      decisions: list[CategoryDecision]
  ```
- **System prompt** MUST contain (per user):
  1. **A short schema description** — what a row is and what each signal column means, e.g.:
     "Each row is one bank/credit-card transaction. Use ALL of these signals:
     merchant_name (cleaned merchant), name/original_description (raw bank text),
     counterparties (parties involved), website, payment_channel (in store/online),
     location, amount (POSITIVE = money out / a purchase; negative = money in/refund),
     and the current pf_category_detailed. Prefer merchant_entity_id-stable identity."
  2. **The Plaid PFC taxonomy** to choose from — inject PRIMARY + DETAILED + one-line glosses
     from `pfc_taxonomy.py` (analogous to how reviewer.py injects CATEGORY_GLOSS).
  3. Output rules: choose the best `primary` + a `detailed` that belongs to that primary; set
     `changed=true` only if different from current; `reason` = one sentence.
- **User prompt:** a per-row block/table (GLOBAL indices, like reviewer's pass-1) listing the
  signal fields above + current `pf primary/detailed/confidence` + the Stage-1 mechanical
  suggestion (if any). Batch of ~35.
- **Validation:** reject decisions whose `primary`/`detailed` aren't in the taxonomy or where
  `detailed` ∉ `DETAILED[primary]` (drop or fall back to mechanical/original). The LLM's
  decision is the **final authority** for selected rows (it sees the most context).

### `pfc_taxonomy.py` (VENDOR, do not fetch at runtime — offline-first)
- Source: Plaid's published taxonomy CSV
  (`https://plaid.com/documents/transactions-personal-finance-category-taxonomy.csv`).
  The executing agent should obtain it ONCE and vendor it as a local module/CSV in `src/`
  (commit it). Provide `PRIMARY: list[str]`, `DETAILED: dict[str, list[str]]`,
  `GLOSS: dict[str, str]` (detailed → one-line description).
- Known PRIMARY set (16): `INCOME, TRANSFER_IN, TRANSFER_OUT, LOAN_PAYMENTS, BANK_FEES,
  ENTERTAINMENT, FOOD_AND_DRINK, GENERAL_MERCHANDISE, HOME_IMPROVEMENT, MEDICAL,
  PERSONAL_CARE, GENERAL_SERVICES, GOVERNMENT_AND_NON_PROFIT, TRANSPORTATION, TRAVEL,
  RENT_AND_UTILITIES`. DETAILED names are `PRIMARY_*` (e.g. `FOOD_AND_DRINK_RESTAURANT`) —
  take the exact strings from the vendored CSV.

### Output schema + provenance (`schema.py`)
Output = the **full raw Plaid record** (every original field, so it stays schema-compatible
and persistable by persister) **plus these new nullable fields** (empty/null when no change):
- `original_pf_category_primary`
- `original_pf_category_detailed`
- `original_pf_category_confidence`
- `category_update_step`   — `"mechanical"` | `"llm"` | `""` (none)
- `category_update_reason` — short string (rule name or LLM reason)
- `category_update_confidence` — the corrector's confidence (mechanical: `"HIGH"` for
  exact-entity-id memory hits; LLM: its self-rated value)

**On a change** (final category differs from original):
1. Copy original `personal_finance_category.{primary,detailed,confidence_level}` →
   the three `original_*` columns.
2. Overwrite `pf_category_primary`/`pf_category_detailed` (and the nested
   `personal_finance_category.primary/detailed`) with the corrected values.
3. Set `pf_category_confidence` (and nested `confidence_level`) to the sentinel
   `"CORRECTED"` so downstream knows it's no longer Plaid's confidence. (Original confidence
   is preserved in `original_pf_category_confidence`.)
4. Set `category_update_step` to the stage that produced the final value, with this
   precedence: if the LLM's final differs from the original → `"llm"`; else if mechanical
   changed it and the LLM concurred (LLM `changed=false` but value already corrected by
   mechanical) → `"mechanical"`. If nothing changed → all new columns empty.

> Net effect: "wrong" category overwritten in place; originals retained in the new columns;
> a clear flag for which stage made the change. Matches the user's spec exactly.

### Persistence (reuse Project A)
- Persist the transformed table via `persister`:
  - `persister.save_jsonl("data/transactions_categorized.jsonl", out_store)` (committed to git).
  - `persister.derive_csv(out_store, "data/transactions_categorized.csv", COLUMNS,
    row_fn=…)` where COLUMNS = the 54 base columns + the 6 new columns; `_csv_safe` applied.
  - `DriveSync("transactions_categorized.jsonl", folder_name="transactions_archive").push(...)`
    and a CSV push for the human/Sheets view. Default ON, `--no-drive` to disable.
- Provenance/audit: append every change to `var/category_log.jsonl` (timestamp, txn id,
  original→new primary/detailed, step, reason) — mirrors converter's `correction_log.jsonl`.

### Tests
- **Unit (offline, no LLM):**
  - selection predicate (LOW/MEDIUM selected; HIGH/VERY_HIGH passthrough untouched);
  - mechanical rules (entity-id memory hit; TST*→restaurant; website hint) produce expected
    `(primary, detailed)`;
  - provenance columns set correctly on change vs no-change (the decision table above);
  - taxonomy validation rejects invalid primary/detailed;
  - output is schema-compatible (all original keys present + the 6 new ones).
  - Stub the LLM (inject a fake `CategoryAudit`), tmp dirs, no network.
- **Integration (`integration_tests/`, requires Ollama + qwen2.5:7b):**
  - a small **tiered golden set** (easy / medium / hard / ambiguous) of real LOW/MEDIUM rows
    with hand-labeled correct PFC; assert recall/precision **thresholds** (not exact match),
    inherit model/sampling/batch from the single source of truth in `llm.py`. Do NOT overfit
    the prompt to the golden set.

---

## Build & run order (dependencies)

1. **`persister`** first (library both others need). `git init`; create **private** GitHub
   repo `<your-github-user>/persister`. Build, test, `pip install -e .` into its own `.venv`.
2. **`transactions`** changes: `./venv/bin/pip install -e ../persister`; add `fetch_window.py`
   + `persist_runner.py` + flags; test offline; then a guarded live run.
3. **`plaid_category_transformer`**: `git init`; private repo `<your-github-user>/plaid_category_transformer`;
   `pip install -e ../persister`; build mechanical + LLM + provenance + persistence; unit
   tests, then Ollama integration tests.

> Each project keeps its own `.venv`. `persister` is installed **editable** in the other
> two so edits propagate. If a repo dir is moved/renamed, rebuild venvs (paths are absolute).

---

## Verification (end-to-end)

**persister (offline):** `cd persister && ./.venv/bin/python -m pytest` — all unit tests green;
`persist window --store tests/fixtures/sample.jsonl` prints a sane window; `persist reconcile`
with two crafted local/remote fixtures reports correct in_sync/conflict/remote_only counts.
Drive path verified via the stubbed-service tests (no real network in CI).

**transactions (offline → live):**
1. Offline: `./venv/bin/python -m pytest` (existing 39 + new `fetch_window`/`run_persist`).
2. Live (guarded, ask user — outward Plaid + Drive egress): run the fetch with `--persist`;
   confirm `../persister/data/transactions.jsonl` (+ `.csv`) written, deduped, row count sane;
   confirm a new Drive revision appears (Drive UI → file → version history); re-run and
   confirm reconcile reports `in_sync` (no spurious conflicts); introduce a hand-edit in the
   local store and confirm reconcile flags a conflict and the windowed `/transactions/get`
   repair overwrites it from Plaid.

**plaid_category_transformer:**
1. Offline unit tests green (selection, rules, provenance, taxonomy).
2. Ollama up (`ollama serve`; `qwen2.5:7b` pulled): `./.venv/bin/python -m pytest
   integration_tests -s` meets accuracy thresholds on the golden set.
3. Real run on `../persister/data/transactions.jsonl`: confirm only LOW/MEDIUM rows changed,
   HIGH untouched; spot-check `original_*` columns populated on changes, `category_update_step`
   correct; `data/transactions_categorized.{jsonl,csv}` written + Drive revision created.

---

## Open risks / notes for the executing agents

- **Private repos are mandatory** (committed financial data). Confirm with the user before
  the first `git push`; create repos `--private` under `<your-github-user>`.
- **Drive `drive.file` scope** means the app only sees files it created — the persister's
  create-then-remember-`file_id` flow is required (you can't "find" a pre-existing arbitrary
  file). First run creates the folder + files; `var/drive_state.json` must persist the IDs.
- **`/transactions/get` requires the data to be within the Item's `days_requested` window**
  (set to 730 at link time in `transactions/src/app.py`). The repair window is small, so this
  is fine; but a conflict on a very old txn outside the window can't be re-fetched — in that
  case keep the existing value and log the conflict (don't delete).
- **Plaid is golden ONLY on success.** On `ApiException`/error codes, do not overwrite or
  delete local data — keep local and log.
- **Determinism:** LLM at `temperature=0, seed=0`; still allow minor hardware-driven drift —
  assert thresholds, never exact equality, in integration tests.
- **Don't over-fetch:** the everyday path is cursor `/transactions/sync`; windowed get fires
  only when reconcile finds drift/conflict.
- **`pfc_taxonomy.py` must be vendored** (committed), not fetched at runtime (offline-first).
```
