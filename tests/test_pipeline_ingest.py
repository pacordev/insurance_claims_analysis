"""
tests for the transform + FK-check logic in data/pipeline/ingest.py.
still no database involved here - check_foreign_keys works purely off
in-memory dataframes.
"""

import pandas as pd
import pytest

from data.pipeline.ingest import check_foreign_keys, clean_dataframe


def test_clean_dataframe_strips_whitespace_and_lowercases_email():
    df = pd.DataFrame({
        "first_name": ["  Alice  "],
        "email": ["  ALICE@Example.com "],
        "amount": [12.5],
    })
    cleaned = clean_dataframe(df)
    assert cleaned.loc[0, "first_name"] == "Alice"
    assert cleaned.loc[0, "email"] == "alice@example.com"
    assert cleaned.loc[0, "amount"] == 12.5  # untouched, not a text column


# parent tables a claims row can point at (claim_type -> products, cause -> ref_cause_of_loss)
CLAIM_PARENTS = {
    "products": pd.DataFrame({"product_name": ["Auto"]}),
    "ref_cause_of_loss": pd.DataFrame({"product_name": ["Auto"], "cause_of_loss": ["Collision"]}),
}


def test_check_foreign_keys_passes_when_all_ids_exist():
    policies = pd.DataFrame({"policy_id": [1, 2, 3]})
    adjudicators = pd.DataFrame({"adjudicator_id": [10, 20]})
    claims = pd.DataFrame({
        "policy_id": [1, 2, 2], "adjudicator_id": [10, 20, 10],
        "claim_type": ["Auto"] * 3, "cause_of_loss": ["Collision"] * 3,
    })
    loaded = {"policies": policies, "adjudicators": adjudicators, **CLAIM_PARENTS}
    check_foreign_keys("claims", claims, loaded)  # should not raise


def test_check_foreign_keys_raises_on_orphaned_row():
    policies = pd.DataFrame({"policy_id": [1, 2, 3]})
    claims = pd.DataFrame({"policy_id": [1, 99]})  # 99 doesn't exist
    with pytest.raises(ValueError, match="policy_id"):
        check_foreign_keys("claims", claims, {"policies": policies})


def test_check_foreign_keys_ignores_nulls_on_nullable_fk():
    policies = pd.DataFrame({"policy_id": [1, 2]})
    adjudicators = pd.DataFrame({"adjudicator_id": [10, 20]})
    claims = pd.DataFrame({
        "policy_id": [1, 2], "adjudicator_id": [10, None],
        "claim_type": ["Auto", "Auto"], "cause_of_loss": ["Collision", "Collision"],
    })
    check_foreign_keys("claims", claims, {"policies": policies, "adjudicators": adjudicators, **CLAIM_PARENTS})


# ---------- bringing your own data: optional audit columns, any column order, templates ----------

from pathlib import Path  # noqa: E402

from data.pipeline.ingest import fill_audit_columns, load_csv, validate_table  # noqa: E402
from data.pipeline.schemas import AUDIT_COLUMNS, LOAD_ORDER, TABLES  # noqa: E402

TEMPLATES = Path(__file__).resolve().parent.parent / "data" / "templates"


def test_fill_audit_columns_adds_missing_ones():
    df = pd.DataFrame({"policy_id": [1]})
    out = fill_audit_columns("policies", df)
    assert {"created_at", "updated_at"} <= set(out.columns)
    assert out["created_at"].notna().all()


def test_fill_audit_columns_keeps_values_and_fills_blanks():
    df = pd.DataFrame({"product_id": [1, 2], "created_at": ["2020-01-01", None]})
    out = fill_audit_columns("products", df)
    assert out.loc[0, "created_at"] == pd.Timestamp("2020-01-01")  # kept
    assert out.loc[1, "created_at"] > pd.Timestamp("2025-01-01")  # filled with now
    assert "updated_at" not in out.columns  # products table has no updated_at


def test_csv_without_audit_columns_loads_and_validates(tmp_path):
    (tmp_path / "products.csv").write_text(
        "product_id,product_name,line_of_business,description,base_premium\n"
        "1,Auto,Auto,Car cover,700\n"
    )
    df = load_csv(tmp_path / "products.csv", "products")
    df = validate_table("products", fill_audit_columns("products", df))
    assert len(df) == 1


def test_validate_table_puts_columns_in_schema_order():
    # user's CSV has the columns shuffled - primary key must still end up first
    cols = list(TABLES["products"]["schema"].columns)
    df = pd.DataFrame({
        "base_premium": [700.0], "product_name": ["Auto"], "created_at": [pd.Timestamp("2025-01-01")],
        "line_of_business": ["Auto"], "description": ["x"], "product_id": [1],
    })
    assert list(validate_table("products", df).columns) == cols


@pytest.mark.parametrize("table", LOAD_ORDER)
def test_template_matches_schema(table):
    """templates are header-only CSVs - they must list every schema column except the optional audit ones"""
    header = (TEMPLATES / f"{table}.csv").read_text().strip().split(",")
    expected = [c for c in TABLES[table]["schema"].columns if c not in AUDIT_COLUMNS]
    assert header == expected


@pytest.mark.parametrize("table", LOAD_ORDER)
def test_empty_template_passes_validation(table):
    df = fill_audit_columns(table, load_csv(TEMPLATES / f"{table}.csv", table))
    assert validate_table(table, df).empty


# ---------- lookup tables: products / causes of loss are no longer hard-coded ----------

LOOKUPS = {
    "ref_line_of_business": pd.DataFrame({"line_of_business": ["Property", "Pet"]}),
    "products": pd.DataFrame({"product_name": ["Home", "Pet"]}),
    "ref_cause_of_loss": pd.DataFrame({
        "product_name": ["Home", "Pet"], "cause_of_loss": ["Fire", "VetBills"],
    }),
}


def test_product_must_use_a_known_line_of_business():
    products = pd.DataFrame({"line_of_business": ["Property", "Space"]})
    with pytest.raises(ValueError, match="line_of_business"):
        check_foreign_keys("products", products, LOOKUPS)


def test_cause_lookup_must_point_at_a_known_product():
    causes = pd.DataFrame({"product_name": ["Home", "Boat"], "cause_of_loss": ["Fire", "Sinking"]})
    with pytest.raises(ValueError, match="product_name"):
        check_foreign_keys("ref_cause_of_loss", causes, LOOKUPS)


def test_claim_type_must_be_a_known_product():
    claims = pd.DataFrame({"policy_id": [1], "adjudicator_id": [None], "claim_type": ["Boat"], "cause_of_loss": [None]})
    loaded = {**LOOKUPS, "policies": pd.DataFrame({"policy_id": [1]}), "adjudicators": pd.DataFrame({"adjudicator_id": [1]})}
    with pytest.raises(ValueError, match="claim_type"):
        check_foreign_keys("claims", claims, loaded)


def test_new_product_with_its_own_cause_passes():
    claims = pd.DataFrame({"policy_id": [1], "adjudicator_id": [None], "claim_type": ["Pet"], "cause_of_loss": ["VetBills"]})
    loaded = {**LOOKUPS, "policies": pd.DataFrame({"policy_id": [1]}), "adjudicators": pd.DataFrame({"adjudicator_id": [1]})}
    check_foreign_keys("claims", claims, loaded)  # should not raise


def test_cause_must_fit_the_product():
    # Fire is a Home cause, not a Pet cause
    claims = pd.DataFrame({"policy_id": [1], "adjudicator_id": [None], "claim_type": ["Pet"], "cause_of_loss": ["Fire"]})
    loaded = {**LOOKUPS, "policies": pd.DataFrame({"policy_id": [1]}), "adjudicators": pd.DataFrame({"adjudicator_id": [1]})}
    with pytest.raises(ValueError, match="cause_of_loss"):
        check_foreign_keys("claims", claims, loaded)


def test_blank_cause_is_not_checked():
    claims = pd.DataFrame({"policy_id": [1], "adjudicator_id": [None], "claim_type": ["Pet"], "cause_of_loss": [None]})
    loaded = {**LOOKUPS, "policies": pd.DataFrame({"policy_id": [1]}), "adjudicators": pd.DataFrame({"adjudicator_id": [1]})}
    check_foreign_keys("claims", claims, loaded)  # should not raise


# ---------- cross-table claim rules (claim_type = policy's product, claim_date inside policy term) ----------

from data.pipeline.ingest import check_cross_table_rules  # noqa: E402

RULE_PARENTS = {
    "policies": pd.DataFrame({
        "policy_id": [1], "product_id": [1],
        "effective_date": [pd.Timestamp("2025-01-01")], "expiry_date": [pd.Timestamp("2025-12-31")],
    }),
    "products": pd.DataFrame({"product_id": [1, 2], "product_name": ["Auto", "Home"]}),
}


def _claim(claim_type: str | None = "Auto", claim_date: str = "2025-06-01") -> pd.DataFrame:
    return pd.DataFrame({
        "claim_id": [7], "policy_id": [1], "claim_type": [claim_type], "claim_date": [pd.Timestamp(claim_date)],
    })


def test_consistent_claim_passes():
    check_cross_table_rules("claims", _claim(), RULE_PARENTS)  # should not raise


def test_claim_type_must_match_the_policys_product():
    with pytest.raises(ValueError, match=r"claim 7 is 'Home' but policy 1 is a Auto policy"):
        check_cross_table_rules("claims", _claim(claim_type="Home"), RULE_PARENTS)


def test_claim_outside_policy_term_is_rejected():
    with pytest.raises(ValueError, match=r"claim 7 on 2026-01-15, policy 1 runs 2025-01-01 to 2025-12-31"):
        check_cross_table_rules("claims", _claim(claim_date="2026-01-15"), RULE_PARENTS)


@pytest.mark.parametrize("day", ["2025-01-01", "2025-12-31"])
def test_first_and_last_day_of_the_term_are_allowed(day):
    check_cross_table_rules("claims", _claim(claim_date=day), RULE_PARENTS)  # should not raise


def test_blank_claim_type_is_not_checked():
    check_cross_table_rules("claims", _claim(claim_type=None), RULE_PARENTS)  # should not raise


def test_both_problems_are_reported_together():
    with pytest.raises(ValueError) as err:
        check_cross_table_rules("claims", _claim(claim_type="Home", claim_date="2024-01-01"), RULE_PARENTS)
    assert "doesn't match" in str(err.value) and "outside" in str(err.value)


def test_other_tables_are_ignored():
    check_cross_table_rules("payments", pd.DataFrame({"payment_id": [1]}), RULE_PARENTS)  # should not raise
