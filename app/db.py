"""
One shared SQLAlchemy engine for the whole app. We mostly use it for its
raw psycopg2 connection (fast COPY loads) rather than the ORM - the tables
already exist via data/database/claims_schema_tables_v3.sql, we're not managing
them through SQLAlchemy models.
"""

from sqlalchemy import Engine, create_engine, inspect, text
from sqlalchemy.exc import OperationalError

from app.config import get_settings


def get_engine() -> Engine:
    settings = get_settings()
    return create_engine(settings.database_url)


class DatabaseNotReady(Exception):
    """The database isn't reachable, has no tables, or has no claims yet. The message says what to do."""


def check_database_ready(engine: Engine) -> None:
    """
    Quick look before running anything big, so a new user gets one clear sentence
    instead of a wall of SQLAlchemy errors. Raises DatabaseNotReady with a fix.
    """
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except OperationalError as exc:
        # hide_password so the message is safe to paste into a chat or an issue
        where = engine.url.render_as_string(hide_password=True)
        raise DatabaseNotReady(
            f"can't connect to the database at {where}.\n"
            "Is it running? Try `docker compose up -d`. Also check DATABASE_URL in your .env file."
        ) from exc

    if not inspect(engine).has_table("claims"):
        raise DatabaseNotReady(
            "the database is running but has no tables yet.\n"
            "With Docker the schema is applied the first time the container starts. "
            "To start over: `docker compose down -v && docker compose up -d`."
        )

    with engine.connect() as conn:
        if conn.execute(text("SELECT COUNT(*) FROM claims")).scalar() == 0:
            raise DatabaseNotReady(
                "no claims are loaded yet.\n"
                "Load some data first: `python -m data.pipeline.cli --data-dir data/datasets` "
                "(or point --data-dir at your own CSVs)."
            )
