"""
Claims performance metrics: frequency, average amount, severity,
approval rate, monthly trends.
"""

from typing import Literal

import pandas as pd
from sqlalchemy import Engine, case, extract, func, select

from app.analytics.common import Filters, run_query, safe_divide
from app.analytics.tables import (
    APPROVED_STATUSES,
    DENIED_STATUSES,
    claims,
    claims_with_product,
    policies_with_product,
    products,
)


def claims_frequency(engine: Engine, filters: Filters | None = None) -> pd.DataFrame:
    """Claims per policy, by product. (date filters only touch the claims side)"""
    f = filters or Filters()

    claim_counts = f.apply(
        select(products.c.product_name, func.count(claims.c.claim_id).label("claims"))
        .select_from(claims_with_product)
        .group_by(products.c.product_name)
    )
    policy_counts = f.apply(
        select(products.c.product_name, func.count().label("policies"))
        .select_from(policies_with_product)
        .group_by(products.c.product_name),
        dates=False,
    )

    # two separate queries then merge - joining claims and policies in one go
    # would multiply rows and mess up the counts
    df = run_query(engine, policy_counts).merge(
        run_query(engine, claim_counts), on="product_name", how="left"
    )
    df["claims"] = df["claims"].fillna(0).astype(int)
    df["frequency"] = safe_divide(df["claims"], df["policies"])
    return df.sort_values("product_name").reset_index(drop=True)


def average_claim_amount(
    engine: Engine,
    by: Literal["product", "cause_of_loss"] = "product",
    filters: Filters | None = None,
) -> pd.DataFrame:
    """Mean (and total) claim amount, grouped by product or by cause of loss."""
    group_col = {"product": products.c.product_name, "cause_of_loss": claims.c.cause_of_loss}[by]
    stmt = (
        select(
            group_col,
            func.count(claims.c.claim_id).label("claims"),
            func.avg(claims.c.claim_amount).label("avg_claim_amount"),
            func.sum(claims.c.claim_amount).label("total_claim_amount"),
        )
        .select_from(claims_with_product)
        .group_by(group_col)
        .order_by(group_col)
    )
    return run_query(engine, (filters or Filters()).apply(stmt))


def claim_severity(engine: Engine, filters: Filters | None = None) -> pd.DataFrame:
    """
    Severity = approved amount per paid-out claim, by product. Median and p90
    are done in pandas since percentile functions differ between databases.
    """
    stmt = (
        select(products.c.product_name, claims.c.approved_amount)
        .select_from(claims_with_product)
        .where(claims.c.status.in_(APPROVED_STATUSES))
        .where(claims.c.approved_amount.is_not(None))
    )
    df = run_query(engine, (filters or Filters()).apply(stmt))
    if df.empty:
        return pd.DataFrame(columns=["product_name", "claims", "mean", "median", "p90", "max"])

    grouped = df.groupby("product_name")["approved_amount"]
    out = pd.DataFrame(
        {
            "claims": grouped.count(),
            "mean": grouped.mean(),
            "median": grouped.median(),
            "p90": grouped.quantile(0.9),
            "max": grouped.max(),
        }
    )
    return out.reset_index()


def approval_rate(
    engine: Engine,
    by: Literal["product", "adjudicator"] = "product",
    filters: Filters | None = None,
) -> pd.DataFrame:
    """% approved vs denied among decided claims (pending ones don't count)."""
    is_approved = case((claims.c.status.in_(APPROVED_STATUSES), 1), else_=0)
    is_denied = case((claims.c.status.in_(DENIED_STATUSES), 1), else_=0)

    # always join products so the product filter works for both groupings
    group_col = {"product": products.c.product_name, "adjudicator": claims.c.adjudicator_id}[by]
    source = claims_with_product

    stmt = (
        select(
            group_col,
            func.sum(is_approved).label("approved"),
            func.sum(is_denied).label("denied"),
        )
        .select_from(source)
        .group_by(group_col)
        .order_by(group_col)
    )
    df = run_query(engine, (filters or Filters()).apply(stmt))
    df["decided"] = df["approved"] + df["denied"]
    df["approval_rate"] = safe_divide(df["approved"], df["decided"])
    return df


def monthly_trends(engine: Engine, filters: Filters | None = None) -> pd.DataFrame:
    """Claim count and total claim amount per month (by date of loss)."""
    year = extract("year", claims.c.claim_date).label("year")
    month = extract("month", claims.c.claim_date).label("month")
    stmt = (
        select(
            year,
            month,
            func.count(claims.c.claim_id).label("claims"),
            func.sum(claims.c.claim_amount).label("total_claim_amount"),
        )
        .select_from(claims_with_product)
        .group_by(year, month)
        .order_by(year, month)
    )
    df = run_query(engine, (filters or Filters()).apply(stmt))
    df[["year", "month"]] = df[["year", "month"]].astype(int)
    # handy single column for charts / x-axes
    df["period"] = pd.to_datetime(df[["year", "month"]].assign(day=1))
    return df
