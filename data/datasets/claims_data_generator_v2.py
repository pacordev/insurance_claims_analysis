"""
Synthetic data generator - v2

Generates fake but structurally realistic rows for all 8 tables (6 data tables +
2 lookup tables) in
data/database/claims_schema_tables_v3.sql, sized to give enough volume for monthly trends and per-product comparisons:

    20,000 customers
    60 adjudicators
    35,000 policies
    50,000 claims
    ~80,000 payments (target - actual count varies a bit, see generate_payments)

Everything here is built to satisfy the v2 schema's CHECK constraints
(status/type enums, date ordering, approved_amount <= claim_amount, etc.)
so the CSVs should load into a fresh v2 database with zero violations.

Each `generate_*` function returns a plain DataFrame and takes its inputs
as arguments (no hidden global state), so they're easy to call individually
from a notebook or a pytest test.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from faker import Faker

# -------------------------
# Config - volumes chosen to give enough data for monthly trends and per-product comparisons
# -------------------------
N_CUSTOMERS = 20_000
N_ADJUDICATORS = 60
N_POLICIES = 35_000
N_CLAIMS = 50_000
N_PAYMENTS_TARGET = 80_000

SEED = 42
TODAY = date.today()

OUTPUT_DIR = Path(__file__).resolve().parent

# fixed 4-product catalogue - must match the CHECK constraints in schema v2
PRODUCTS = [
    {"product_id": 1, "product_name": "Home", "line_of_business": "Property",
     "description": "Homeowners insurance", "base_premium": 1800.00},
    {"product_id": 2, "product_name": "Auto", "line_of_business": "Auto",
     "description": "Auto insurance", "base_premium": 2000.00},
    {"product_id": 3, "product_name": "Life", "line_of_business": "Life",
     "description": "Life insurance", "base_premium": 1200.00},
    {"product_id": 4, "product_name": "Travel", "line_of_business": "Travel",
     "description": "Travel insurance", "base_premium": 250.00},
]

# which causes of loss are valid for which product - has to line up with the
# chk_claims_cause CHECK constraint in schema v2
CAUSE_OF_LOSS_BY_PRODUCT = {
    "Home": ["Fire", "WaterDamage", "Theft", "Storm"],
    "Auto": ["Collision", "Vandalism", "GlassBreakage"],
    "Life": ["Illness", "Accident", "NaturalCauses"],
    "Travel": ["TripCancellation", "MedicalEmergency", "LostLuggage"],
}

# lognormal(mean, sigma) params per product - controls how big claims tend to be.
# these are tuned together with the base_premium values in PRODUCTS so the
# loss ratio (approved / premium) lands somewhere believable per product:
#   Home ~0.65, Auto ~0.85 (the "problem" line), Life ~0.55, Travel ~0.50
# rough maths: each policy ends up with ~1.4 claims and ~70% get paid at ~80%
# of the claimed amount, so approved $ per policy is about 0.8 x the average claim.
# if you change N_CLAIMS / N_POLICIES, re-check these with analytics.financial.loss_ratio()
# - the ratio moves with claims-per-policy.
CLAIM_AMOUNT_PARAMS = {
    "Home": (7.1, 0.6),
    "Auto": (7.55, 0.5),
    "Life": (6.65, 0.4),
    "Travel": (4.9, 0.5),
}

# who typically gets paid out, by claim type - all values must stay inside
# the chk_payments_payee CHECK constraint
PAYEE_BY_CLAIM_TYPE = {
    "Home": ["Vendor", "Customer", "Repair Shop"],
    "Auto": ["Repair Shop", "Customer", "Vendor"],
    "Life": ["Customer"],
    "Travel": ["TravelAgency", "Customer", "Hospital"],
}

PAYMENT_METHODS = ["EFT", "Cheque", "Credit"]
CLAIM_STATUSES = ["Pending", "Approved", "Denied", "Closed"]
CLAIM_STATUS_WEIGHTS = [0.20, 0.40, 0.10, 0.30]
POLICY_DEPARTMENTS = ["Claims", "Fraud", "Property", "Auto"]
SENIORITY_LEVELS = ["Junior", "Senior", "Lead"]
CERTIFICATIONS = ["CIP", "FCIP", "None"]


# Canadian provinces: (population weight, real cities in that province, postal code first letters).
# weights are rough population shares so Ontario/Quebec get more customers than PEI.
# keeping city and province together means we never end up with "Toronto, Alberta".
CANADA_REGIONS = {
    "Ontario": (0.39, ["Toronto", "Ottawa", "Mississauga", "Hamilton", "London", "Kitchener", "Windsor"], "KLMNP"),
    "Quebec": (0.22, ["Montreal", "Quebec City", "Laval", "Gatineau", "Sherbrooke"], "GHJ"),
    "British Columbia": (0.14, ["Vancouver", "Victoria", "Surrey", "Burnaby", "Kelowna"], "V"),
    "Alberta": (0.12, ["Calgary", "Edmonton", "Red Deer", "Lethbridge"], "T"),
    "Manitoba": (0.035, ["Winnipeg", "Brandon", "Steinbach"], "R"),
    "Saskatchewan": (0.03, ["Saskatoon", "Regina", "Prince Albert"], "S"),
    "Nova Scotia": (0.026, ["Halifax", "Sydney", "Dartmouth"], "B"),
    "New Brunswick": (0.021, ["Moncton", "Saint John", "Fredericton"], "E"),
    "Newfoundland and Labrador": (0.014, ["St. John's", "Corner Brook", "Mount Pearl"], "A"),
    "Prince Edward Island": (0.004, ["Charlottetown", "Summerside"], "C"),
}


def _canadian_postal_code(first_letters: str, rng: np.random.Generator) -> str:
    """Canadian format is A1A 1A1 - first letter depends on the province"""
    letters = "ABCEGHJKLMNPRSTVXY"  # letters Canada Post actually uses
    return (
        f"{rng.choice(list(first_letters))}{rng.integers(0, 10)}{rng.choice(list(letters))} "
        f"{rng.integers(0, 10)}{rng.choice(list(letters))}{rng.integers(0, 10)}"
    )


def _make_email(first_name: str, last_name: str, unique_id: int, fake: Faker) -> str:
    """builds a guaranteed-unique email by baking the row's id into it"""
    domain = fake.free_email_domain()
    return f"{first_name.lower()}.{last_name.lower()}{unique_id}@{domain}"


def generate_customers(n: int, fake: Faker) -> pd.DataFrame:
    """one row per customer - the top of our FK chain"""
    provinces = list(CANADA_REGIONS)
    weights = np.array([CANADA_REGIONS[p][0] for p in provinces])
    weights = weights / weights.sum()
    rng = np.random.default_rng(SEED)

    rows = []
    for cid in range(1, n + 1):
        first_name = fake.first_name()
        last_name = fake.last_name()
        province = str(rng.choice(provinces, p=weights))
        _, cities, postal_prefix = CANADA_REGIONS[province]
        rows.append({
            "customer_id": cid,
            "first_name": first_name,
            "last_name": last_name,
            "dob": fake.date_of_birth(minimum_age=18, maximum_age=90),
            "email": _make_email(first_name, last_name, cid, fake),
            "phone": fake.phone_number(),
            "address_line1": fake.street_address(),
            "city": str(rng.choice(cities)),
            "province": province,
            "postal_code": _canadian_postal_code(postal_prefix, rng),
            "created_at": datetime.now(),
        })
    return pd.DataFrame(rows)


def generate_ref_line_of_business() -> pd.DataFrame:
    """lookup table: the lines of business our products belong to (Property, Auto, ...)"""
    lines = sorted({p["line_of_business"] for p in PRODUCTS})
    return pd.DataFrame({"line_of_business": lines})


def generate_ref_cause_of_loss() -> pd.DataFrame:
    """lookup table: which causes of loss are valid for which product"""
    return pd.DataFrame(
        [
            {"product_name": product, "cause_of_loss": cause}
            for product, causes in CAUSE_OF_LOSS_BY_PRODUCT.items()
            for cause in causes
        ]
    )


def generate_products() -> pd.DataFrame:
    """fixed catalogue, not randomised - matches the schema's CHECK constraints"""
    df = pd.DataFrame(PRODUCTS)
    df["created_at"] = datetime.now()
    return df


def generate_adjudicators(n: int, fake: Faker) -> pd.DataFrame:
    """claims staff who get assigned to claims and make approve/deny decisions"""
    rows = []
    for aid in range(1, n + 1):
        first_name = fake.first_name()
        last_name = fake.last_name()
        rows.append({
            "adjudicator_id": aid,
            "first_name": first_name,
            "last_name": last_name,
            "email": _make_email(first_name, last_name, aid, fake),
            "phone": fake.phone_number(),
            "department": np.random.choice(POLICY_DEPARTMENTS),
            "seniority_level": np.random.choice(SENIORITY_LEVELS),
            "certification": np.random.choice(CERTIFICATIONS),
            # v2: this is a capacity baseline, not live workload - real workload
            # gets computed from COUNT(claims) in the analytics layer
            "capacity_score": int(np.random.randint(10, 101)),
            "created_at": datetime.now(),
        })
    return pd.DataFrame(rows)


def generate_policies(n: int, customers_df: pd.DataFrame, products_df: pd.DataFrame) -> pd.DataFrame:
    """
    one row per policy. status is derived from the dates (expired policies
    can't be 'Active') rather than picked independently, so the data stays
    internally consistent for analytics.
    """
    customer_id = np.random.choice(customers_df["customer_id"].values, size=n, replace=True)
    product_idx = np.random.choice(len(products_df), size=n, replace=True)
    product_row = products_df.iloc[product_idx].reset_index(drop=True)

    # effective_date sampled somewhere in the last 5 years, term is always 1 year
    today64 = np.datetime64(TODAY)
    days_ago = np.random.randint(0, 5 * 365, size=n)
    effective_date = today64 - days_ago.astype("timedelta64[D]")
    expiry_date = effective_date + np.timedelta64(365, "D")

    base_premium = product_row["base_premium"].to_numpy(dtype=float)
    premium_amount = np.round(np.random.normal(loc=base_premium, scale=base_premium * 0.2), 2)
    premium_amount = np.clip(premium_amount, 50, None)
    coverage_limit = np.round(premium_amount * np.random.uniform(10, 50, size=n), 2)
    deductible = np.round(np.random.uniform(250, 2000, size=n), 2)

    # status follows from whether the policy has actually lapsed yet
    already_expired = expiry_date < today64
    cancelled_roll = np.random.rand(n)
    status = np.where(
        already_expired,
        np.where(cancelled_roll < 0.10, "Cancelled", "Expired"),
        np.where(cancelled_roll < 0.08, "Cancelled", "Active"),
    )

    df = pd.DataFrame({
        "policy_id": np.arange(1, n + 1),
        "policy_number": [f"POL-{i:06d}" for i in range(1, n + 1)],
        "customer_id": customer_id,
        "product_id": product_row["product_id"].values,
        "effective_date": effective_date,
        "expiry_date": expiry_date,
        "status": status,
        "premium_amount": premium_amount,
        "coverage_limit": coverage_limit,
        "deductible": deductible,
    })
    df["created_at"] = datetime.now()
    df["updated_at"] = df["created_at"]
    return df


def generate_claims(n: int, policies_df: pd.DataFrame, products_df: pd.DataFrame,
                     adjudicators_df: pd.DataFrame) -> pd.DataFrame:
    """
    one row per claim. dates are built in order (loss -> reported -> decision)
    so they always satisfy the schema's date-ordering CHECK constraints, and
    fraud_score/is_flagged_fraud are correlated with how unusually large the
    claim is for its product - gives the later anomaly-detection analysis
    something real to find.
    """
    sampled_policy_ids = np.random.choice(policies_df["policy_id"].values, size=n, replace=True)
    chosen = (
        pd.DataFrame({"policy_id": sampled_policy_ids})
        .merge(policies_df[["policy_id", "effective_date", "expiry_date", "product_id"]], on="policy_id", how="left")
        .merge(products_df[["product_id", "product_name"]], on="product_id", how="left")
    )

    # claim_date: somewhere between the policy's effective date and whichever
    # comes first, its expiry or today (can't have a claim in the future)
    window_end = np.minimum(chosen["expiry_date"].values, np.datetime64(TODAY))
    window_days = ((window_end - chosen["effective_date"].values) / np.timedelta64(1, "D")).astype(int)
    window_days = np.clip(window_days, 0, None)
    offset = (np.random.rand(n) * (window_days + 1)).astype(int)
    claim_date = chosen["effective_date"].values + offset * np.timedelta64(1, "D")

    # reported a few days to two weeks after the loss, never after today
    report_lag = np.random.randint(0, 15, size=n)
    reported_date = claim_date + report_lag * np.timedelta64(1, "D")
    reported_date = np.minimum(reported_date, np.datetime64(TODAY))

    status = np.random.choice(CLAIM_STATUSES, size=n, p=CLAIM_STATUS_WEIGHTS)

    # claim amount: lognormal, params depend on product
    claim_amount = np.empty(n, dtype=float)
    cause_of_loss = np.empty(n, dtype=object)
    for product_name, (mu, sigma) in CLAIM_AMOUNT_PARAMS.items():
        mask = (chosen["product_name"] == product_name).values
        count = int(mask.sum())
        if count == 0:
            continue
        claim_amount[mask] = np.round(np.random.lognormal(mean=mu, sigma=sigma, size=count), 2)
        cause_of_loss[mask] = np.random.choice(CAUSE_OF_LOSS_BY_PRODUCT[product_name], size=count)

    # fraud score: baseline noise + a boost for claims that are unusually big
    # for their product (z-score of claim_amount within product), so flagged
    # claims skew toward outliers without being a perfect 1:1 mapping
    fraud_score = np.random.beta(2, 20, size=n) * 60  # mostly low baseline noise
    for product_name in CLAIM_AMOUNT_PARAMS:
        mask = (chosen["product_name"] == product_name).values
        if mask.sum() < 2:
            continue
        amounts = claim_amount[mask]
        z = (amounts - amounts.mean()) / (amounts.std() + 1e-9)
        fraud_score[mask] += np.clip(z, 0, 3) * 15
    fraud_score = np.clip(fraud_score, 0, 100)
    # flag roughly the top ~4% riskiest claims as fraud
    flag_threshold = np.percentile(fraud_score, 96)
    is_flagged_fraud = fraud_score >= flag_threshold

    # processing time: lognormal-ish base, flagged claims take longer to clear
    base_processing_days = np.random.lognormal(mean=2.1, sigma=0.5, size=n)
    processing_days = base_processing_days + np.where(is_flagged_fraud, np.random.uniform(10, 30, size=n), 0)
    processing_days = np.round(processing_days).astype(int)

    decided_mask = np.isin(status, ["Approved", "Denied", "Closed"])
    decision_date = np.full(n, np.datetime64("NaT", "D"), dtype="datetime64[D]")
    decision_date[decided_mask] = reported_date[decided_mask] + processing_days[decided_mask] * np.timedelta64(1, "D")
    decision_date[decided_mask] = np.minimum(decision_date[decided_mask], np.datetime64(TODAY))

    approved_amount = np.full(n, np.nan)
    approved_amount[status == "Denied"] = 0.0
    approved_mask = np.isin(status, ["Approved", "Closed"])
    approved_amount[approved_mask] = np.round(
        claim_amount[approved_mask] * np.random.uniform(0.6, 1.0, size=int(approved_mask.sum())), 2
    )

    # adjudicators: most claims get one assigned, but a slice of brand-new
    # Pending claims haven't been triaged yet
    adjudicator_id = np.random.choice(adjudicators_df["adjudicator_id"].values, size=n, replace=True).astype(object)
    untriaged = (status == "Pending") & (np.random.rand(n) < 0.15)
    adjudicator_id[untriaged] = None

    df = pd.DataFrame({
        "claim_id": np.arange(1, n + 1),
        "claim_number": [f"CLM-{i:06d}" for i in range(1, n + 1)],
        "policy_id": chosen["policy_id"].values,
        "adjudicator_id": adjudicator_id,
        "claim_date": claim_date,
        "reported_date": reported_date,
        "decision_date": decision_date,
        "cause_of_loss": cause_of_loss,
        "claim_type": chosen["product_name"].values,
        "status": status,
        "claim_amount": claim_amount,
        "approved_amount": approved_amount,
        "is_flagged_fraud": is_flagged_fraud,
        "fraud_score": np.round(fraud_score, 2),
    })
    df["description"] = df["claim_type"] + " claim due to " + df["cause_of_loss"]
    df["created_at"] = datetime.now()
    df["updated_at"] = df["created_at"]
    return df


def generate_payments(target_n: int, claims_df: pd.DataFrame) -> pd.DataFrame:
    """
    only claims that actually got money approved (Approved/Closed with
    approved_amount > 0) get payments - unlike v1, we don't fabricate
    payments for denied/pending claims just to fill a row count.

    each eligible claim gets 1+ payments (Poisson-ish spread) that split its
    approved_amount, so amounts roughly reconcile back to the claim.
    """
    eligible = claims_df[
        claims_df["status"].isin(["Approved", "Closed"]) & (claims_df["approved_amount"] > 0)
    ].reset_index(drop=True)

    if eligible.empty:
        return pd.DataFrame(columns=[
            "payment_id", "claim_id", "payment_date", "payment_amount",
            "payment_method", "payee", "created_at",
        ])

    avg_payments_per_claim = np.clip(target_n / len(eligible), 1.0, 4.0)
    n_payments = 1 + np.random.poisson(lam=max(avg_payments_per_claim - 1, 0.01), size=len(eligible))
    n_payments = np.clip(n_payments, 1, 5)

    rows = []
    payment_id = 1
    for claim, k in zip(eligible.itertuples(index=False), n_payments):
        shares = np.random.dirichlet(np.ones(k))
        offsets = np.cumsum(np.random.randint(1, 30, size=k))
        payee_choices = PAYEE_BY_CLAIM_TYPE.get(claim.claim_type, ["Customer"])
        for share, offset in zip(shares, offsets):
            payment_date = min(
                pd.Timestamp(claim.decision_date).date() + timedelta(days=int(offset)),
                TODAY,
            )
            rows.append({
                "payment_id": payment_id,
                "claim_id": claim.claim_id,
                "payment_date": payment_date,
                "payment_amount": round(float(claim.approved_amount) * share, 2),
                "payment_method": np.random.choice(PAYMENT_METHODS),
                "payee": np.random.choice(payee_choices),
                "created_at": datetime.now(),
            })
            payment_id += 1

    return pd.DataFrame(rows)


def validate(dfs: dict[str, pd.DataFrame]) -> None:
    """quick sanity pass - not a substitute for the real pandera validation
    step in the ingestion pipeline, just a fast check that nothing generated
    would blow up the v2 schema's constraints"""
    claims = dfs["claims"]
    payments = dfs["payments"]

    assert dfs["customers"]["email"].is_unique, "duplicate customer emails"
    assert dfs["adjudicators"]["email"].is_unique, "duplicate adjudicator emails"
    assert (dfs["policies"]["expiry_date"] > dfs["policies"]["effective_date"]).all(), "bad policy dates"
    assert (claims["reported_date"] >= claims["claim_date"]).all(), "reported before loss"
    decided = claims["decision_date"].notna()
    assert (claims.loc[decided, "decision_date"] >= claims.loc[decided, "reported_date"]).all(), "decided before reported"
    approved = claims["approved_amount"].notna()
    assert (claims.loc[approved, "approved_amount"] <= claims.loc[approved, "claim_amount"]).all(), "overpaid claim"
    assert claims["fraud_score"].between(0, 100).all(), "fraud_score out of range"
    assert payments["payment_amount"].ge(0).all(), "negative payment"
    # every (product, cause) pair on a claim has to exist in the lookup
    valid_pairs = set(zip(dfs["ref_causes"]["product_name"], dfs["ref_causes"]["cause_of_loss"]))
    used_pairs = set(zip(claims["claim_type"], claims["cause_of_loss"]))
    assert used_pairs <= valid_pairs, f"claims use causes missing from the lookup: {used_pairs - valid_pairs}"
    assert set(dfs["products"]["product_name"]) >= set(claims["claim_type"]), "claim_type not in products"
    print("validation passed: dates, amounts, and uniqueness all check out")


def main() -> None:
    np.random.seed(SEED)
    fake = Faker("en_CA")
    Faker.seed(SEED)

    ref_lines_df = generate_ref_line_of_business()
    ref_causes_df = generate_ref_cause_of_loss()
    customers_df = generate_customers(N_CUSTOMERS, fake)
    products_df = generate_products()
    adjudicators_df = generate_adjudicators(N_ADJUDICATORS, fake)
    policies_df = generate_policies(N_POLICIES, customers_df, products_df)
    claims_df = generate_claims(N_CLAIMS, policies_df, products_df, adjudicators_df)
    payments_df = generate_payments(N_PAYMENTS_TARGET, claims_df)

    validate({
        "ref_causes": ref_causes_df,
        "products": products_df,
        "customers": customers_df,
        "adjudicators": adjudicators_df,
        "policies": policies_df,
        "claims": claims_df,
        "payments": payments_df,
    })

    ref_lines_df.to_csv(OUTPUT_DIR / "ref_line_of_business.csv", index=False)
    ref_causes_df.to_csv(OUTPUT_DIR / "ref_cause_of_loss.csv", index=False)
    customers_df.to_csv(OUTPUT_DIR / "customers.csv", index=False)
    products_df.to_csv(OUTPUT_DIR / "products.csv", index=False)
    adjudicators_df.to_csv(OUTPUT_DIR / "adjudicators.csv", index=False)
    policies_df.to_csv(OUTPUT_DIR / "policies.csv", index=False)
    claims_df.to_csv(OUTPUT_DIR / "claims.csv", index=False)
    payments_df.to_csv(OUTPUT_DIR / "payments.csv", index=False)

    print("synthetic data generated:")
    print("  lookups:     ", len(ref_lines_df), "lines of business,", len(ref_causes_df), "causes of loss")
    print("  customers:   ", len(customers_df))
    print("  products:    ", len(products_df))
    print("  adjudicators:", len(adjudicators_df))
    print("  policies:    ", len(policies_df))
    print("  claims:      ", len(claims_df))
    print("  payments:    ", len(payments_df))


if __name__ == "__main__":
    main()
