"""
Financial metrics: loss ratio, product profitability, payout trends.

Simplification worth knowing about: "premium" here is the full premium_amount
of each policy (written premium), not a pro-rated earned premium. Good enough
for comparing products/regions, not an actuarial number.
"""

import pandas as pd
from sqlalchemy import Engine, extract, func, select

from app.analytics.common import Filters, run_query, safe_divide
from app.analytics.tables import (
    APPROVED_STATUSES,
    claims,
    claims_with_product,
    payments,
    policies,
    policies_with_product,
    products,
)


def build_loss_ratio(premium: pd.DataFrame, losses: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """
    Merges a premium table and a losses table on `keys` and adds loss_ratio.
    Shared with the regional module. Kept separate from the queries so it's
    easy to unit test without a database.
    """
    df = premium.merge(losses, on=keys, how="left")
    df["approved_total"] = df["approved_total"].fillna(0.0)
    df["loss_ratio"] = safe_divide(df["approved_total"], df["premium_total"])
    return df


def loss_ratio(
    engine: Engine, by_year: bool = False, filters: Filters | None = None
) -> pd.DataFrame:
    """
    Approved claim amount / premium, per product (optionally per year too).
    Premium is bucketed by policy start year, claims by date of loss.
    """
    f = filters or Filters()

    premium_keys = [products.c.product_name]
    loss_keys = [products.c.product_name]
    if by_year:
        premium_keys.append(extract("year", policies.c.effective_date).label("year"))
        loss_keys.append(extract("year", claims.c.claim_date).label("year"))

    premium_stmt = f.apply(
        select(*premium_keys, func.sum(policies.c.premium_amount).label("premium_total"))
        .select_from(policies_with_product)
        .group_by(*premium_keys),
        dates=False,
    )
    loss_stmt = f.apply(
        select(*loss_keys, func.sum(claims.c.approved_amount).label("approved_total"))
        .select_from(claims_with_product)
        .where(claims.c.status.in_(APPROVED_STATUSES))
        .group_by(*loss_keys)
    )

    keys = ["product_name"] + (["year"] if by_year else [])
    premium, losses = run_query(engine, premium_stmt), run_query(engine, loss_stmt)
    if by_year:
        premium["year"] = premium["year"].astype(int)
        losses["year"] = losses["year"].astype(int)
    return build_loss_ratio(premium, losses, keys).sort_values(keys).reset_index(drop=True)


def product_profitability(engine: Engine, filters: Filters | None = None) -> pd.DataFrame:
    """Premium collected vs actual money paid out (from payments), per product."""
    f = filters or Filters()

    premium_stmt = f.apply(
        select(products.c.product_name, func.sum(policies.c.premium_amount).label("premium_total"))
        .select_from(policies_with_product)
        .group_by(products.c.product_name),
        dates=False,
    )
    paid_stmt = f.apply(
        select(products.c.product_name, func.sum(payments.c.payment_amount).label("paid_total"))
        .select_from(
            claims_with_product.join(payments, payments.c.claim_id == claims.c.claim_id)
        )
        .group_by(products.c.product_name)
    )

    df = run_query(engine, premium_stmt).merge(
        run_query(engine, paid_stmt), on="product_name", how="left"
    )
    df["paid_total"] = df["paid_total"].fillna(0.0)
    df["profit"] = df["premium_total"] - df["paid_total"]
    df["margin"] = safe_divide(df["profit"], df["premium_total"])
    return df.sort_values("product_name").reset_index(drop=True)


def payout_trends(engine: Engine, filters: Filters | None = None) -> pd.DataFrame:
    """Total paid per month, split by payment method."""
    year = extract("year", payments.c.payment_date).label("year")
    month = extract("month", payments.c.payment_date).label("month")
    stmt = (
        select(
            year,
            month,
            payments.c.payment_method,
            func.count(payments.c.payment_id).label("payments"),
            func.sum(payments.c.payment_amount).label("total_paid"),
        )
        .select_from(claims_with_product.join(payments, payments.c.claim_id == claims.c.claim_id))
        .group_by(year, month, payments.c.payment_method)
        .order_by(year, month, payments.c.payment_method)
    )
    df = run_query(engine, (filters or Filters()).apply(stmt))
    df[["year", "month"]] = df[["year", "month"]].astype(int)
    return df
