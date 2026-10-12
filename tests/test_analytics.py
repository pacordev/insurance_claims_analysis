"""
Tests for the analytics layer.

They run on an in-memory SQLite database with a tiny hand-made dataset where
we know the right answers by heart. Doubles as proof that the queries aren't
tied to Postgres.
"""

from datetime import date

import pandas as pd
import pytest
from sqlalchemy import Engine

from app.analytics import adjudicators, claims_performance, financial, fraud, regional
from app.analytics.common import Filters


def row(df: pd.DataFrame, **match: object) -> pd.Series:
    """grabs the single row matching e.g. product_name='Home'"""
    mask = pd.Series(True, index=df.index)
    for col, val in match.items():
        mask &= df[col] == val
    assert mask.sum() == 1, f"expected exactly one row for {match}"
    return df[mask].iloc[0]


# ---------- claims performance ----------


def test_claims_frequency(engine: Engine) -> None:
    df = claims_performance.claims_frequency(engine)
    assert row(df, product_name="Home")["frequency"] == pytest.approx(3 / 2)
    assert row(df, product_name="Auto")["frequency"] == pytest.approx(1.0)


def test_filters_narrow_results(engine: Engine) -> None:
    df = claims_performance.claims_frequency(engine, Filters(product="Auto"))
    assert list(df["product_name"]) == ["Auto"]


def test_date_filter_applies_to_claims(engine: Engine) -> None:
    df = claims_performance.monthly_trends(engine, Filters(date_from=date(2024, 2, 1)))
    assert df["claims"].sum() == 3  # january claim is gone


def test_average_claim_amount_by_product_and_cause(engine: Engine) -> None:
    by_product = claims_performance.average_claim_amount(engine)
    assert row(by_product, product_name="Home")["avg_claim_amount"] == pytest.approx(2000 / 3)
    by_cause = claims_performance.average_claim_amount(engine, by="cause_of_loss")
    assert row(by_cause, cause_of_loss="Collision")["avg_claim_amount"] == 2000


def test_claim_severity_only_counts_approved(engine: Engine) -> None:
    df = claims_performance.claim_severity(engine)
    assert row(df, product_name="Home")["mean"] == 800
    assert row(df, product_name="Auto")["max"] == 1500


def test_approval_rate_ignores_pending(engine: Engine) -> None:
    by_product = claims_performance.approval_rate(engine)
    assert row(by_product, product_name="Home")["approval_rate"] == 0.5
    by_adj = claims_performance.approval_rate(engine, by="adjudicator")
    # adjudicator 2 has one pending + one closed -> only the closed one counts
    assert row(by_adj, adjudicator_id=2)["approval_rate"] == 1.0


def test_monthly_trends(engine: Engine) -> None:
    df = claims_performance.monthly_trends(engine)
    assert list(df["claims"]) == [1, 2, 1]
    assert list(df["total_claim_amount"]) == [1000, 1000, 2000]
    assert df["period"].iloc[0] == pd.Timestamp("2024-01-01")


# ---------- financial ----------


def test_loss_ratio_by_product(engine: Engine) -> None:
    df = financial.loss_ratio(engine)
    assert row(df, product_name="Home")["loss_ratio"] == pytest.approx(800 / 1500)
    assert row(df, product_name="Auto")["loss_ratio"] == pytest.approx(1500 / 800)


def test_loss_ratio_by_year(engine: Engine) -> None:
    df = financial.loss_ratio(engine, by_year=True)
    assert set(df["year"]) == {2024}
    assert len(df) == 2


def test_product_profitability(engine: Engine) -> None:
    df = financial.product_profitability(engine)
    assert row(df, product_name="Home")["profit"] == 700
    assert row(df, product_name="Auto")["profit"] == -700


def test_payout_trends(engine: Engine) -> None:
    df = financial.payout_trends(engine)
    assert row(df, month=2, payment_method="EFT")["total_paid"] == 400
    assert row(df, month=3, payment_method="EFT")["total_paid"] == 1500


def test_build_loss_ratio_handles_missing_losses_and_zero_premium() -> None:
    premium = pd.DataFrame({"k": ["a", "b"], "premium_total": [100.0, 0.0]})
    losses = pd.DataFrame({"k": ["b"], "approved_total": [50.0]})
    df = financial.build_loss_ratio(premium, losses, ["k"])
    assert row(df, k="a")["loss_ratio"] == 0  # no claims at all
    assert pd.isna(row(df, k="b")["loss_ratio"])  # zero premium -> NaN, no crash


# ---------- fraud ----------


def test_fraud_flag_rate(engine: Engine) -> None:
    df = fraud.fraud_flag_rate(engine)
    assert row(df, product_name="Home")["fraud_rate"] == pytest.approx(1 / 3)
    assert row(df, product_name="Auto")["fraud_rate"] == 1.0


def _amounts_with_one_huge_claim() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "claim_id": range(10),
            "product_name": "Home",
            "claim_amount": [100.0] * 9 + [1000.0],
            "is_flagged_fraud": [False] * 9 + [True],
        }
    )


def test_iqr_outlier_found() -> None:
    df = fraud.mark_outliers(_amounts_with_one_huge_claim(), "iqr")
    assert df["is_outlier"].sum() == 1
    assert df.loc[df["claim_amount"] == 1000, "is_outlier"].item()


def test_zscore_outlier_found() -> None:
    df = fraud.mark_outliers(_amounts_with_one_huge_claim(), "zscore", threshold=2)
    assert df["is_outlier"].sum() == 1


def test_identical_amounts_give_no_outliers() -> None:
    df = _amounts_with_one_huge_claim().assign(claim_amount=100.0)
    assert not fraud.mark_outliers(df, "zscore")["is_outlier"].any()
    assert not fraud.mark_outliers(df, "iqr")["is_outlier"].any()


def test_unknown_outlier_method_raises() -> None:
    with pytest.raises(ValueError):
        fraud.mark_outliers(_amounts_with_one_huge_claim(), "magic")  # type: ignore[arg-type]


def test_anomaly_hit_rate_runs_on_db(engine: Engine) -> None:
    df = fraud.anomaly_hit_rate(engine)
    assert set(df["product_name"]) == {"Home", "Auto"}
    assert (df["both"] <= df["outliers"]).all()


def test_high_risk_segments_respects_min_claims(engine: Engine) -> None:
    assert fraud.high_risk_segments(engine, min_claims=100).empty
    assert not fraud.high_risk_segments(engine, min_claims=1).empty


# ---------- adjudicators ----------


def test_adjudicator_performance(engine: Engine) -> None:
    df = adjudicators.adjudicator_performance(engine)
    ana = row(df, adjudicator_id=1)
    assert ana["total_claims"] == 2
    assert ana["avg_processing_days"] == pytest.approx(7.5)
    assert ana["fraud_flag_rate"] == 0.5
    bob = row(df, adjudicator_id=2)
    assert bob["open_claims"] == 1
    assert bob["avg_processing_days"] == 2  # the pending claim has no decision date, skipped
    assert bob["capacity_used"] == pytest.approx(1 / 5)


def test_performance_by_seniority(engine: Engine) -> None:
    df = adjudicators.performance_by_seniority(engine)
    assert set(df["seniority_level"]) == {"Junior", "Senior"}
    assert row(df, seniority_level="Junior")["approval_rate"] == 0.5


# ---------- regional ----------


def test_regional_analysis(engine: Engine) -> None:
    df = regional.regional_analysis(engine)
    ontario = row(df, province="Ontario")
    assert ontario["claims"] == 3
    assert ontario["loss_ratio"] == pytest.approx(2300 / 1800)
    assert row(df, province="Quebec")["loss_ratio"] == 0


def test_regional_by_city(engine: Engine) -> None:
    df = regional.regional_analysis(engine, level="city")
    assert set(df["city"]) == {"Toronto", "Montreal"}


def test_regional_with_product_filter(engine: Engine) -> None:
    df = regional.regional_analysis(engine, filters=Filters(product="Auto"))
    assert row(df, province="Ontario")["claims"] == 1


def test_customer_segmentation_age_bands(engine: Engine) -> None:
    df = regional.customer_segmentation(engine, today=date(2025, 1, 1))
    assert row(df, age_band="35-44", product_name="Home")["claims"] == 2
    assert row(df, age_band="65+", product_name="Home")["claims"] == 1
