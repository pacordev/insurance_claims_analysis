"""
Risk and fraud metrics: fraud flag rate, outlier detection, and how well
the outlier logic lines up with claims adjudicators actually flagged.
"""

from typing import Literal

import pandas as pd
from sqlalchemy import Engine, func, select

from app.analytics.common import Filters, run_query, safe_divide
from app.analytics.tables import (
    claims,
    claims_with_product,
    claims_with_product_and_customer,
    customers,
    products,
)


def fraud_flag_rate(
    engine: Engine,
    by: Literal["product", "adjudicator"] = "product",
    filters: Filters | None = None,
) -> pd.DataFrame:
    """% of claims flagged as fraud, per product or per adjudicator."""
    group_col = {"product": products.c.product_name, "adjudicator": claims.c.adjudicator_id}[by]
    stmt = select(group_col, claims.c.is_flagged_fraud).select_from(claims_with_product)
    df = run_query(engine, (filters or Filters()).apply(stmt))

    # counting in pandas: summing a boolean column in SQL isn't the same
    # across databases (some have real booleans, some use 0/1)
    key = group_col.name
    grouped = df.groupby(key)["is_flagged_fraud"]
    out = pd.DataFrame({"claims": grouped.count(), "flagged": grouped.sum().astype(int)})
    out["fraud_rate"] = safe_divide(out["flagged"], out["claims"])
    return out.reset_index()


def find_outliers(
    engine: Engine,
    method: Literal["iqr", "zscore"] = "iqr",
    threshold: float | None = None,
    filters: Filters | None = None,
) -> pd.DataFrame:
    """
    Marks claims whose claim_amount is unusually big compared to the other
    claims of the same product. Returns every claim with an `is_outlier` column.

    - iqr:    outlier if above Q3 + threshold * IQR   (default threshold 1.5)
    - zscore: outlier if z-score above threshold       (default threshold 3.0)

    Only the high side counts - a tiny claim isn't suspicious here.
    """
    stmt = select(
        claims.c.claim_id,
        products.c.product_name,
        claims.c.claim_amount,
        claims.c.is_flagged_fraud,
    ).select_from(claims_with_product)
    df = run_query(engine, (filters or Filters()).apply(stmt))
    return mark_outliers(df, method, threshold)


def mark_outliers(
    df: pd.DataFrame,
    method: Literal["iqr", "zscore"] = "iqr",
    threshold: float | None = None,
) -> pd.DataFrame:
    """The actual outlier maths, split out so tests can feed it a plain DataFrame."""
    if method not in ("iqr", "zscore"):
        raise ValueError(f"unknown method '{method}' - use 'iqr' or 'zscore'")

    df = df.copy()
    amounts = df.groupby("product_name")["claim_amount"]

    if method == "iqr":
        k = 1.5 if threshold is None else threshold
        q1 = amounts.transform(lambda s: s.quantile(0.25))
        q3 = amounts.transform(lambda s: s.quantile(0.75))
        df["upper_bound"] = q3 + k * (q3 - q1)
    else:
        z = 3.0 if threshold is None else threshold
        # std of 0 (all claims identical) would blow up - treat as "no spread, no outliers"
        std = amounts.transform("std").replace(0, float("nan"))
        df["upper_bound"] = amounts.transform("mean") + z * std

    df["is_outlier"] = df["claim_amount"] > df["upper_bound"]
    return df


def anomaly_hit_rate(
    engine: Engine,
    method: Literal["iqr", "zscore"] = "iqr",
    threshold: float | None = None,
    filters: Filters | None = None,
) -> pd.DataFrame:
    """
    Sanity check for the detection logic: out of the claims we call outliers,
    how many did an adjudicator really flag as fraud (precision), and out of
    the flagged ones, how many did we catch (recall). One row per product.
    """
    df = find_outliers(engine, method, threshold, filters)
    df["both"] = df["is_outlier"] & df["is_flagged_fraud"]

    out = df.groupby("product_name").agg(
        claims=("claim_id", "count"),
        outliers=("is_outlier", "sum"),
        flagged=("is_flagged_fraud", "sum"),
        both=("both", "sum"),
    )
    out = out.astype(int)
    out["precision"] = safe_divide(out["both"], out["outliers"])
    out["recall"] = safe_divide(out["both"], out["flagged"])
    return out.reset_index()


def high_risk_segments(
    engine: Engine, min_claims: int = 30, filters: Filters | None = None
) -> pd.DataFrame:
    """
    Product x province combos ranked by how much worse than average their claim
    amount and fraud rate are. `min_claims` keeps tiny segments from topping the list.
    """
    stmt = select(
        products.c.product_name,
        customers.c.province,
        claims.c.claim_amount,
        claims.c.is_flagged_fraud,
    ).select_from(claims_with_product_and_customer)
    df = run_query(engine, (filters or Filters()).apply(stmt))
    if df.empty:
        return df

    # compare each segment to its OWN product's average - otherwise Life (big
    # claims everywhere) would top the list just for being Life
    product_avg = df.groupby("product_name").agg(
        product_avg_amount=("claim_amount", "mean"),
        product_fraud_rate=("is_flagged_fraud", "mean"),
    )

    seg = df.groupby(["product_name", "province"]).agg(
        claims=("claim_amount", "count"),
        avg_claim_amount=("claim_amount", "mean"),
        fraud_rate=("is_flagged_fraud", "mean"),
    )
    seg = seg[seg["claims"] >= min_claims].reset_index()
    seg = seg.merge(product_avg, on="product_name")
    seg["amount_vs_avg"] = safe_divide(seg["avg_claim_amount"], seg["product_avg_amount"])
    seg["fraud_vs_avg"] = safe_divide(seg["fraud_rate"], seg["product_fraud_rate"])
    seg = seg.drop(columns=["product_avg_amount", "product_fraud_rate"])
    # simple combined score: how far above average on both measures
    seg["risk_index"] = (seg["amount_vs_avg"] + seg["fraud_vs_avg"]) / 2
    return seg.sort_values("risk_index", ascending=False).reset_index(drop=True)
