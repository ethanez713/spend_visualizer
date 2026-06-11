"""Embedded copy of Plaid's Personal Finance Category (PFC) taxonomy.

Bootstrap source for ``config/taxonomy.yaml`` (PLAN.md §6.6). Embedded rather
than fetched so generation works fully offline (security baseline: offline by
default). The full published list is ~104 detailed codes across 16 primaries.
If Plaid adds codes, append them here; unmapped atoms are surfaced by the QC
panel regardless.

TIER_DEFAULTS maps each primary -> (tier0 necessity, tier1 friendly group).
ATOM_OVERRIDES lets a *child* atom land in a different necessity/tier1 than its
primary's default — e.g. restaurants are Discretionary while groceries are
Necessary, even though both are FOOD_AND_DRINK. Strict nesting still holds:
every atom (and every tier1) maps to exactly ONE tier0, so rollups never
double-count (PLAN.md §6.3/§6.4).

tier1 names are aligned to the user's 2026 budget sheet so budget.yaml goals
map 1:1 (Mortgage, Utilities, House, Dining Out, Groceries, Travel, Merchandise,
Transportation, Healthcare, Entertainment, Gifts/Charity, Other).

DETAILED lists every detailed code grouped by primary.
"""
from __future__ import annotations

# tier0 necessity buckets: Necessary | Discretionary | Income | Transfer
TIER_DEFAULTS: dict[str, tuple[str, str]] = {
    "INCOME":                  ("Income",        "Income"),
    "TRANSFER_IN":             ("Transfer",      "Transfer In"),
    "TRANSFER_OUT":            ("Transfer",      "Transfer Out"),
    "LOAN_PAYMENTS":           ("Necessary",     "Loans"),
    "BANK_FEES":               ("Necessary",     "Fees"),
    "ENTERTAINMENT":           ("Discretionary", "Entertainment"),
    "FOOD_AND_DRINK":          ("Discretionary", "Dining Out"),
    "GENERAL_MERCHANDISE":     ("Discretionary", "Merchandise"),
    "HOME_IMPROVEMENT":        ("Discretionary", "House"),
    "MEDICAL":                 ("Necessary",     "Healthcare"),
    "PERSONAL_CARE":           ("Discretionary", "Personal Care"),
    "GENERAL_SERVICES":        ("Necessary",     "Services"),
    "GOVERNMENT_AND_NON_PROFIT": ("Necessary",   "Government"),
    "TRANSPORTATION":          ("Necessary",     "Transportation"),
    "TRAVEL":                  ("Discretionary", "Travel"),
    "RENT_AND_UTILITIES":      ("Necessary",     "Utilities"),
    "OTHER":                   ("Discretionary", "Other"),
}

# Per-atom (tier0, tier1) overrides. The signature non-1:1 slicing the user
# wants lives here: a child category can opt out of its primary's necessity.
ATOM_OVERRIDES: dict[str, tuple[str, str]] = {
    # FOOD_AND_DRINK splits: groceries are necessary, the rest is dining out.
    "FOOD_AND_DRINK_GROCERIES":               ("Necessary",     "Groceries"),
    # LOAN_PAYMENTS: mortgage is a necessary housing cost & its own tier1
    # (and is intentionally NOT excluded — the #1 double-count trap).
    "LOAN_PAYMENTS_MORTGAGE_PAYMENT":         ("Necessary",     "Mortgage"),
    "LOAN_PAYMENTS_CAR_PAYMENT":              ("Necessary",     "Transportation"),
    # Gifts & charity pulled together from two primaries.
    "GENERAL_MERCHANDISE_GIFTS_AND_NOVELTIES": ("Discretionary", "Gifts/Charity"),
    "GOVERNMENT_AND_NON_PROFIT_DONATIONS":    ("Discretionary", "Gifts/Charity"),
    "GOVERNMENT_AND_NON_PROFIT_TAX_PAYMENT":  ("Necessary",     "Taxes"),
    # Rent kept distinct from metered utilities.
    "RENT_AND_UTILITIES_RENT":                ("Necessary",     "Rent"),
}


def atom_tiers(detailed: str, primary: str) -> tuple[str, str, str]:
    """Resolve (tier0, tier1, tier2) for a detailed atom."""
    tier0, tier1 = ATOM_OVERRIDES.get(detailed, TIER_DEFAULTS.get(primary, ("Discretionary", "Other")))
    tier2 = humanize_tier2(detailed, primary)
    return tier0, tier1, tier2

DETAILED: dict[str, list[str]] = {
    "INCOME": [
        "INCOME_DIVIDENDS", "INCOME_INTEREST_EARNED", "INCOME_RETIREMENT_PENSION",
        "INCOME_TAX_REFUND", "INCOME_UNEMPLOYMENT", "INCOME_WAGES",
        "INCOME_OTHER_INCOME", "INCOME_SALARY", "INCOME_CONTRACTOR", "INCOME_OTHER",
    ],
    "TRANSFER_IN": [
        "TRANSFER_IN_CASH_ADVANCES_AND_LOANS", "TRANSFER_IN_DEPOSIT",
        "TRANSFER_IN_INVESTMENT_AND_RETIREMENT_FUNDS", "TRANSFER_IN_SAVINGS",
        "TRANSFER_IN_ACCOUNT_TRANSFER", "TRANSFER_IN_OTHER_TRANSFER_IN",
        "TRANSFER_IN_TRANSFER_IN_FROM_APPS",
    ],
    "TRANSFER_OUT": [
        "TRANSFER_OUT_INVESTMENT_AND_RETIREMENT_FUNDS", "TRANSFER_OUT_SAVINGS",
        "TRANSFER_OUT_WITHDRAWAL", "TRANSFER_OUT_ACCOUNT_TRANSFER",
        "TRANSFER_OUT_OTHER_TRANSFER_OUT", "TRANSFER_OUT_TRANSFER_OUT_FROM_APPS",
    ],
    "LOAN_PAYMENTS": [
        "LOAN_PAYMENTS_CAR_PAYMENT", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT",
        "LOAN_PAYMENTS_PERSONAL_LOAN_PAYMENT", "LOAN_PAYMENTS_MORTGAGE_PAYMENT",
        "LOAN_PAYMENTS_STUDENT_LOAN_PAYMENT", "LOAN_PAYMENTS_OTHER_PAYMENT",
    ],
    "BANK_FEES": [
        "BANK_FEES_ATM_FEES", "BANK_FEES_FOREIGN_TRANSACTION_FEES",
        "BANK_FEES_INSUFFICIENT_FUNDS", "BANK_FEES_INTEREST_CHARGE",
        "BANK_FEES_OVERDRAFT_FEES", "BANK_FEES_OTHER_BANK_FEES",
    ],
    "ENTERTAINMENT": [
        "ENTERTAINMENT_CASINOS_AND_GAMBLING", "ENTERTAINMENT_MUSIC_AND_AUDIO",
        "ENTERTAINMENT_SPORTING_EVENTS_AMUSEMENT_PARKS_AND_MUSEUMS",
        "ENTERTAINMENT_TV_AND_MOVIES", "ENTERTAINMENT_VIDEO_GAMES",
        "ENTERTAINMENT_OTHER_ENTERTAINMENT",
    ],
    "FOOD_AND_DRINK": [
        "FOOD_AND_DRINK_BEER_WINE_AND_LIQUOR", "FOOD_AND_DRINK_COFFEE",
        "FOOD_AND_DRINK_FAST_FOOD", "FOOD_AND_DRINK_GROCERIES",
        "FOOD_AND_DRINK_RESTAURANT", "FOOD_AND_DRINK_VENDING_MACHINES",
        "FOOD_AND_DRINK_OTHER_FOOD_AND_DRINK",
    ],
    "GENERAL_MERCHANDISE": [
        "GENERAL_MERCHANDISE_BOOKSTORES_AND_NEWSSTANDS",
        "GENERAL_MERCHANDISE_CLOTHING_AND_ACCESSORIES",
        "GENERAL_MERCHANDISE_CONVENIENCE_STORES",
        "GENERAL_MERCHANDISE_DEPARTMENT_STORES",
        "GENERAL_MERCHANDISE_DISCOUNT_STORES",
        "GENERAL_MERCHANDISE_ELECTRONICS",
        "GENERAL_MERCHANDISE_GIFTS_AND_NOVELTIES",
        "GENERAL_MERCHANDISE_OFFICE_SUPPLIES",
        "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES",
        "GENERAL_MERCHANDISE_PET_SUPPLIES",
        "GENERAL_MERCHANDISE_SPORTING_GOODS",
        "GENERAL_MERCHANDISE_SUPERSTORES",
        "GENERAL_MERCHANDISE_TOBACCO_AND_VAPE",
        "GENERAL_MERCHANDISE_OTHER_GENERAL_MERCHANDISE",
    ],
    "HOME_IMPROVEMENT": [
        "HOME_IMPROVEMENT_FURNITURE", "HOME_IMPROVEMENT_HARDWARE",
        "HOME_IMPROVEMENT_REPAIR_AND_MAINTENANCE", "HOME_IMPROVEMENT_SECURITY",
        "HOME_IMPROVEMENT_OTHER_HOME_IMPROVEMENT",
    ],
    "MEDICAL": [
        "MEDICAL_DENTAL_CARE", "MEDICAL_EYE_CARE", "MEDICAL_NURSING_CARE",
        "MEDICAL_PHARMACIES_AND_SUPPLEMENTS", "MEDICAL_PRIMARY_CARE",
        "MEDICAL_VETERINARY_SERVICES", "MEDICAL_OTHER_MEDICAL",
    ],
    "PERSONAL_CARE": [
        "PERSONAL_CARE_GYMS_AND_FITNESS_CENTERS", "PERSONAL_CARE_HAIR_AND_BEAUTY",
        "PERSONAL_CARE_LAUNDRY_AND_DRY_CLEANING", "PERSONAL_CARE_OTHER_PERSONAL_CARE",
    ],
    "GENERAL_SERVICES": [
        "GENERAL_SERVICES_ACCOUNTING_AND_FINANCIAL_PLANNING",
        "GENERAL_SERVICES_AUTOMOTIVE", "GENERAL_SERVICES_CHILDCARE",
        "GENERAL_SERVICES_CONSULTING_AND_LEGAL", "GENERAL_SERVICES_EDUCATION",
        "GENERAL_SERVICES_INSURANCE", "GENERAL_SERVICES_POSTAGE_AND_SHIPPING",
        "GENERAL_SERVICES_STORAGE", "GENERAL_SERVICES_OTHER_GENERAL_SERVICES",
    ],
    "GOVERNMENT_AND_NON_PROFIT": [
        "GOVERNMENT_AND_NON_PROFIT_DONATIONS",
        "GOVERNMENT_AND_NON_PROFIT_GOVERNMENT_DEPARTMENTS_AND_AGENCIES",
        "GOVERNMENT_AND_NON_PROFIT_TAX_PAYMENT",
        "GOVERNMENT_AND_NON_PROFIT_OTHER_GOVERNMENT_AND_NON_PROFIT",
    ],
    "TRANSPORTATION": [
        "TRANSPORTATION_BIKES_AND_SCOOTERS", "TRANSPORTATION_GAS",
        "TRANSPORTATION_PARKING", "TRANSPORTATION_PUBLIC_TRANSIT",
        "TRANSPORTATION_TAXIS_AND_RIDE_SHARES", "TRANSPORTATION_TOLLS",
        "TRANSPORTATION_OTHER_TRANSPORTATION",
    ],
    "TRAVEL": [
        "TRAVEL_FLIGHTS", "TRAVEL_LODGING", "TRAVEL_RENTAL_CARS",
        "TRAVEL_OTHER_TRAVEL",
    ],
    "RENT_AND_UTILITIES": [
        "RENT_AND_UTILITIES_GAS_AND_ELECTRICITY", "RENT_AND_UTILITIES_INTERNET_AND_CABLE",
        "RENT_AND_UTILITIES_RENT", "RENT_AND_UTILITIES_SEWAGE_AND_WASTE_MANAGEMENT",
        "RENT_AND_UTILITIES_TELEPHONE", "RENT_AND_UTILITIES_WATER",
        "RENT_AND_UTILITIES_OTHER_UTILITIES",
    ],
    "OTHER": [
        "OTHER_OTHER",
    ],
}


def humanize_tier2(detailed: str, primary: str) -> str:
    """Derive a friendly tier2 leaf name from a detailed code.

    Strips the primary prefix and title-cases the remainder. e.g.
    TRANSPORTATION_TAXIS_AND_RIDE_SHARES -> 'Taxis And Ride Shares'.
    """
    suffix = detailed[len(primary) + 1:] if detailed.startswith(primary + "_") else detailed
    # collapse the boilerplate "OTHER_<PRIMARY>" leaves to just "Other"
    if suffix.startswith("OTHER_") or suffix == "OTHER":
        return "Other"
    words = suffix.replace("_AND_", " & ").split("_")
    return " ".join(w.capitalize() for w in words if w)


def all_atoms() -> list[tuple[str, str]]:
    """Yield (primary, detailed) for the full embedded taxonomy."""
    out: list[tuple[str, str]] = []
    for primary, details in DETAILED.items():
        for d in details:
            out.append((primary, d))
    return out
