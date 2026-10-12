"""
Gathers everything the report needs from the analytics layer, and works out
the headline numbers. No Excel in here on purpose - this part is plain
DataFrames so it's easy to test.
"""

from dataclasses import dataclass

import pandas as pd
from sqlalchemy import Engine

from app.analytics import adjudicators, claims_performance, financial, fraud, regional
from app.analytics.common import Filters

Frames = dict[str, pd.DataFrame]


@dataclass(frozen=True)
class Kpi:
    label: str
    value: float
    kind: str  # int | money | pct | dec1 | dec2 - the writer picks the number format from this


def collect_frames(engine: Engine, filters: Filters | None = None) -> Frames:
    """Runs every analytics function once and keeps the results by name."""
    f = filters or Filters()
    return {
        "frequency": claims_performance.claims_frequency(engine, f),
        "avg_by_product": claims_performance.average_claim_amount(engine, "product", f),
        "avg_by_cause": claims_performance.average_claim_amount(engine, "cause_of_loss", f),
        "severity": claims_performance.claim_severity(engine, f),
        "approval": claims_performance.approval_rate(engine, "product", f),
        "monthly": claims_performance.monthly_trends(engine, f),
        "loss_ratio": financial.loss_ratio(engine, False, f),
        "loss_ratio_year": financial.loss_ratio(engine, True, f),
        "profitability": financial.product_profitability(engine, f),
        "payouts": financial.payout_trends(engine, f),
        "fraud_rate": fraud.fraud_flag_rate(engine, "product", f),
        "anomalies": fraud.anomaly_hit_rate(engine, "iqr", None, f),
        "high_risk": fraud.high_risk_segments(engine, 30, f),
        "adjudicators": adjudicators.adjudicator_performance(engine, f),
        "seniority": adjudicators.performance_by_seniority(engine, f),
        "province": regional.regional_analysis(engine, "province", f),
        "city": regional.regional_analysis(engine, "city", f),
        "segmentation": regional.customer_segmentation(engine, None, f),
    }


def _ratio(num: float, den: float) -> float:
    return num / den if den else float("nan")


def compute_kpis(frames: Frames) -> list[Kpi]:
    """The ten headline numbers for the Overview sheet, built from the frames above."""
    freq, avg, lr = frames["frequency"], frames["avg_by_product"], frames["loss_ratio"]
    appr, fr, adj = frames["approval"], frames["fraud_rate"], frames["adjudicators"]

    claims = float(freq["claims"].sum())
    policies = float(freq["policies"].sum())
    premium = float(lr["premium_total"].sum())
    approved_total = float(lr["approved_total"].sum())

    # average processing time weighted by how many decisions each adjudicator made
    timed = adj.dropna(subset=["avg_processing_days"])
    processing = _ratio(
        float((timed["avg_processing_days"] * timed["decided"]).sum()), float(timed["decided"].sum())
    )

    return [
        Kpi("Claims", claims, "int"),
        Kpi("Policies", policies, "int"),
        Kpi("Claims per policy", _ratio(claims, policies), "dec2"),
        Kpi("Total claimed", float(avg["total_claim_amount"].sum()), "money"),
        Kpi("Total approved", approved_total, "money"),
        Kpi("Premium", premium, "money"),
        Kpi("Loss ratio", _ratio(approved_total, premium), "pct"),
        Kpi("Approval rate", _ratio(float(appr["approved"].sum()), float(appr["decided"].sum())), "pct"),
        Kpi("Fraud flag rate", _ratio(float(fr["flagged"].sum()), float(fr["claims"].sum())), "pct"),
        Kpi("Avg processing days", processing, "dec1"),
    ]


def build_highlights(frames: Frames) -> list[str]:
    """A few plain-English takeaways pulled from the numbers (skips any that have no data)."""
    out: list[str] = []

    lr = frames["loss_ratio"].dropna(subset=["loss_ratio"])
    if not lr.empty:
        worst, best = lr.loc[lr["loss_ratio"].idxmax()], lr.loc[lr["loss_ratio"].idxmin()]
        out.append(f"Highest loss ratio: {worst['product_name']} at {worst['loss_ratio']:.1%}.")
        out.append(f"Lowest loss ratio: {best['product_name']} at {best['loss_ratio']:.1%}.")

    monthly = frames["monthly"]
    if not monthly.empty:
        peak = monthly.loc[monthly["claims"].idxmax()]
        out.append(f"Busiest month: {peak['period']:%B %Y} with {int(peak['claims']):,} claims.")

    prov = frames["province"]
    if not prov.empty:
        top = prov.iloc[0]  # already sorted by claims, biggest first
        out.append(f"Most claims: {top['province']} ({int(top['claims']):,}).")

    risk = frames["high_risk"]
    if not risk.empty:
        top = risk.iloc[0]
        out.append(
            f"Highest-risk segment: {top['product_name']} in {top['province']} "
            f"(fraud rate {top['fraud_rate']:.1%}, {top['fraud_vs_avg']:.1f}x that product's average)."
        )

    an = frames["anomalies"]
    if not an.empty and an["outliers"].sum() > 0:
        precision = an["both"].sum() / an["outliers"].sum()
        recall = an["both"].sum() / an["flagged"].sum() if an["flagged"].sum() else float("nan")
        out.append(
            f"Outlier check (IQR) vs adjudicator fraud flags: {precision:.0%} of outliers were "
            f"flagged, and it caught {recall:.0%} of flagged claims."
        )
    return out


def scope_text(filters: Filters) -> str:
    """One line describing what the report covers, shown on the Overview and Notes sheets."""
    parts = []
    if filters.date_from or filters.date_to:
        parts.append(f"claims dated {filters.date_from or 'start'} to {filters.date_to or 'today'}")
    if filters.product:
        parts.append(f"product: {filters.product}")
    return "Scope: " + (", ".join(parts) if parts else "all claims, all products")
