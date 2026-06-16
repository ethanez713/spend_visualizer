#!/usr/bin/env python3
"""Generate a SYNTHETIC data root for demos/screenshots — never real data.

Writes a categorized store + accounts.yaml + budget.yaml that mirror the real
data-root layout, so the analyzer runs against it unchanged (point
SPEND_VISUALIZER_DATA at the output dir). Everything here is fake: invented
people, banks, and merchants, deterministic from a seed. The amounts and
PFC codes are realistic enough to exercise every view (spend/income/excluded,
recurrence detection, multi-person attribution, budget goals).

Usage:
    python -m tools.gen_demo_data [--out DIR] [--months N] [--seed N]

Default --out is a fresh temp dir (path printed on exit). It will NOT write
inside the repo or the real data root.
"""
from __future__ import annotations

import argparse
import json
import random
import tempfile
from datetime import date, timedelta
from pathlib import Path

import yaml

# (merchant, primary, detailed, lo, hi, channel) — spend is +, income is - (set later).
MERCHANTS = {
    "groceries": [
        ("Green Valley Market", "FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES", 28, 145, "in store"),
        ("FreshMart", "FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES", 22, 120, "in store"),
        ("Cornerstone Grocery Co-op", "FOOD_AND_DRINK", "FOOD_AND_DRINK_GROCERIES", 18, 95, "in store"),
    ],
    "restaurant": [
        ("The Copper Kettle", "FOOD_AND_DRINK", "FOOD_AND_DRINK_RESTAURANT", 24, 88, "in store"),
        ("Saffron Bistro", "FOOD_AND_DRINK", "FOOD_AND_DRINK_RESTAURANT", 30, 110, "in store"),
        ("Nonna's Table", "FOOD_AND_DRINK", "FOOD_AND_DRINK_RESTAURANT", 18, 72, "in store"),
    ],
    "coffee": [
        ("Daily Grind Coffee", "FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE", 3.5, 9.5, "in store"),
        ("Bean & Leaf", "FOOD_AND_DRINK", "FOOD_AND_DRINK_COFFEE", 4.0, 8.0, "in store"),
    ],
    "fastfood": [
        ("Burger Barn", "FOOD_AND_DRINK", "FOOD_AND_DRINK_FAST_FOOD", 6, 19, "in store"),
        ("Taco Stop", "FOOD_AND_DRINK", "FOOD_AND_DRINK_FAST_FOOD", 7, 22, "in store"),
    ],
    "gas": [
        ("QuickFuel", "TRANSPORTATION", "TRANSPORTATION_GAS", 32, 68, "in store"),
        ("Petro Stop", "TRANSPORTATION", "TRANSPORTATION_GAS", 30, 64, "in store"),
    ],
    "rideshare": [
        ("RideNow", "TRANSPORTATION", "TRANSPORTATION_TAXIS_AND_RIDE_SHARES", 9, 38, "online"),
    ],
    "transit": [
        ("Metro Transit", "TRANSPORTATION", "TRANSPORTATION_PUBLIC_TRANSIT", 2.75, 30, "online"),
    ],
    "online": [
        ("Shopzilla", "GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES", 14, 210, "online"),
        ("MegaMart Online", "GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES", 12, 160, "online"),
    ],
    "clothing": [
        ("Urban Thread", "GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_CLOTHING_AND_ACCESSORIES", 25, 165, "in store"),
        ("Cobblestone Shoes", "GENERAL_MERCHANDISE", "GENERAL_MERCHANDISE_CLOTHING_AND_ACCESSORIES", 40, 180, "in store"),
    ],
    "pharmacy": [
        ("WellCare Pharmacy", "MEDICAL", "MEDICAL_PHARMACIES_AND_SUPPLEMENTS", 8, 64, "in store"),
    ],
    "doctor": [
        ("Riverside Clinic", "MEDICAL", "MEDICAL_PRIMARY_CARE", 40, 230, "in store"),
    ],
    "flight": [
        ("SkyHigh Airways", "TRAVEL", "TRAVEL_FLIGHTS", 180, 520, "online"),
    ],
    "lodging": [
        ("Coastline Inn", "TRAVEL", "TRAVEL_LODGING", 120, 340, "online"),
    ],
}

# Fixed monthly recurring (merchant, primary, detailed, amount, day-of-month, channel).
MONTHLY = [
    ("Skyline Apartments", "RENT_AND_UTILITIES", "RENT_AND_UTILITIES_RENT", 2100, 1, "online"),
    ("City Power & Light", "RENT_AND_UTILITIES", "RENT_AND_UTILITIES_GAS_AND_ELECTRICITY", None, 8, "online"),
    ("Fiberlink Internet", "RENT_AND_UTILITIES", "RENT_AND_UTILITIES_INTERNET_AND_CABLE", 69.99, 12, "online"),
    ("Metro Water Dept", "RENT_AND_UTILITIES", "RENT_AND_UTILITIES_WATER", None, 15, "online"),
    ("Streamflix", "ENTERTAINMENT", "ENTERTAINMENT_TV_AND_MOVIES", 15.49, 5, "online"),
    ("Tunestream", "ENTERTAINMENT", "ENTERTAINMENT_MUSIC_AND_AUDIO", 10.99, 7, "online"),
    ("Iron Peak Fitness", "PERSONAL_CARE", "PERSONAL_CARE_GYMS_AND_FITNESS_CENTERS", 45.00, 3, "in store"),
    ("SafeGuard Insurance", "GENERAL_SERVICES", "GENERAL_SERVICES_INSURANCE", 128.00, 18, "online"),
]

EXCLUDED = [
    ("Transfer to Savings", "TRANSFER_OUT", "TRANSFER_OUT_ACCOUNT_TRANSFER", 250, 650, "online"),
    ("Card Payment - Thank You", "LOAN_PAYMENTS", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT", 200, 900, "online"),
]

CITY = {"city": "Portland", "region": "OR", "country": "US"}


def _mid(name: str) -> str:
    return "ent_" + "".join(c for c in name.lower() if c.isalnum())[:18]


def _rec(rng, tid, acct, owner, amount, d, name, primary, detailed, channel, conf="HIGH"):
    return {
        "transaction_id": tid,
        "account_id": acct,
        "amount": round(amount, 2),
        "date": d.isoformat(),
        "authorized_date": d.isoformat(),
        "iso_currency_code": "USD",
        "name": name.upper(),
        "merchant_name": name,
        "merchant_entity_id": _mid(name),
        "payment_channel": channel,
        "location": dict(CITY),
        "personal_finance_category": {
            "primary": primary, "detailed": detailed, "confidence_level": conf,
        },
        "txn_owner": owner,
        "pending": False,
    }


def generate(out: Path, months: int, seed: int) -> Path:
    rng = random.Random(seed)
    people = [
        ("Alex Rivera", "acct_alex_chk", "acct_alex_cc"),
        ("Sam Chen", "acct_sam_chk", "acct_sam_cc"),
    ]
    end = date(2026, 6, 10)
    start = end - timedelta(days=months * 30)
    rows: list[dict] = []
    n = 0

    def nid() -> str:
        nonlocal n
        n += 1
        return f"demo_txn_{n:05d}"

    # Per person: biweekly salary (income), monthly recurring bills, random daily spend.
    for person, chk, cc in people:
        # Biweekly salary into checking (negative = money in).
        pay = rng.choice([2450, 2600, 2780])
        d = start
        while d <= end:
            rows.append(_rec(rng, nid(), chk, person, -pay, d,
                             f"{person.split()[0]}Corp Payroll", "INCOME", "INCOME_WAGES", "online"))
            d += timedelta(days=14)

        # Monthly recurring bills (rent only for Alex; mortgage-free Sam pays rent too is fine).
        for name, primary, detailed, amt, dom, channel in MONTHLY:
            if name == "Skyline Apartments" and person != "Alex Rivera":
                continue  # one renter
            cur = date(start.year, start.month, min(dom, 28))
            while cur <= end:
                a = amt if amt is not None else rng.uniform(38, 140)
                rows.append(_rec(rng, nid(), chk, person, a, cur, name, primary, detailed, channel))
                # advance one month
                y, m = (cur.year, cur.month + 1) if cur.month < 12 else (cur.year + 1, 1)
                cur = date(y, m, min(dom, 28))

        # Random discretionary/necessary spend on the credit card.
        weights = {
            "groceries": 26, "restaurant": 18, "coffee": 22, "fastfood": 10,
            "gas": 10, "rideshare": 8, "transit": 6, "online": 14, "clothing": 5,
            "pharmacy": 5, "doctor": 1, "flight": 1, "lodging": 1,
        }
        bag = [k for k, w in weights.items() for _ in range(w)]
        days = (end - start).days
        for _ in range(int(days * 1.4)):
            cat = rng.choice(bag)
            name, primary, detailed, lo, hi, channel = rng.choice(MERCHANTS[cat])
            amt = rng.uniform(lo, hi)
            d = start + timedelta(days=rng.randint(0, days))
            rows.append(_rec(rng, nid(), cc, person, amt, d, name, primary, detailed, channel))

        # A couple of excluded internal movements (so the analyzer can prove it excludes them).
        for name, primary, detailed, lo, hi, channel in EXCLUDED:
            for _ in range(months // 2):
                d = start + timedelta(days=rng.randint(0, days))
                rows.append(_rec(rng, nid(), chk, person, rng.uniform(lo, hi), d,
                                 name, primary, detailed, channel))

    rows.sort(key=lambda r: r["date"])

    # Write the store + an empty manual-edits log (analyzer reads it from the data root).
    store = out / "plaid_category_transformer" / "data"
    store.mkdir(parents=True, exist_ok=True)
    with (store / "transactions_categorized.jsonl").open("w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
    (store / "manual_edits.jsonl").write_text("", encoding="utf-8")

    # accounts.yaml + budget.yaml under the analyzer config dir.
    cfg = out / "spend_analyzer" / "config"
    cfg.mkdir(parents=True, exist_ok=True)
    accounts = {
        "accounts": {
            "acct_alex_chk": {"person": "Alex Rivera", "name": "Everyday Checking",
                              "type": "depository", "subtype": "checking",
                              "institution": "Globex Bank", "include": True},
            "acct_alex_cc": {"person": "Alex Rivera", "name": "Travel Rewards Card",
                             "type": "credit", "subtype": "credit card",
                             "institution": "Globex Bank", "include": True},
            "acct_sam_chk": {"person": "Sam Chen", "name": "Main Checking",
                             "type": "depository", "subtype": "checking",
                             "institution": "Initech CU", "include": True},
            "acct_sam_cc": {"person": "Sam Chen", "name": "Cashback Card",
                            "type": "credit", "subtype": "credit card",
                            "institution": "Initech CU", "include": True},
        }
    }
    (cfg / "accounts.yaml").write_text(yaml.safe_dump(accounts, sort_keys=False), encoding="utf-8")
    budget = {"period": "monthly", "goals": {
        "Groceries": 650, "Dining Out": 480, "Transportation": 260, "Merchandise": 380,
        "Entertainment": 60, "Personal Care": 120, "Utilities": 300, "Rent": 2100,
        "Healthcare": 180, "Travel": 350, "Services": 150,
    }}
    (cfg / "budget.yaml").write_text(yaml.safe_dump(budget, sort_keys=False), encoding="utf-8")

    print(f"wrote {len(rows)} synthetic transactions")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=None,
                    help="output data root (default: a fresh temp dir)")
    ap.add_argument("--months", type=int, default=9)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    out = args.out or Path(tempfile.mkdtemp(prefix="spend_demo_"))
    out = generate(out, args.months, args.seed)
    print(f"SPEND_VISUALIZER_DATA={out}")


if __name__ == "__main__":
    main()
