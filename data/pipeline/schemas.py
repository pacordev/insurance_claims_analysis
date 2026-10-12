"""
Pandera schemas - this is the "data validation" step from the pipeline
diagram in the README. Each schema mirrors the CHECK constraints
already in data/database/claims_schema_tables_v3.sql, so bad rows get caught here
with a readable pandas error instead of a cryptic Postgres constraint error.

Cross-table checks (e.g. "every claims.policy_id exists in policies") aren't
something pandera can see on a single DataFrame, so those live in
ingest.py's `check_foreign_keys` instead.
"""

import pandas as pd
from pandera.pandas import Check, Column, DataFrameSchema

DEPARTMENTS = ["Claims", "Fraud", "Property", "Auto"]
SENIORITY_LEVELS = ["Junior", "Senior", "Lead"]
CERTIFICATIONS = ["CIP", "FCIP", "None"]
POLICY_STATUSES = ["Active", "Expired", "Cancelled"]
CLAIM_STATUSES = ["Pending", "Approved", "Denied", "Closed"]
PAYMENT_METHODS = ["EFT", "Cheque", "Credit"]
PAYEES = ["Customer", "Vendor", "Repair Shop", "Hospital", "TravelAgency"]


# product names, lines of business and causes of loss are NOT listed here any more:
# they live in the lookup tables (ref_line_of_business, products, ref_cause_of_loss),
# so new products are just new rows. ingest.check_foreign_keys makes sure the values
# used by products/claims exist there.

REF_LINE_OF_BUSINESS_SCHEMA = DataFrameSchema(
    {"line_of_business": Column(str, unique=True)},
    strict=True,
    coerce=True,
)

REF_CAUSE_OF_LOSS_SCHEMA = DataFrameSchema(
    {
        "product_name": Column(str),
        "cause_of_loss": Column(str),
    },
    unique=["product_name", "cause_of_loss"],
    strict=True,
    coerce=True,
)

CUSTOMERS_SCHEMA = DataFrameSchema(
    {
        "customer_id": Column(int, unique=True, checks=Check.ge(1)),
        "first_name": Column(str),
        "last_name": Column(str),
        "dob": Column("datetime64[ns]", nullable=True),
        "email": Column(str, unique=True),
        "phone": Column(str, nullable=True),
        "address_line1": Column(str, nullable=True),
        "city": Column(str, nullable=True),
        "province": Column(str, nullable=True),
        "postal_code": Column(str, nullable=True),
        "created_at": Column("datetime64[ns]"),
    },
    strict=True,
    coerce=True,
)

PRODUCTS_SCHEMA = DataFrameSchema(
    {
        "product_id": Column(int, unique=True, checks=Check.ge(1)),
        "product_name": Column(str, unique=True),
        "line_of_business": Column(str),
        "description": Column(str, nullable=True),
        "base_premium": Column(float, checks=Check.ge(0)),
        "created_at": Column("datetime64[ns]"),
    },
    strict=True,
    coerce=True,
)

ADJUDICATORS_SCHEMA = DataFrameSchema(
    {
        "adjudicator_id": Column(int, unique=True, checks=Check.ge(1)),
        "first_name": Column(str),
        "last_name": Column(str),
        "email": Column(str, unique=True),
        "phone": Column(str, nullable=True),
        "department": Column(str, nullable=True, checks=Check.isin(DEPARTMENTS)),
        "seniority_level": Column(str, nullable=True, checks=Check.isin(SENIORITY_LEVELS)),
        "certification": Column(str, nullable=True, checks=Check.isin(CERTIFICATIONS)),
        "capacity_score": Column(pd.Int64Dtype(), nullable=True, checks=Check.ge(0)),
        "created_at": Column("datetime64[ns]"),
    },
    strict=True,
    coerce=True,
)

POLICIES_SCHEMA = DataFrameSchema(
    {
        "policy_id": Column(int, unique=True, checks=Check.ge(1)),
        "policy_number": Column(str, unique=True),
        "customer_id": Column(int, checks=Check.ge(1)),
        "product_id": Column(int, checks=Check.ge(1)),
        "effective_date": Column("datetime64[ns]"),
        "expiry_date": Column("datetime64[ns]"),
        "status": Column(str, checks=Check.isin(POLICY_STATUSES)),
        "premium_amount": Column(float, nullable=True, checks=Check.ge(0)),
        "coverage_limit": Column(float, nullable=True, checks=Check.ge(0)),
        "deductible": Column(float, nullable=True, checks=Check.ge(0)),
        "created_at": Column("datetime64[ns]"),
        "updated_at": Column("datetime64[ns]"),
    },
    checks=[
        Check(lambda df: df["expiry_date"] > df["effective_date"], error="expiry_date must be after effective_date"),
    ],
    strict=True,
    coerce=True,
)

CLAIMS_SCHEMA = DataFrameSchema(
    {
        "claim_id": Column(int, unique=True, checks=Check.ge(1)),
        "claim_number": Column(str, unique=True),
        "policy_id": Column(int, checks=Check.ge(1)),
        "adjudicator_id": Column(pd.Int64Dtype(), nullable=True, checks=Check.ge(1)),
        "claim_date": Column("datetime64[ns]"),
        "reported_date": Column("datetime64[ns]"),
        "decision_date": Column("datetime64[ns]", nullable=True),
        "cause_of_loss": Column(str, nullable=True),
        "claim_type": Column(str, nullable=True),
        "description": Column(str, nullable=True),
        "status": Column(str, checks=Check.isin(CLAIM_STATUSES)),
        "claim_amount": Column(float, nullable=True, checks=Check.ge(0)),
        "approved_amount": Column(float, nullable=True, checks=Check.ge(0)),
        "is_flagged_fraud": Column(bool),
        "fraud_score": Column(float, nullable=True, checks=Check.in_range(0, 100)),
        "created_at": Column("datetime64[ns]"),
        "updated_at": Column("datetime64[ns]"),
    },
    checks=[
        Check(
            lambda df: df["reported_date"] >= df["claim_date"],
            error="reported_date can't be before claim_date",
        ),
        Check(
            lambda df: df["decision_date"].isna() | (df["decision_date"] >= df["reported_date"]),
            error="decision_date can't be before reported_date",
        ),
        Check(
            lambda df: df["approved_amount"].isna() | (df["approved_amount"] <= df["claim_amount"]),
            error="approved_amount can't exceed claim_amount",
        ),
    ],
    strict=True,
    coerce=True,
)

PAYMENTS_SCHEMA = DataFrameSchema(
    {
        "payment_id": Column(int, unique=True, checks=Check.ge(1)),
        "claim_id": Column(int, checks=Check.ge(1)),
        "payment_date": Column("datetime64[ns]"),
        "payment_amount": Column(float, checks=Check.ge(0)),
        "payment_method": Column(str, nullable=True, checks=Check.isin(PAYMENT_METHODS)),
        "payee": Column(str, nullable=True, checks=Check.isin(PAYEES)),
        "created_at": Column("datetime64[ns]"),
    },
    strict=True,
    coerce=True,
)

# name -> (schema, date columns to parse, pandas dtype overrides, FK columns -> (parent table, parent column))
TABLES = {
    "ref_line_of_business": {
        "schema": REF_LINE_OF_BUSINESS_SCHEMA,
        "date_columns": [],
        "dtypes": {},
        "foreign_keys": {},
        "has_sequence": False,  # text key, nothing auto-numbered
    },
    "ref_cause_of_loss": {
        "schema": REF_CAUSE_OF_LOSS_SCHEMA,
        "date_columns": [],
        "dtypes": {},
        "foreign_keys": {"product_name": ("products", "product_name")},
        "has_sequence": False,
    },
    "customers": {
        "schema": CUSTOMERS_SCHEMA,
        "date_columns": ["dob", "created_at"],
        "dtypes": {},
        "foreign_keys": {},
    },
    "products": {
        "schema": PRODUCTS_SCHEMA,
        "date_columns": ["created_at"],
        "dtypes": {},
        "foreign_keys": {"line_of_business": ("ref_line_of_business", "line_of_business")},
    },
    "adjudicators": {
        "schema": ADJUDICATORS_SCHEMA,
        "date_columns": ["created_at"],
        "dtypes": {"capacity_score": "Int64"},
        "foreign_keys": {},
    },
    "policies": {
        "schema": POLICIES_SCHEMA,
        "date_columns": ["effective_date", "expiry_date", "created_at", "updated_at"],
        "dtypes": {},
        "foreign_keys": {"customer_id": ("customers", "customer_id"), "product_id": ("products", "product_id")},
    },
    "claims": {
        "schema": CLAIMS_SCHEMA,
        "date_columns": ["claim_date", "reported_date", "decision_date", "created_at", "updated_at"],
        "dtypes": {"adjudicator_id": "Int64"},
        "foreign_keys": {"policy_id": ("policies", "policy_id")},
        # nullable columns, checked separately since check_foreign_keys skips NaN
        "nullable_foreign_keys": {
            "adjudicator_id": ("adjudicators", "adjudicator_id"),
            "claim_type": ("products", "product_name"),
        },
        # (claim_type, cause_of_loss) has to be a pair that exists in ref_cause_of_loss,
        # i.e. the cause fits the product. skipped for rows where either is blank
        "composite_foreign_keys": [
            (("claim_type", "cause_of_loss"), ("ref_cause_of_loss", ("product_name", "cause_of_loss"))),
        ],
    },
    "payments": {
        "schema": PAYMENTS_SCHEMA,
        "date_columns": ["payment_date", "created_at"],
        "dtypes": {},
        "foreign_keys": {"claim_id": ("claims", "claim_id")},
    },
}

# bookkeeping columns the database tracks itself. they're optional in the CSVs:
# if a file doesn't have them (or leaves them blank) the loader fills in "now"
AUDIT_COLUMNS = ["created_at", "updated_at"]

# FK-safe order to load tables in
LOAD_ORDER = [
    "ref_line_of_business", "products", "ref_cause_of_loss",
    "customers", "adjudicators", "policies", "claims", "payments",
]
