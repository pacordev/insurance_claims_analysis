"""
end-to-end test against a real Postgres - skipped automatically (see
conftest.db_engine) if `docker compose up -d` hasn't been run locally.

builds a tiny one-row-per-table CSV set, runs the real pipeline, and checks
the rows actually landed and the sequences got bumped correctly.
"""

from pathlib import Path

import pandas as pd
import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from data.pipeline.ingest import run_pipeline, truncate_all


def _write_minimal_dataset(data_dir: Path) -> None:
    now = "2026-01-01 00:00:00"

    pd.DataFrame([{"line_of_business": "Auto"}]).to_csv(data_dir / "ref_line_of_business.csv", index=False)
    pd.DataFrame([
        {"product_name": "Auto", "cause_of_loss": "Collision"},
    ]).to_csv(data_dir / "ref_cause_of_loss.csv", index=False)

    pd.DataFrame([{
        "customer_id": 1, "first_name": "Test", "last_name": "Customer", "dob": "1990-01-01",
        "email": "test.customer@example.com", "phone": "555-0100", "address_line1": "1 Main St",
        "city": "Testville", "province": "ON", "postal_code": "A1A1A1", "created_at": now,
    }]).to_csv(data_dir / "customers.csv", index=False)

    pd.DataFrame([{
        "product_id": 1, "product_name": "Auto", "line_of_business": "Auto",
        "description": "Auto insurance", "base_premium": 700.0, "created_at": now,
    }]).to_csv(data_dir / "products.csv", index=False)

    pd.DataFrame([{
        "adjudicator_id": 1, "first_name": "Jo", "last_name": "Adjudicator",
        "email": "jo.adjudicator@example.com", "phone": "555-0200", "department": "Auto",
        "seniority_level": "Senior", "certification": "CIP", "capacity_score": 50, "created_at": now,
    }]).to_csv(data_dir / "adjudicators.csv", index=False)

    pd.DataFrame([{
        "policy_id": 1, "policy_number": "POL-TEST-000001", "customer_id": 1, "product_id": 1,
        "effective_date": "2025-01-01", "expiry_date": "2026-01-01", "status": "Active",
        "premium_amount": 700.0, "coverage_limit": 20000.0, "deductible": 500.0,
        "created_at": now, "updated_at": now,
    }]).to_csv(data_dir / "policies.csv", index=False)

    pd.DataFrame([{
        "claim_id": 1, "claim_number": "CLM-TEST-000001", "policy_id": 1, "adjudicator_id": 1,
        "claim_date": "2025-06-01", "reported_date": "2025-06-02", "decision_date": "2025-06-10",
        "cause_of_loss": "Collision", "claim_type": "Auto", "description": "test claim",
        "status": "Approved", "claim_amount": 1000.0, "approved_amount": 800.0,
        "is_flagged_fraud": False, "fraud_score": 5.0, "created_at": now, "updated_at": now,
    }]).to_csv(data_dir / "claims.csv", index=False)

    pd.DataFrame([{
        "payment_id": 1, "claim_id": 1, "payment_date": "2025-06-15", "payment_amount": 800.0,
        "payment_method": "EFT", "payee": "Repair Shop", "created_at": now,
    }]).to_csv(data_dir / "payments.csv", index=False)


def test_full_pipeline_loads_and_bumps_sequences(db_engine, tmp_path):
    truncate_all(db_engine)
    _write_minimal_dataset(tmp_path)

    row_counts = run_pipeline(tmp_path, db_engine)

    assert row_counts == {
        "ref_line_of_business": 1, "products": 1, "ref_cause_of_loss": 1,
        "customers": 1, "adjudicators": 1, "policies": 1, "claims": 1, "payments": 1,
    }

    with db_engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM claims")).scalar() == 1
        # sequence should be past the loaded id, so a fresh insert doesn't collide
        next_id = conn.execute(text("SELECT nextval(pg_get_serial_sequence('claims', 'claim_id'))")).scalar()
        assert next_id > 1


def test_user_style_csvs_load_without_audit_columns_and_in_any_column_order(db_engine, tmp_path):
    """what a newcomer's files look like: no created_at/updated_at, columns in a random order"""
    _write_minimal_dataset(tmp_path)
    for csv_file in tmp_path.glob("*.csv"):
        df = pd.read_csv(csv_file).drop(columns=["created_at", "updated_at"], errors="ignore")
        df[list(reversed(df.columns))].to_csv(csv_file, index=False)

    truncate_all(db_engine)
    row_counts = run_pipeline(tmp_path, db_engine)
    assert row_counts["claims"] == 1

    with db_engine.connect() as conn:
        assert conn.execute(text("SELECT created_at IS NOT NULL FROM customers")).scalar()
        # sequence must be past the loaded id even though the pk column was last in the file
        next_id = conn.execute(text("SELECT nextval(pg_get_serial_sequence('policies', 'policy_id'))")).scalar()
        assert next_id > 1


def test_empty_templates_load_cleanly(db_engine):
    truncate_all(db_engine)
    templates = Path(__file__).resolve().parent.parent / "data" / "templates"
    counts = run_pipeline(templates, db_engine)
    assert set(counts.values()) == {0}


def test_new_product_is_just_new_lookup_rows(db_engine, tmp_path):
    """the point of the lookup tables: a 'Pet' product needs no schema or code change"""
    _write_minimal_dataset(tmp_path)
    pd.DataFrame([{"line_of_business": "Auto"}, {"line_of_business": "Pet"}]).to_csv(
        tmp_path / "ref_line_of_business.csv", index=False)
    pd.DataFrame([
        {"product_name": "Auto", "cause_of_loss": "Collision"},
        {"product_name": "Pet", "cause_of_loss": "VetBills"},
    ]).to_csv(tmp_path / "ref_cause_of_loss.csv", index=False)
    products = pd.read_csv(tmp_path / "products.csv")
    products.loc[len(products)] = [2, "Pet", "Pet", "Pet insurance", 300.0, "2026-01-01 00:00:00"]
    products.to_csv(tmp_path / "products.csv", index=False)
    policies = pd.read_csv(tmp_path / "policies.csv")
    policies.loc[0, "product_id"] = 2  # the policy is a Pet policy now...
    policies.to_csv(tmp_path / "policies.csv", index=False)
    claims = pd.read_csv(tmp_path / "claims.csv")
    claims.loc[0, ["claim_type", "cause_of_loss"]] = ["Pet", "VetBills"]  # ...and so is its claim
    claims.to_csv(tmp_path / "claims.csv", index=False)

    truncate_all(db_engine)
    counts = run_pipeline(tmp_path, db_engine)
    assert counts["products"] == 2 and counts["ref_cause_of_loss"] == 2
    with db_engine.connect() as conn:
        assert conn.execute(text("SELECT claim_type FROM claims")).scalar() == "Pet"


def test_database_rejects_cause_that_does_not_fit_product(db_engine, tmp_path):
    """the database itself (not just our Python check) enforces cause-fits-product"""
    import pytest
    from sqlalchemy.exc import IntegrityError

    _write_minimal_dataset(tmp_path)
    truncate_all(db_engine)
    run_pipeline(tmp_path, db_engine)
    with pytest.raises(IntegrityError):
        with db_engine.begin() as conn:
            conn.execute(text("UPDATE claims SET cause_of_loss = 'Fire' WHERE claim_id = 1"))


# ---------- cross-table claim rules ----------


def _dataset_with_two_products(data_dir: Path) -> None:
    """minimal dataset (one Auto policy + claim) plus a second product, Home, that nothing uses yet"""
    _write_minimal_dataset(data_dir)
    lines = pd.read_csv(data_dir / "ref_line_of_business.csv")
    lines.loc[len(lines)] = ["Property"]
    lines.to_csv(data_dir / "ref_line_of_business.csv", index=False)
    causes = pd.read_csv(data_dir / "ref_cause_of_loss.csv")
    causes.loc[len(causes)] = ["Home", "Fire"]
    causes.to_csv(data_dir / "ref_cause_of_loss.csv", index=False)
    products = pd.read_csv(data_dir / "products.csv")
    products.loc[len(products)] = [2, "Home", "Property", "Home insurance", 900.0, "2026-01-01 00:00:00"]
    products.to_csv(data_dir / "products.csv", index=False)


def _loaded_db(db_engine, tmp_path):
    _dataset_with_two_products(tmp_path)
    truncate_all(db_engine)
    run_pipeline(tmp_path, db_engine)


def test_loader_rejects_claim_type_that_does_not_match_policy(db_engine, tmp_path):
    _dataset_with_two_products(tmp_path)
    claims = pd.read_csv(tmp_path / "claims.csv")
    claims.loc[0, ["claim_type", "cause_of_loss"]] = ["Home", "Fire"]  # valid pair, but the policy is Auto
    claims.to_csv(tmp_path / "claims.csv", index=False)
    truncate_all(db_engine)
    with pytest.raises(ValueError, match="claim_type doesn't match"):
        run_pipeline(tmp_path, db_engine)


def test_loader_rejects_claim_outside_policy_term(db_engine, tmp_path):
    _dataset_with_two_products(tmp_path)
    claims = pd.read_csv(tmp_path / "claims.csv")
    claims.loc[0, ["claim_date", "reported_date", "decision_date"]] = ["2027-03-01", "2027-03-02", "2027-03-10"]
    claims.to_csv(tmp_path / "claims.csv", index=False)
    truncate_all(db_engine)
    with pytest.raises(ValueError, match="outside their policy's term"):
        run_pipeline(tmp_path, db_engine)


def test_database_rejects_mismatched_claim_type_even_without_the_loader(db_engine, tmp_path):
    _loaded_db(db_engine, tmp_path)
    with pytest.raises(IntegrityError, match="policy 1 is a Auto policy"):
        with db_engine.begin() as conn:
            conn.execute(text("UPDATE claims SET claim_type = 'Home', cause_of_loss = 'Fire' WHERE claim_id = 1"))


def test_database_rejects_claim_dated_outside_policy_term(db_engine, tmp_path):
    _loaded_db(db_engine, tmp_path)
    with pytest.raises(IntegrityError, match="runs 2025-01-01 to 2026-01-01"):
        with db_engine.begin() as conn:
            conn.execute(text("UPDATE claims SET claim_date = '2024-12-31' WHERE claim_id = 1"))


def test_database_rejects_new_claim_that_breaks_the_rules(db_engine, tmp_path):
    _loaded_db(db_engine, tmp_path)
    with pytest.raises(IntegrityError, match="policy 1 is a Auto policy"):
        with db_engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO claims (claim_number, policy_id, claim_date, reported_date, claim_type, cause_of_loss, status) "
                "VALUES ('CLM-X', 1, '2025-06-01', '2025-06-02', 'Home', 'Fire', 'Pending')"
            ))


def test_database_blocks_policy_change_that_would_strand_a_claim(db_engine, tmp_path):
    _loaded_db(db_engine, tmp_path)
    with pytest.raises(IntegrityError, match="can't change like that"):
        with db_engine.begin() as conn:  # claim is dated 2025-06-01, this policy would end before it
            conn.execute(text("UPDATE policies SET expiry_date = '2025-03-01' WHERE policy_id = 1"))
    with pytest.raises(IntegrityError, match="can't change like that"):
        with db_engine.begin() as conn:  # and the claim is Auto, so the policy can't become Home
            conn.execute(text("UPDATE policies SET product_id = 2 WHERE policy_id = 1"))


def test_harmless_policy_and_claim_updates_still_work(db_engine, tmp_path):
    _loaded_db(db_engine, tmp_path)
    with db_engine.begin() as conn:
        conn.execute(text("UPDATE policies SET expiry_date = '2026-06-01' WHERE policy_id = 1"))  # longer term is fine
        conn.execute(text("UPDATE claims SET status = 'Closed' WHERE claim_id = 1"))  # unrelated column
    with db_engine.connect() as conn:
        assert conn.execute(text("SELECT status FROM claims WHERE claim_id = 1")).scalar() == "Closed"
