# Plaid → single transactions.csv

Pulls all your bank / credit-card transactions from Plaid into one combined,
de-duplicated `transactions.csv`. Re-running fetches only new or changed
transactions (cursor-based incremental sync).

Runs entirely on your machine. On the Plaid **Trial** plan this is free, real
production data, up to **10 connected banks (Items)**.

## Files

| File | Purpose |
|------|---------|
| `app.py` | Flask backend for the Plaid Link flow (link token + token exchange) |
| `link.html` | Browser page to log into a bank and capture its access token |
| `fetch_transactions.py` | Core: `/transactions/sync` → `transactions.csv` |
| `plaid_client.py` | Shared client + local state helpers |
| `run_fetch.sh` | Wrapper for scheduled runs (logs to `logs/fetch.log`) |
| `.env` | Your credentials (git-ignored) |
| `tokens.json` | Access token + item_id + institution per bank (git-ignored) |
| `sync_cursors.json` | `next_cursor` per item (git-ignored) |
| `transactions_raw.jsonl.xz` | Lossless raw Plaid object per transaction, xz-compressed; source of truth + audit/QC (git-ignored) |

## Setup (one time)

```bash
python3 -m venv venv
./venv/bin/pip install plaid-python python-dotenv flask
```

`.env` is already created with your Production credentials and `PLAID_ENV=production`.
(See `.env.example` for the format.)

## Step 1 — Link your banks

```bash
./venv/bin/python app.py
```

Open <http://127.0.0.1:5000/> in a browser. Click **Connect a bank**, log in,
and approve. On success the page shows "Linked: <bank>" and adds it to
`tokens.json`. Repeat once per bank.

> ⚠️ **10 Items max**, and removing an Item does **not** free the slot. Only link
> banks you actually want. Confirm before each.

Stop the server with `Ctrl+C` when done linking.

## Step 2 — Fetch transactions

```bash
./venv/bin/python fetch_transactions.py
```

Prints a per-bank summary of added / modified / removed and writes
`transactions.csv`.

### CSV columns (53)

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

### Raw archive (audit / QC)

`transactions_raw.jsonl.xz` is the **source of truth**: the complete, untouched
Plaid object for every transaction (keyed by `transaction_id`), stored as
xz-compressed JSONL at max compression (~25× smaller than raw). `transactions.csv`
is a derived projection of it, so the two can never drift. The archive is
lossless — it preserves every field Plaid returns, including ones the CSV omits.

Inspect it without decompressing to disk:

```bash
xzcat transactions_raw.jsonl.xz | jq '.'                 # all records, pretty
xzcat transactions_raw.jsonl.xz | jq 'select(.amount>100)'  # filter
xzcat transactions_raw.jsonl.xz | wc -l                  # count
```

## Step 3 — Schedule (daily)

A wrapper `run_fetch.sh` runs the fetch and appends output to `logs/fetch.log`.

Add a cron job (runs daily at 7am):

```bash
crontab -e
```

Add this line:

```
0 7 * * * ~/transactions/run_fetch.sh
```

Verify it works first by running the wrapper by hand:

```bash
./run_fetch.sh && tail -n 20 logs/fetch.log
```

> **WSL note:** cron is not started automatically in WSL. Start it once per boot
> with `sudo service cron start`, or add that to your shell profile. Alternatively
> run the job from the Windows Task Scheduler.

## Testing in sandbox first (optional)

To prove the whole pipeline with fake data before linking real banks:

1. In `.env` set `PLAID_ENV=sandbox` and `PLAID_SECRET=<your Sandbox secret>`.
2. Run `app.py`, link any bank, and use Plaid's test creds `user_good` / `pass_good`.
3. Run `fetch_transactions.py` and inspect `transactions.csv`.
4. Switch `.env` back to `production` + Production secret, then delete the sandbox
   state files (`tokens.json`, `sync_cursors.json`, `transaction_store.json`,
   `transactions.csv`) so real data starts clean, and link your real banks.

## Notes / constraints

- Trial = the `production` environment string (free, real data). Don't apply for
  full Production access — it's a one-way door off the free Trial.
- Auth is `client_id` + `secret` in the request body (no client certificate).
- Access tokens are persisted the instant they're issued; keep `tokens.json` safe.
