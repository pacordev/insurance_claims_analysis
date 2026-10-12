"""
Adjudicator metrics: real workload (counted from claims), processing time,
approval rate and fraud catch rate, all in one table per adjudicator.
"""

import pandas as pd
from sqlalchemy import Engine, select

from app.analytics.common import Filters, run_query, safe_divide
from app.analytics.tables import (
    APPROVED_STATUSES,
    DENIED_STATUSES,
    adjudicators,
    claims,
    claims_with_product,
)


def adjudicator_performance(engine: Engine, filters: Filters | None = None) -> pd.DataFrame:
    """
    One row per adjudicator. Workload is a live count of their claims, NOT the
    static capacity_score column (that's just a baseline set at hire time).
    Processing days = decision_date - reported_date, only for decided claims.
    """
    stmt = select(
        claims.c.claim_id,
        claims.c.adjudicator_id,
        claims.c.status,
        claims.c.is_flagged_fraud,
        claims.c.reported_date,
        claims.c.decision_date,
    ).select_from(claims_with_product)
    df = run_query(engine, (filters or Filters()).apply(stmt))
    # unassigned claims have no adjudicator - nothing to report on them here
    df = df.dropna(subset=["adjudicator_id"])

    # date subtraction is done here, not in SQL, because every database does it differently
    df["processing_days"] = (
        pd.to_datetime(df["decision_date"]) - pd.to_datetime(df["reported_date"])
    ).dt.days
    df["is_approved"] = df["status"].isin(APPROVED_STATUSES)
    df["is_denied"] = df["status"].isin(DENIED_STATUSES)
    df["is_open"] = df["status"] == "Pending"

    stats = df.groupby("adjudicator_id").agg(
        total_claims=("claim_id", "count"),
        open_claims=("is_open", "sum"),
        approved=("is_approved", "sum"),
        denied=("is_denied", "sum"),
        flagged_fraud=("is_flagged_fraud", "sum"),
        avg_processing_days=("processing_days", "mean"),
    )
    stats["decided"] = stats["approved"] + stats["denied"]
    stats["approval_rate"] = safe_divide(stats["approved"], stats["decided"])
    stats["fraud_flag_rate"] = safe_divide(stats["flagged_fraud"], stats["total_claims"])

    # attach who they are (name, seniority, capacity) from the adjudicators table
    people = run_query(
        engine,
        select(
            adjudicators.c.adjudicator_id,
            adjudicators.c.first_name,
            adjudicators.c.last_name,
            adjudicators.c.department,
            adjudicators.c.seniority_level,
            adjudicators.c.capacity_score,
        ),
    )
    out = people.merge(stats.reset_index(), on="adjudicator_id", how="left")

    count_cols = ["total_claims", "open_claims", "approved", "denied", "flagged_fraud", "decided"]
    out[count_cols] = out[count_cols].fillna(0).astype(int)
    # how much of their baseline capacity they're actually using
    out["capacity_used"] = safe_divide(out["open_claims"], out["capacity_score"])
    return out.sort_values("adjudicator_id").reset_index(drop=True)


def performance_by_seniority(engine: Engine, filters: Filters | None = None) -> pd.DataFrame:
    """Same metrics rolled up by seniority level (Junior / Senior / Lead)."""
    perf = adjudicator_performance(engine, filters)
    grouped = perf.groupby("seniority_level")

    out = grouped.agg(
        adjudicators=("adjudicator_id", "count"),
        total_claims=("total_claims", "sum"),
        approved=("approved", "sum"),
        denied=("denied", "sum"),
        flagged_fraud=("flagged_fraud", "sum"),
    )
    out["approval_rate"] = safe_divide(out["approved"], out["approved"] + out["denied"])
    out["fraud_flag_rate"] = safe_divide(out["flagged_fraud"], out["total_claims"])
    out["claims_per_adjudicator"] = safe_divide(out["total_claims"], out["adjudicators"])

    # weighted by claims, so an adjudicator with 10 claims doesn't count like one with 1000
    weighted = perf.dropna(subset=["avg_processing_days"]).assign(
        w=lambda d: d["avg_processing_days"] * d["decided"]
    )
    sums = weighted.groupby("seniority_level")[["w", "decided"]].sum()
    out["avg_processing_days"] = safe_divide(sums["w"], sums["decided"])
    return out.reset_index()
