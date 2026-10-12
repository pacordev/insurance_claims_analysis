"""
Lightweight table definitions for the analytics queries.

We're NOT reflecting from the live database or using ORM models on purpose:
these are just names + types, so the same queries compile for Postgres,
MySQL, SQLite, SQL Server... whatever SQLAlchemy supports. That's the whole
point of building with SQLAlchemy Core instead of hand-written SQL strings.
"""

from sqlalchemy import Boolean, Date, Integer, Numeric, String, column, table

# money comes back as plain floats (not Decimal) so pandas can do math on it right away
MONEY = Numeric(12, 2, asdecimal=False)

customers = table(
    "customers",
    column("customer_id", Integer),
    column("dob", Date),
    column("city", String),
    column("province", String),
)

products = table(
    "products",
    column("product_id", Integer),
    column("product_name", String),
    column("line_of_business", String),
)

adjudicators = table(
    "adjudicators",
    column("adjudicator_id", Integer),
    column("first_name", String),
    column("last_name", String),
    column("department", String),
    column("seniority_level", String),
    column("capacity_score", Integer),
)

policies = table(
    "policies",
    column("policy_id", Integer),
    column("customer_id", Integer),
    column("product_id", Integer),
    column("effective_date", Date),
    column("expiry_date", Date),
    column("status", String),
    column("premium_amount", MONEY),
)

claims = table(
    "claims",
    column("claim_id", Integer),
    column("policy_id", Integer),
    column("adjudicator_id", Integer),
    column("claim_date", Date),
    column("reported_date", Date),
    column("decision_date", Date),
    column("cause_of_loss", String),
    column("claim_type", String),
    column("status", String),
    column("claim_amount", MONEY),
    column("approved_amount", MONEY),
    column("is_flagged_fraud", Boolean),
    column("fraud_score", Numeric(5, 2, asdecimal=False)),
)

payments = table(
    "payments",
    column("payment_id", Integer),
    column("claim_id", Integer),
    column("payment_date", Date),
    column("payment_amount", MONEY),
    column("payment_method", String),
    column("payee", String),
)

# the joins almost every query needs, defined once so they stay consistent
claims_with_policy = claims.join(policies, claims.c.policy_id == policies.c.policy_id)
claims_with_product = claims_with_policy.join(
    products, policies.c.product_id == products.c.product_id
)
claims_with_customer = claims_with_policy.join(
    customers, policies.c.customer_id == customers.c.customer_id
)
# claim -> policy -> product AND customer, for segment/regional analysis
claims_with_product_and_customer = claims_with_product.join(
    customers, policies.c.customer_id == customers.c.customer_id
)
policies_with_product = policies.join(products, policies.c.product_id == products.c.product_id)
policies_with_customer = policies.join(
    customers, policies.c.customer_id == customers.c.customer_id
)

# which claim statuses count as "approved" vs "denied" (pending = no decision yet, ignored)
APPROVED_STATUSES = ("Approved", "Closed")
DENIED_STATUSES = ("Denied",)
