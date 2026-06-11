# Plaid → single transactions.csv

Pulls all your bank / credit-card transactions from Plaid into one combined,
de-duplicated `transactions.csv`. Re-running fetches only new or changed
transactions (cursor-based incremental sync).

Runs entirely on your machine. On the Plaid **Trial** plan this is free, real
production data, up to **10 connected banks (Items)**.

## Layout

```
.
├── app.py                      # entry-point shim → src/app.py
├── fetch_transactions.py       # entry-point shim → src/fetch_transactions.py
├── run_fetch.sh                # wrapper for scheduled runs (logs to data/logs/fetch.log)
├── transactions.csv            # ← the deliverable (git-ignored, regenerated each run)
├── requirements.txt            # pinned runtime deps  (requirements-dev.txt = test deps)
├── src/
│   ├── app.py                  # Flask backend for the Plaid Link flow
│   ├── fetch_transactions.py   # core: /transactions/sync → transactions.csv
│   ├── plaid_client.py         # shared client + local-state helpers
│   └── link.html               # browser page to log into a bank
├── tests/                      # pytest suite (offline, deterministic)
├── requirements-persist.txt    # durable-persist extras: persister (editable) + Drive libs
├── .secrets/                   # git-ignored, 0700: secrets only
│   ├── .env                    # your Plaid credentials (0600)
│   └── tokens.json             # access_token + item_id + institution per bank (0600)
└── data/                       # git-ignored, 0700: runtime state + raw archive
    ├── sync_cursors.json       # next_cursor per item
    ├── transactions_raw.jsonl.xz  # lossless raw Plaid objects; source of truth + audit/QC
    ├── transactions.jsonl      # durable system-of-record store (Drive-synced revisions)
    ├── transactions.csv        # derived projection of the durable store (Drive-synced)
    ├── reconcile_log.jsonl     # 0600 audit trail of each persist reconcile
    └── logs/fetch.log          # scheduled-run output
```

(`.secrets/` also holds the Drive credentials + `drive_state.json` file-id memory once
the persist step has run — see **Durable persist**.)

`src/` also holds the durable-persist orchestration: `persist_runner.py` (reconcile →
golden repair → durable store + Drive push) and `fetch_window.py` (the bounded
`/transactions/get` repair fetch). See **Durable persist** below.

Run the tool from the project root via the two shims (`app.py`, `fetch_transactions.py`);
the importable code lives in `src/`.

## Setup (one time)

```bash
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

`.secrets/.env` is already created with your Production credentials and `PLAID_ENV=production`.
(See `.env.example` for the format. The `.secrets/` directory holds all secrets and `data/`
holds runtime state; both are git-ignored and locked to `0700`.)

## Step 1 — Link your banks

```bash
./venv/bin/python app.py
```

Open <http://127.0.0.1:5000/> in a browser. Click **Connect a bank**, log in,
and approve. On success the page shows "Linked: <bank>" and adds it to
`.secrets/tokens.json`. Repeat once per bank.

> ⚠️ **10 Items max**, and removing an Item does **not** free the slot. Only link
> banks you actually want. Confirm before each.

Stop the server with `Ctrl+C` when done linking.

## Step 2 — Fetch transactions

```bash
./venv/bin/python fetch_transactions.py
```

Prints a per-bank summary of added / modified / removed and writes
`transactions.csv`.

### CSV columns (54)

The file is intentionally wide — it captures essentially every populated field
Plaid returns per transaction, plus per-account metadata, so a downstream
analytics app has the maximum raw data to work with.

- **Account identity** (from `/accounts/get`): `institution, account_id,
  account_name, account_mask, account_official_name, account_type,
  account_subtype`
- **Transaction core:** `transaction_id, pending, pending_transaction_id, date,
  authorized_date, datetime, authorized_datetime, name, original_description,
  merchant_name, merchant_entity_id, website, logo_url, amount,
  iso_currency_code, unofficial_currency_code, payment_channel,
  transaction_type, transaction_code, check_number, account_owner`
- **Categorization** (`personal_finance_category`): `pf_category_primary,
  pf_category_detailed, pf_category_confidence, pf_category_version,
  pf_category_icon_url`
- **Location:** `location_address, location_city, location_region,
  location_postal_code, location_country, location_lat, location_lon,
  location_store_number`
- **Payment metadata** (checks / ACH / transfers): `payment_reference_number,
  payment_ppd_id, payment_payee, payment_by_order_of, payment_payer,
  payment_method, payment_processor, payment_reason`
- **Counterparties:** `counterparty_name, counterparty_type,
  counterparty_entity_id, counterparty_confidence` (the primary counterparty,
  flattened) plus `counterparties_json` (the full variable-length list as a JSON
  string — lossless)

Notes:
- `date` is the posted date; `authorized_date` is when the purchase was actually
  made (use it for accurate spend timing where present).
- `amount` follows Plaid's sign convention: **positive = money out** of the
  account, negative = money in.
- `merchant_entity_id` is a stable merchant id — group by it for reliable
  "spending by merchant" without string-matching names.
- Deprecated/empty Plaid fields are intentionally omitted: `category` /
  `category_id` (legacy taxonomy, superseded by `personal_finance_category`),
  `business_finance_category`, `client_customization`.

First run right after linking: Plaid may still be pulling history — the script
retries automatically. If it still shows no data, wait a few minutes and re-run.

Safe to re-run anytime — only new/changed transactions are pulled, and the CSV
is always de-duplicated by `transaction_id`.

### Durable persist (the default `--persist` step)

After each sync the fetch also maintains the **durable system-of-record store** in
this repo's `data/transactions.jsonl` (gitignored; its audit history is Drive's
append-only revision trail) and pushes new Google Drive revisions of it + a derived
CSV. `persister` is a pure library — this repo owns its own data, Drive credentials,
and Drive file-id state:

1. **reconcile** the local raw store against the Drive remote (via the `persister`
   library) — classifies in-sync / local-only / remote-only / conflicts;
2. **golden repair** — conflicting ids are re-fetched over a bounded window with
   `/transactions/get`; Plaid's fresh answer overwrites (Plaid is golden **only on
   success**). A conflict the re-fetch cannot confirm (aged out of Plaid's window,
   or an item error) **stops the run non-zero before anything is written or
   pushed** — callers like `finance_pipeline` halt instead of persisting divergence;
3. **dedupe** settled pendings, write the durable JSONL + CSV, push Drive revisions
   (the file is updated in place; old revisions survive — persister is append-only).

Flags: `--no-persist` (sync + local CSV only), `--no-drive` (persist locally, no
egress), `--no-refetch` (skip the repair fetch; conflicts then stop the run).
One-time setup: `./venv/bin/pip install -r requirements-persist.txt`. Drive
credentials + file-id state live in this repo's `.secrets/` (`client_secret.json`,
`token.json`, `drive_state.json` — see persister's README for the OAuth setup).

The whole chain (fetch → categorize → analyze UI) is normally driven by the
**`../finance_pipeline`** orchestrator (`./run.py`).

### Raw archive (audit / QC)

`data/transactions_raw.jsonl.xz` is the **source of truth**: the complete, untouched
Plaid object for every transaction (keyed by `transaction_id`), stored as
xz-compressed JSONL at max compression (~25× smaller than raw). `transactions.csv`
is a derived projection of it, so the two can never drift. The archive is
lossless — it preserves every field Plaid returns, including ones the CSV omits.

Inspect it without decompressing to disk:

```bash
xzcat data/transactions_raw.jsonl.xz | jq '.'                 # all records, pretty
xzcat data/transactions_raw.jsonl.xz | jq 'select(.amount>100)'  # filter
xzcat data/transactions_raw.jsonl.xz | wc -l                  # count
```

## Step 3 — Schedule (daily)

A wrapper `run_fetch.sh` runs the fetch and appends output to `data/logs/fetch.log`.

Add a cron job (runs daily at 7am):

```bash
crontab -e
```

Add this line:

```
0 7 * * * ~/spend_vizualiser/transactions/run_fetch.sh
```

Verify it works first by running the wrapper by hand:

```bash
./run_fetch.sh && tail -n 20 data/logs/fetch.log
```

> **WSL note:** cron is not started automatically in WSL. Start it once per boot
> with `sudo service cron start`, or add that to your shell profile. Alternatively
> run the job from the Windows Task Scheduler.

## Testing in sandbox first (optional)

To prove the whole pipeline with fake data before linking real banks:

1. In `.secrets/.env` set `PLAID_ENV=sandbox` and `PLAID_SECRET=<your Sandbox secret>`.
2. Run `app.py`, link any bank, and use Plaid's test creds `user_good` / `pass_good`.
3. Run `fetch_transactions.py` and inspect `transactions.csv`.
4. Switch `.secrets/.env` back to `production` + Production secret, then delete the sandbox
   state files (`.secrets/tokens.json`, `data/sync_cursors.json`,
   `data/transactions_raw.jsonl.xz`, `transactions.csv`) so real data starts clean, and
   link your real banks.

## Notes / constraints

- Trial = the `production` environment string (free, real data). Don't apply for
  full Production access — it's a one-way door off the free Trial.
- Auth is `client_id` + `secret` in the request body (no client certificate).
- Access tokens are persisted the instant they're issued; keep `.secrets/tokens.json` safe.

## Tests

```bash
./venv/bin/pip install -r requirements-dev.txt
./venv/bin/python -m pytest
```

Fast, offline, and deterministic — they isolate all file I/O to a tmp dir and never touch
`.secrets/`, `data/`, the real CSV, or the network.
