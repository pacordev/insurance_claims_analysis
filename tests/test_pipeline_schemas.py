"""
tests for the pandera schemas - all pure pandas, no database needed.
just building tiny good/bad dataframes and checking the schema
accepts/rejects them the way the v2 SQL CHECK constraints would.
"""

from datetime import datetime

import pandas as pd
import pandera.errors
import pytest

from data.pipeline.schemas import CLAIMS_SCHEMA, CUSTOMERS_SCHEMA, PAYMENTS_SCHEMA, POLICIES_SCHEMA


def _base_claim_row(**overrides) -> dict:
    row = {
        "claim_id": 1,
        "claim_number": "CLM-000001",
        "policy_id": 1,
        "adjudicator_id": 1,
        "claim_date": datetime(2025, 1, 1),
        "reported_date": datetime(2025, 1, 3),
        "decision_date": datetime(2025, 1, 10),
        "cause_of_loss": "Collision",
        "claim_type": "Auto",
        "description": "Auto claim due to Collision",
        "status": "Approved",
        "claim_amount": 1000.0,
        "approved_amount": 800.0,
        "is_flagged_fraud": False,
        "fraud_score": 12.5,
        "created_at": datetime(2025, 1, 1),
        "updated_at": datetime(2025, 1, 10),
    }
    row.update(overrides)
    return row


def test_valid_claim_passes():
    df = pd.DataFrame([_base_claim_row()])
    validated = CLAIMS_SCHEMA.validate(df)
    assert len(validated) == 1


def test_claim_rejects_approved_amount_over_claim_amount():
    df = pd.DataFrame([_base_claim_row(approved_amount=5000.0)])
    with pytest.raises(pandera.errors.SchemaError):
        CLAIMS_SCHEMA.validate(df)


def test_claim_rejects_decision_before_reported():
    df = pd.DataFrame([_base_claim_row(decision_date=datetime(2024, 12, 1))])
    with pytest.raises(pandera.errors.SchemaError):
        CLAIMS_SCHEMA.validate(df)


def test_claim_rejects_reported_before_loss():
    df = pd.DataFrame([_base_claim_row(reported_date=datetime(2024, 12, 1))])
    with pytest.raises(pandera.errors.SchemaError):
        CLAIMS_SCHEMA.validate(df)


def test_claim_rejects_unknown_status():
    df = pd.DataFrame([_base_claim_row(status="Cancelled")])  # not a valid claim status
    with pytest.raises(pandera.errors.SchemaError):
        CLAIMS_SCHEMA.validate(df)


def test_claim_allows_pending_with_no_decision_yet():
    df = pd.DataFrame([_base_claim_row(
        status="Pending", decision_date=pd.NaT, approved_amount=None, adjudicator_id=None,
    )])
    validated = CLAIMS_SCHEMA.validate(df)
    assert pd.isna(validated.loc[0, "decision_date"])


def test_customers_rejects_duplicate_email():
    df = pd.DataFrame([
        {"customer_id": 1, "first_name": "A", "last_name": "B", "dob": datetime(1990, 1, 1),
         "email": "a@b.com", "phone": "555", "address_line1": "x", "city": "x", "province": "x",
         "postal_code": "x", "created_at": datetime.now()},
        {"customer_id": 2, "first_name": "C", "last_name": "D", "dob": datetime(1991, 1, 1),
         "email": "a@b.com", "phone": "555", "address_line1": "x", "city": "x", "province": "x",
         "postal_code": "x", "created_at": datetime.now()},
    ])
    with pytest.raises(pandera.errors.SchemaError):
        CUSTOMERS_SCHEMA.validate(df)


def test_policies_rejects_expiry_before_effective():
    df = pd.DataFrame([{
        "policy_id": 1, "policy_number": "POL-000001", "customer_id": 1, "product_id": 1,
        "effective_date": datetime(2025, 6, 1), "expiry_date": datetime(2025, 1, 1),
        "status": "Active", "premium_amount": 700.0, "coverage_limit": 20000.0, "deductible": 500.0,
        "created_at": datetime.now(), "updated_at": datetime.now(),
    }])
    with pytest.raises(pandera.errors.SchemaError):
        POLICIES_SCHEMA.validate(df)


def test_payments_rejects_unknown_payee():
    df = pd.DataFrame([{
        "payment_id": 1, "claim_id": 1, "payment_date": datetime(2025, 1, 15),
        "payment_amount": 500.0, "payment_method": "EFT", "payee": "Loan Shark",
        "created_at": datetime.now(),
    }])
    with pytest.raises(pandera.errors.SchemaError):
        PAYMENTS_SCHEMA.validate(df)
