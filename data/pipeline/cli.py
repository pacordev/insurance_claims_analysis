"""
Command-line entrypoint for the ingestion pipeline.

Usage:
    python -m data.pipeline.cli --data-dir data/datasets
    python -m data.pipeline.cli --data-dir data/datasets --fresh   # wipes tables first, LOCAL/DEV ONLY
"""

import argparse
from pathlib import Path

from app.db import get_engine
from data.pipeline.ingest import run_pipeline, truncate_all


def main() -> None:
    parser = argparse.ArgumentParser(description="Load claims CSVs into Postgres")
    parser.add_argument("--data-dir", type=Path, default=Path("data/datasets"), help="folder holding the 6 CSVs")
    parser.add_argument(
        "--fresh", action="store_true",
        help="TRUNCATE every table before loading - only ever use this against a local/dev database",
    )
    args = parser.parse_args()

    engine = get_engine()

    if args.fresh:
        print("--fresh set: truncating all tables first")
        truncate_all(engine)

    row_counts = run_pipeline(args.data_dir, engine)
    print("done:", row_counts)


if __name__ == "__main__":
    main()
