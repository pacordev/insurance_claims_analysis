"""
Small shared bits: the optional filters every query accepts, and the one
function that actually runs a query and hands back a DataFrame.
"""

from dataclasses import dataclass
from datetime import date

import pandas as pd
from sqlalchemy import Engine, Select, select

from app.analytics.tables import claims, products


@dataclass(frozen=True)
class Filters:
    """
    Optional filters - leave everything as None to get the whole dataset.
    Values always go in as bound parameters (never pasted into the SQL),
    so there's no injection risk even when these come from API query strings.
    """

    date_from: date | None = None  # claim_date >= this
    date_to: date | None = None  # claim_date <= this
    product: str | None = None  # any product_name from the products table

    def apply(self, stmt: Select, *, dates: bool = True) -> Select:
        """Adds the WHERE bits. Set dates=False for queries that aren't about claims."""
        if dates and self.date_from is not None:
            stmt = stmt.where(claims.c.claim_date >= self.date_from)
        if dates and self.date_to is not None:
            stmt = stmt.where(claims.c.claim_date <= self.date_to)
        if self.product is not None:
            stmt = stmt.where(products.c.product_name == self.product)
        return stmt


def run_query(engine: Engine, stmt: Select) -> pd.DataFrame:
    """Runs a SQLAlchemy statement and returns the result as a DataFrame."""
    with engine.connect() as conn:
        return pd.read_sql(stmt, conn)


def safe_divide(numerator: pd.Series, denominator: pd.Series) -> pd.Series:
    """Division that gives NaN (not an error or inf) when the denominator is 0."""
    return numerator / denominator.where(denominator != 0)


def product_names(engine: Engine) -> list[str]:
    """Every product in the products table (it's the source of truth now, no hard-coded list)."""
    return list(run_query(engine, select(products.c.product_name).order_by(products.c.product_name))["product_name"])
