"""
The actual ingestion pipeline: CSV -> validate -> light transform -> load
into Postgres. This is the "Python ingestion / Data validation / Pandas
transformation / PostgreSQL" chunk of the pipeline diagram in
docs/basic_requirement.txt.

Run it end to end with:
    python -m data.pipeline.cli --data-dir data/datasets

Each step is a separate function so tests can exercise validation/transform
without needing a live database.
"""

import io
from pathlib import Path

import pandas as pd
from sqlalchemy import Engine, text

from data.pipeline.schemas import AUDIT_COLUMNS, LOAD_ORDER, TABLES


def load_csv(path: Path, table_name: str) -> pd.DataFrame:
    """
    reads one CSV, parsing its date columns and any special dtypes. only
    asks pandas about columns the file actually has, so a CSV without the
    optional audit columns (created_at / updated_at) still loads.
    """
    config = TABLES[table_name]
    present = set(pd.read_csv(path, nrows=0).columns)
    return pd.read_csv(
        path,
        parse_dates=[c for c in config["date_columns"] if c in present],
        dtype={c: t for c, t in config["dtypes"].items() if c in present},
    )


def fill_audit_columns(table_name: str, df: pd.DataFrame) -> pd.DataFrame:
    """
    created_at / updated_at are bookkeeping, nobody's source data has them.
    if the column is missing, add it as "now"; if it's there but has blanks,
    fill just the blanks.
    """
    df = df.copy()
    now = pd.Timestamp.now()
    wanted = [c for c in AUDIT_COLUMNS if c in TABLES[table_name]["schema"].columns]
    for col in wanted:
        if col not in df.columns:
            df[col] = now
        else:
            df[col] = pd.to_datetime(df[col]).fillna(now)
    return df


def clean_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    """
    light transform step - trims stray whitespace on text columns and
    lowercases emails. real-world source files are messier than our own
    generator's output, this is where that kind of cleanup would grow.
    """
    df = df.copy()
    text_columns = df.select_dtypes(include=["object", "str"]).columns
    for col in text_columns:
        df[col] = df[col].str.strip()
    if "email" in df.columns:
        df["email"] = df["email"].str.lower()
    return df


def validate_table(table_name: str, df: pd.DataFrame) -> pd.DataFrame:
    """runs the pandera schema for this table, returns the validated/coerced df"""
    schema = TABLES[table_name]["schema"]
    validated = schema.validate(df, lazy=True)
    # put the columns in schema order (CSV column order is up to the user), the
    # loader relies on the primary key being first
    return validated[list(schema.columns)]


def check_foreign_keys(table_name: str, df: pd.DataFrame, loaded: dict[str, pd.DataFrame]) -> None:
    """
    makes sure every FK column actually points at a row we've already
    loaded - same rule Postgres would enforce, just with a friendlier error
    raised in Python before we even open a connection.
    """
    config = TABLES[table_name]
    all_fks = {**config.get("foreign_keys", {}), **config.get("nullable_foreign_keys", {})}
    for column, (parent_table, parent_column) in all_fks.items():
        known_ids = set(loaded[parent_table][parent_column])
        actual = df[column].dropna()
        missing = actual[~actual.isin(known_ids)]
        if not missing.empty:
            raise ValueError(
                f"{table_name}.{column} has {len(missing)} value(s) with no matching "
                f"{parent_table}.{parent_column}, e.g. {missing.unique()[:5].tolist()}"
            )

    # multi-column keys, e.g. (claim_type, cause_of_loss) must be a pair that exists in the parent
    for columns, (parent_table, parent_columns) in config.get("composite_foreign_keys", []):
        known_pairs = set(zip(*(loaded[parent_table][c] for c in parent_columns)))
        present = df[list(columns)].dropna()  # a blank in either column means "not set", nothing to check
        pairs = list(zip(*(present[c] for c in columns)))
        missing_pairs = sorted({p for p in pairs if p not in known_pairs})
        if missing_pairs:
            raise ValueError(
                f"{table_name}.({', '.join(columns)}) has {len(missing_pairs)} combination(s) with no "
                f"matching {parent_table}({', '.join(parent_columns)}), e.g. {missing_pairs[:5]}"
            )


def check_cross_table_rules(table_name: str, df: pd.DataFrame, loaded: dict[str, pd.DataFrame]) -> None:
    """
    rules that compare a row with a row in ANOTHER table, which pandera (one
    dataframe at a time) and Postgres CHECKs (no subqueries) can't express.
    right now that's two rules on claims:
      1. claim_type has to be the product of the claim's policy
      2. claim_date has to fall inside the policy's term (start and end days included)
    the database has a trigger with the same two rules, this is the friendly-error version.
    """
    if table_name != "claims" or df.empty:
        return

    joined = (
        df[["claim_id", "policy_id", "claim_type", "claim_date"]]
        .merge(loaded["policies"][["policy_id", "product_id", "effective_date", "expiry_date"]], on="policy_id")
        .merge(loaded["products"][["product_id", "product_name"]], on="product_id")
    )

    wrong_type = joined[joined["claim_type"].notna() & (joined["claim_type"] != joined["product_name"])]
    outside_term = joined[(joined["claim_date"] < joined["effective_date"]) | (joined["claim_date"] > joined["expiry_date"])]

    problems = []
    if not wrong_type.empty:
        examples = [
            f"claim {r.claim_id} is '{r.claim_type}' but policy {r.policy_id} is a {r.product_name} policy"
            for r in wrong_type.head(3).itertuples()
        ]
        problems.append(f"{len(wrong_type)} claim(s) whose claim_type doesn't match their policy's product, e.g. " + "; ".join(examples))
    if not outside_term.empty:
        examples = [
            f"claim {r.claim_id} on {r.claim_date:%Y-%m-%d}, policy {r.policy_id} runs {r.effective_date:%Y-%m-%d} to {r.expiry_date:%Y-%m-%d}"
            for r in outside_term.head(3).itertuples()
        ]
        problems.append(f"{len(outside_term)} claim(s) dated outside their policy's term, e.g. " + "; ".join(examples))
    if problems:
        raise ValueError("claims: " + " | ".join(problems))


def load_table(engine: Engine, table_name: str, df: pd.DataFrame) -> None:
    """bulk loads a validated dataframe via COPY, then fixes up the sequence"""
    if df.empty:
        return

    columns = list(df.columns)
    buffer = io.StringIO()
    df.to_csv(buffer, index=False, header=False, na_rep="")
    buffer.seek(0)

    raw_conn = engine.raw_connection()
    try:
        with raw_conn.cursor() as cursor:
            cursor.copy_expert(
                f"COPY {table_name}({', '.join(columns)}) FROM STDIN WITH (FORMAT csv, NULL '')",
                buffer,
            )
        raw_conn.commit()
    finally:
        raw_conn.close()

    if not TABLES[table_name].get("has_sequence", True):
        return  # lookup tables use their text value as the key, no sequence to fix

    pk_column = columns[0]  # validate_table puts columns in schema order, primary key first
    with engine.begin() as conn:
        conn.execute(text(
            f"SELECT setval(pg_get_serial_sequence('{table_name}', '{pk_column}'), "
            f"COALESCE((SELECT MAX({pk_column}) FROM {table_name}), 1))"
        ))


def truncate_all(engine: Engine) -> None:
    """wipes every table so the pipeline can be re-run from a clean slate.
    only ever call this against a local/dev database - see cli.py's --fresh flag."""
    with engine.begin() as conn:
        tables = ", ".join(reversed(LOAD_ORDER))
        conn.execute(text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE"))


def run_pipeline(data_dir: Path, engine: Engine) -> dict[str, int]:
    """the full CSV -> validate -> transform -> load flow, in FK-safe order"""
    loaded_dataframes: dict[str, pd.DataFrame] = {}
    row_counts: dict[str, int] = {}

    for table_name in LOAD_ORDER:
        csv_path = data_dir / f"{table_name}.csv"
        df = load_csv(csv_path, table_name)
        df = fill_audit_columns(table_name, df)
        df = clean_dataframe(df)
        df = validate_table(table_name, df)
        check_foreign_keys(table_name, df, loaded_dataframes)
        check_cross_table_rules(table_name, df, loaded_dataframes)

        load_table(engine, table_name, df)

        loaded_dataframes[table_name] = df
        row_counts[table_name] = len(df)
        print(f"loaded {table_name}: {len(df)} rows")

    return row_counts
