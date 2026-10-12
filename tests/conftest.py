"""shared pytest fixtures"""

import os
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import StaticPool

from app.config import get_settings

SCHEMA_FILE = Path(__file__).resolve().parent.parent / "data" / "database" / "claims_schema_tables_v3.sql"


def _test_database_url() -> str:
    """
    Tests never touch the real database. By default we take the normal
    DATABASE_URL and tack `_test` onto the db name (ins_claims -> ins_claims_test).
    Set TEST_DATABASE_URL to use something else.
    """
    explicit = os.environ.get("TEST_DATABASE_URL")
    if explicit:
        return explicit
    url = make_url(get_settings().database_url)
    return url.set(database=f"{url.database}_test").render_as_string(hide_password=False)


def _create_database_if_missing(url_str: str) -> None:
    """connects to the server's default 'postgres' db just to run CREATE DATABASE"""
    url = make_url(url_str)
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": url.database}
            ).scalar()
            if not exists:
                # db names can't be bound parameters, but this one is ours (checked by the caller)
                conn.execute(text(f'CREATE DATABASE "{url.database}"'))
    finally:
        admin.dispose()


@pytest.fixture(scope="session")
def _test_engine():
    """
    Session-wide engine for the throwaway test database: creates it if needed,
    then (re)applies schema v2 once so every test run starts from a known shape.
    """
    url_str = _test_database_url()
    db_name = make_url(url_str).database

    # safety net - if this ever points at the real db, bail out before anything gets wiped
    assert db_name and db_name.endswith("_test"), (
        f"refusing to run tests against '{db_name}' - test database name must end in '_test'"
    )

    try:
        _create_database_if_missing(url_str)
        engine = create_engine(url_str)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception:
        pytest.skip("no database available - run `docker compose up -d` to enable this test")

    # the schema file has several statements + a plpgsql function, so it goes
    # through the raw driver connection in one go
    raw = engine.raw_connection()
    try:
        with raw.cursor() as cur:
            cur.execute(SCHEMA_FILE.read_text())
        raw.commit()
    finally:
        raw.close()

    yield engine
    engine.dispose()


@pytest.fixture
def db_engine(_test_engine: Engine) -> Engine:
    """
    Engine pointing at the `_test` database (never the real one). Skips the
    test if there's no Postgres running. Tests are expected to clean up after
    themselves or start with truncate_all().
    """
    return _test_engine


# ---------- sample data for analytics + report tests ----------

SCHEMA = """
CREATE TABLE customers (customer_id INTEGER PRIMARY KEY, dob DATE, city TEXT, province TEXT);
CREATE TABLE products (product_id INTEGER PRIMARY KEY, product_name TEXT, line_of_business TEXT);
CREATE TABLE adjudicators (adjudicator_id INTEGER PRIMARY KEY, first_name TEXT, last_name TEXT,
    department TEXT, seniority_level TEXT, capacity_score INTEGER);
CREATE TABLE policies (policy_id INTEGER PRIMARY KEY, customer_id INTEGER, product_id INTEGER,
    effective_date DATE, expiry_date DATE, status TEXT, premium_amount NUMERIC);
CREATE TABLE claims (claim_id INTEGER PRIMARY KEY, policy_id INTEGER, adjudicator_id INTEGER,
    claim_date DATE, reported_date DATE, decision_date DATE, cause_of_loss TEXT, claim_type TEXT,
    status TEXT, claim_amount NUMERIC, approved_amount NUMERIC, is_flagged_fraud BOOLEAN,
    fraud_score NUMERIC);
CREATE TABLE payments (payment_id INTEGER PRIMARY KEY, claim_id INTEGER, payment_date DATE,
    payment_amount NUMERIC, payment_method TEXT, payee TEXT);
"""

DATA = """
INSERT INTO customers VALUES (1, '1990-06-01', 'Toronto', 'Ontario'), (2, '1960-03-15', 'Montreal', 'Quebec');
INSERT INTO products VALUES (1, 'Home', 'Property'), (2, 'Auto', 'Auto');
INSERT INTO adjudicators VALUES (1, 'Ana', 'Lopez', 'Claims', 'Junior', 10), (2, 'Bob', 'Chen', 'Fraud', 'Senior', 5);
INSERT INTO policies VALUES
    (1, 1, 1, '2024-01-01', '2025-01-01', 'Active', 1000),
    (2, 2, 1, '2024-01-01', '2025-01-01', 'Active', 500),
    (3, 1, 2, '2024-01-01', '2025-01-01', 'Active', 800);
INSERT INTO claims VALUES
    (1, 1, 1, '2024-01-10', '2024-01-11', '2024-01-21', 'Fire',      'Home', 'Approved', 1000, 800,  0, 10),
    (2, 1, 1, '2024-02-05', '2024-02-05', '2024-02-10', 'Theft',     'Home', 'Denied',    400, NULL, 1, 90),
    (3, 2, 2, '2024-02-20', '2024-02-21', NULL,         'Storm',     'Home', 'Pending',   600, NULL, 0, 20),
    (4, 3, 2, '2024-03-01', '2024-03-01', '2024-03-03', 'Collision', 'Auto', 'Closed',   2000, 1500, 1, 80);
INSERT INTO payments VALUES
    (1, 1, '2024-02-01', 400,  'EFT',    'Customer'),
    (2, 1, '2024-02-15', 400,  'Cheque', 'Customer'),
    (3, 4, '2024-03-10', 1500, 'EFT',    'Repair Shop');
"""


@pytest.fixture
def engine() -> Engine:
    """tiny in-memory SQLite database with hand-made data (known answers) - no Postgres needed"""
    # StaticPool = every connection shares the same in-memory db
    eng = create_engine("sqlite://", poolclass=StaticPool)
    with eng.begin() as conn:
        for statement in (SCHEMA + DATA).split(";"):
            if statement.strip():
                conn.execute(text(statement))
    return eng
