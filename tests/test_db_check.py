"""
Tests for the "is the database ready?" check that the report command runs first.
Uses SQLite engines (no Postgres needed): one that can't be opened, one empty,
one with an empty claims table, and the normal sample one.
"""

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import StaticPool

from app.db import DatabaseNotReady, check_database_ready
from app.reports import gen_excel_report as excel


@pytest.fixture
def unreachable_engine(tmp_path) -> Engine:
    # a database file inside a folder that doesn't exist: sqlite can't open it
    return create_engine(f"sqlite:///{tmp_path}/no/such/folder/x.db")


@pytest.fixture
def no_tables_engine() -> Engine:
    return create_engine("sqlite://", poolclass=StaticPool)


@pytest.fixture
def no_claims_engine() -> Engine:
    eng = create_engine("sqlite://", poolclass=StaticPool)
    with eng.begin() as conn:
        conn.execute(text("CREATE TABLE claims (claim_id INTEGER PRIMARY KEY)"))
    return eng


def test_ready_database_passes(engine: Engine) -> None:
    check_database_ready(engine)  # should not raise


def test_unreachable_database_says_how_to_start_it(unreachable_engine: Engine) -> None:
    with pytest.raises(DatabaseNotReady, match=r"(?s)can't connect.*docker compose up -d.*DATABASE_URL"):
        check_database_ready(unreachable_engine)


def test_missing_tables_are_explained(no_tables_engine: Engine) -> None:
    with pytest.raises(DatabaseNotReady, match="no tables yet"):
        check_database_ready(no_tables_engine)


def test_empty_claims_table_says_to_load_data(no_claims_engine: Engine) -> None:
    with pytest.raises(DatabaseNotReady, match="(?s)no claims are loaded yet.*data.pipeline.cli"):
        check_database_ready(no_claims_engine)


def test_password_never_appears_in_the_message() -> None:
    eng = create_engine("postgresql+psycopg2://someone:s3cret-pw@127.0.0.1:1/nothing")
    with pytest.raises(DatabaseNotReady) as err:
        check_database_ready(eng)
    assert "s3cret-pw" not in str(err.value)
    assert "someone" in str(err.value)  # the rest of the address is still shown


# ---------- what the report command does with it ----------


def test_report_command_stops_with_one_clear_line_when_no_claims(no_claims_engine: Engine, tmp_path) -> None:
    out = tmp_path / "r.xlsx"
    with pytest.raises(SystemExit) as err:
        excel.main(["--output", str(out)], engine=no_claims_engine)
    assert str(err.value).startswith("error: no claims are loaded yet")
    assert not out.exists()  # no half-empty workbook left behind


def test_report_command_stops_when_database_unreachable(unreachable_engine: Engine, tmp_path) -> None:
    with pytest.raises(SystemExit) as err:
        excel.main(["--output", str(tmp_path / "r.xlsx")], engine=unreachable_engine)
    assert "docker compose up -d" in str(err.value)
