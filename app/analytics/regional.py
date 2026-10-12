"""
Customer and regional metrics: claims + loss ratio by province/city,
and customer segmentation by age band.
"""

from datetime import date
from typing import Literal

import pandas as pd
from sqlalchemy import Engine, func, select

from app.analytics.common import Filters, run_query, safe_divide
from app.analytics.financial import build_loss_ratio
from app.analytics.tables import (
    APPROVED_STATUSES,
    claims,
    claims_with_product_and_customer,
    customers,
    policies,
    policies_with_customer,
    products,
)

AGE_BANDS = [0, 25, 35, 45, 55, 65, 200]
AGE_LABELS = ["<25", "25-34", "35-44", "45-54", "55-64", "65+"]


def regional_analysis(
    engine: Engine,
    level: Literal["province", "city"] = "province",
    filters: Filters | None = None,
) -> pd.DataFrame:
    """Claim volume, amounts and loss ratio per province (or per city)."""
    f = filters or Filters()
    region = {"province": customers.c.province, "city": customers.c.city}[level]

    # premium side needs the product join only if a product filter is set
    premium_source = policies_with_customer
    if f.product is not None:
        premium_source = premium_source.join(
            products, policies.c.product_id == products.c.product_id
        )
    premium_stmt = f.apply(
        select(region, func.sum(policies.c.premium_amount).label("premium_total"))
        .select_from(premium_source)
        .group_by(region),
        dates=False,
    )

    volume_stmt = f.apply(
        select(
            region,
            func.count(claims.c.claim_id).label("claims"),
            func.sum(claims.c.claim_amount).label("total_claim_amount"),
        )
        .select_from(claims_with_product_and_customer)
        .group_by(region)
    )
    approved_stmt = f.apply(
        select(region, func.sum(claims.c.approved_amount).label("approved_total"))
        .select_from(claims_with_product_and_customer)
        .where(claims.c.status.in_(APPROVED_STATUSES))
        .group_by(region)
    )

    premium = run_query(engine, premium_stmt)
    volume = run_query(engine, volume_stmt)
    approved = run_query(engine, approved_stmt)

    df = build_loss_ratio(premium, approved, [level]).merge(volume, on=level, how="left")
    df["claims"] = df["claims"].fillna(0).astype(int)
    df["total_claim_amount"] = df["total_claim_amount"].fillna(0.0)
    return df.sort_values("claims", ascending=False).reset_index(drop=True)


def customer_segmentation(
    engine: Engine, today: date | None = None, filters: Filters | None = None
) -> pd.DataFrame:
    """
    Claim frequency and severity by age band and product. Age is worked out in
    pandas from dob (`today` is a parameter so tests get stable results).
    """
    f = filters or Filters()
    today = today or date.today()

    claims_stmt = f.apply(
        select(
            customers.c.customer_id,
            customers.c.dob,
            products.c.product_name,
            claims.c.claim_amount,
        ).select_from(claims_with_product_and_customer)
    )
    df = run_query(engine, claims_stmt).dropna(subset=["dob"])
    if df.empty:
        return pd.DataFrame(columns=["age_band", "product_name", "claims", "avg_claim_amount"])

    df["age"] = today.year - pd.to_datetime(df["dob"]).dt.year
    df["age_band"] = pd.cut(df["age"], bins=AGE_BANDS, labels=AGE_LABELS, right=False)

    out = (
        df.groupby(["age_band", "product_name"], observed=True)
        .agg(
            claims=("claim_amount", "count"),
            customers=("customer_id", "nunique"),
            avg_claim_amount=("claim_amount", "mean"),
        )
        .reset_index()
    )
    out["claims_per_customer"] = safe_divide(out["claims"], out["customers"])
    return out
