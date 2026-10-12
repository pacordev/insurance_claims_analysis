# Technical notes

For people who want to know how it works, not just what it does. The main README has the quick start; this covers the same ground in more detail, plus how everything fits together.

## Layout

```
data/
  database/    claims_schema_tables_v1.sql and _v2.sql (kept for reference), claims_schema_tables_v3.sql (live)
  templates/   header-only CSVs, one per table, for people loading their own data
  datasets/    claims_data_generator_v2.py, generated CSVs (git-ignored), reference datasets
  pipeline/    schemas.py (pandera), ingest.py, cli.py
app/
  config.py, db.py        settings from env, one SQLAlchemy engine, check_database_ready
  analytics/              queries, one module per metric group
  reports/                collect_claims_data.py, gen_excel_report.py
summary_reports/          generated workbooks (git-ignored)
docs/                     this file, the report screenshots and the interactive platform diagram
tests/
docker-compose.yml        local Postgres 16
requirements.txt          pinned dependencies
```

`data/pipeline` imports `app.db` and `app.config` for the connection. That's the only dependency between the two halves.

## Running it

**Environment.** Python 3.14 in a virtual environment at `.venv/`, with `pip install -r requirements.txt` (everything is pinned). Nothing is installed as a package, modules are imported as `app.*` and `data.*`, so every command has to run from the project root. Activate the environment per terminal: `source .venv/bin/activate` (Windows: `.venv\Scripts\activate`).

**Configuration.** One setting, `DATABASE_URL`, read by `app/config.py` (pydantic-settings) from the environment or a `.env` file; a real environment variable wins over `.env`. The default and the value in `.env.example` is `postgresql+psycopg2://postgres:postgres@localhost:5433/ins_claims`. `TEST_DATABASE_URL` is optional, see Testing.

**The database.** `docker-compose.yml` runs `postgres:16-alpine` with user and password `postgres` (local use only), database `ins_claims`, port 5433 (5432 was taken on the machine this was built on), and a named volume so data survives restarts. The schema file is mounted into Postgres's init folder, which means it is applied **only the first time a fresh volume starts**. Changing the schema therefore means recreating the volume.

| To do this | Run |
|---|---|
| Start the database | `docker compose up -d` |
| Make the sample data | `python data/datasets/claims_data_generator_v2.py` (writes the 8 CSVs next to the script, same data every time) |
| Load a folder of CSVs | `python -m data.pipeline.cli --data-dir <folder> [--fresh]` |
| Build the report | `python -m app.reports.gen_excel_report [--date-from] [--date-to] [--product] [--output]` |
| Run the tests | `pytest` |

**Rebuilding from nothing** (after a schema change, or to reset):

```bash
docker compose down -v                    # removes the container AND its data volume
docker compose up -d                      # applies the schema on first start
python data/datasets/claims_data_generator_v2.py
python -m data.pipeline.cli --data-dir data/datasets
```

The test database lives in the same container but is separate, so this doesn't need to be redone for tests. It's recreated automatically (and rebuilt from the schema file each test session).

## Data model

Six data tables: `customers`, `products`, `adjudicators`, `policies`, `claims`, `payments`, plus two lookup tables: `ref_line_of_business` and `ref_cause_of_loss`. The chain is customer -> policy -> claim -> payment, with an adjudicator attached to each claim. Claims and payments are the fact tables, the rest are dimensions.

Schema v2 changed v1 because v1 couldn't answer the questions the project is about:

- **Dates on claims.** v1 had one date. v2 has `claim_date` (the loss), `reported_date` and `decision_date`, so processing time can be measured.
- **Fraud fields.** `is_flagged_fraud` and `fraud_score` (0-100). v1 had nothing for fraud analysis.
- **`workload_score` became `capacity_score`.** A fixed number set at hire can't be workload. Real workload is counted from claims.
- **CHECK constraints** on every status, type and enum-like column, plus money columns (>= 0), `approved_amount <= claim_amount`, `reported_date >= claim_date`, and `decision_date >= reported_date`.
- Unique customer email, indexes on every foreign key and on `claim_date`, `status` and `is_flagged_fraud`, and `updated_at` with a trigger on claims and policies.

**Lookup tables for the product-related values (schema v3).** Schema v2 hard-coded the four products and thirteen causes of loss in CHECK constraints, which made a new product (Pet, Health) a schema change. v3 moves them into lookup tables:

- `ref_line_of_business` holds the allowed lines of business. `products.line_of_business` points at it.
- `products.product_name` is unique, and `claims.claim_type` is a foreign key to it. The products table doubles as the list of claim types.
- `ref_cause_of_loss` holds (product, cause) pairs. `claims (claim_type, cause_of_loss)` is a composite foreign key into it, so a cause that doesn't belong to the claim's product is rejected by the database. The old CHECK couldn't do that.

The lookups are keyed on the readable text value, not a numeric id. Claims still store `Fire` and `Auto`, CSVs stay readable, and the analytics queries need no extra joins. It's the same integrity a numeric lookup gives, minus the joins. Adding a product is adding rows.

Everything else keeps its CHECK constraint (claim and policy status, payment method and payee, department, seniority, certification). The analytics depend on exact status values like Approved and Closed, and those values really are fixed, so lookup tables there would add joins for no benefit.

**Two cross-table claim rules.** A claim's `claim_type` has to be the product of its policy, and its `claim_date` has to fall inside the policy's term (first and last day included). A CHECK can't see other tables, so they're enforced twice:

- In the loader (`check_cross_table_rules`), which joins claims to policies and products and fails with readable messages like "claim 1204 is 'Home' but policy 88 is a Auto policy". Both problems are reported together, and a blank `claim_type` is skipped.
- In Postgres, with two triggers in the schema. `trg_claims_match_policy` runs on claim inserts and on updates to `policy_id`, `claim_type` or `claim_date`. `trg_policies_keep_claims_valid` runs the other way round: it blocks changing a policy's product or dates if that would leave one of its claims inconsistent. They raise a `check_violation`, so the database holds even for writes that don't go through the loader.

The triggers are Postgres-specific, and the bulk load fires them per row (the full load went from about 4 to about 6 seconds). A claim filed against a different policy term than the one it falls in (a renewal, say) is rejected, which suits this model of one row per term but may not suit someone else's data.

## Synthetic data

`claims_data_generator_v2.py`, seeded (`SEED = 42`), so the same run gives the same data. Each `generate_*` function takes its inputs as arguments and returns a DataFrame.

- **Customers**: provinces weighted by population (Ontario about 39%, Quebec 22%, down to PEI at 0.4%), with real cities that belong to the province and postal codes whose first letter matches it. Names, phones and street addresses come from Faker's `en_CA`.
- **Policies**: one-year terms starting somewhere in the last five years. Status follows the dates (an expired policy can't be Active).
- **Claims**: dates are built in order (loss, report, decision) so the date constraints always hold. Claim amounts are lognormal per product. Fraud score is random noise plus a boost for claims that are large for their product, and the top 4% are flagged. Flagged claims take 10-30 extra days to decide. Status split is roughly 20% Pending, 40% Approved, 10% Denied, 30% Closed.
- **Payments**: only Approved/Closed claims get paid. Each one is split into 1-5 payments by a random share, so payments reconcile to approved amounts (to within rounding).

**Lookup files.** The generator also writes `ref_line_of_business.csv` and `ref_cause_of_loss.csv` from the same product catalogue it generates claims from, and its validation checks every claim's (product, cause) pair against them.

**Calibration.** The first version produced loss ratios between 1.4 and 11.8, so every product lost money. Premiums were raised and claim sizes lowered per product until the loss ratios landed around Auto 0.85, Home 0.63, Life 0.56, Travel 0.49. The catch is that volume is fixed at 50,000 claims on 35,000 policies, about 1.4 claims per policy, which is far higher than real insurance. So amounts were scaled down to compensate, and Life claims in particular are small. A cleaner fix would be fewer claims, concentrated on a subset of policies.

## Loading pipeline

`python -m data.pipeline.cli --data-dir data/datasets [--fresh]`

For each of the eight tables, in foreign-key order (`ref_line_of_business`, `products`, `ref_cause_of_loss`, then customers, adjudicators, policies, claims, payments):

1. `load_csv` reads it, parsing only the date columns the file actually has.
2. `fill_audit_columns` adds `created_at` / `updated_at` (current time) if the file doesn't have them or leaves them blank. They're optional so people can bring their own data.
3. `clean_dataframe` trims whitespace and lowercases emails.
4. `validate_table` runs the pandera schema, which mirrors every CHECK constraint in the database, and puts the columns in schema order (the loader needs the primary key first, and CSV column order is up to the user).
5. `check_foreign_keys` verifies parent rows exist, in Python, so the error is readable. That includes the multi-column (claim type, cause of loss) pair, and it skips rows where either value is blank.
6. `check_cross_table_rules` checks the two claim-versus-policy rules described above.
7. `load_table` bulk loads with Postgres `COPY` through psycopg2, then resets the ID sequence past the highest loaded ID (lookup tables use text keys and have no sequence).

`data/templates/` holds header-only CSVs for each table, a test checks they always match the schemas. The full dataset loads in about four seconds. `--fresh` truncates everything first and is for local use only.

The load is **not atomic across tables**: each table is committed as it goes. If `claims.csv` fails validation, the lookups, customers and policies are already in. The failure message names the table and rows, and the fix is to correct the file and rerun with `--fresh`. Validation of a table happens before it is written, so a bad table never half-loads, but earlier tables stay.

## Analytics layer

Functions in `app/analytics/`, one module per metric group:

| Module | Functions |
|---|---|
| `claims_performance` | `claims_frequency`, `average_claim_amount`, `claim_severity`, `approval_rate`, `monthly_trends` |
| `financial` | `loss_ratio`, `product_profitability`, `payout_trends` |
| `fraud` | `fraud_flag_rate`, `find_outliers`, `anomaly_hit_rate`, `high_risk_segments` |
| `adjudicators` | `adjudicator_performance`, `performance_by_seniority` |
| `regional` | `regional_analysis`, `customer_segmentation` |

**Portable queries.** They're built with SQLAlchemy Core against lightweight table definitions in `tables.py` (names and types, no reflection), with all filter values passed as bound parameters. Anything that differs between database engines is done in pandas: medians and percentiles, outlier maths, date subtraction for processing days, age bands. The same code runs on SQLite (tests) and PostgreSQL.

**Interface.** Each function takes an engine and an optional `Filters` (date range, product) and returns a DataFrame. Sums over several tables are done as separate queries merged in pandas, because joining claims to policies and payments in one query multiplies rows and breaks the totals.

**Anomaly detection.** For each product, a claim is an outlier if its amount is above Q3 + 1.5 x IQR (or above mean + 3 standard deviations for the z-score option). Only the high side counts. `anomaly_hit_rate` then compares outliers with real fraud flags: precision is the share of outliers that were flagged, recall is the share of flagged claims that were outliers. On the sample data the IQR check has precision of 0.80-0.98 and recall of 0.73-0.95 depending on product. That's partly by construction, since the generator ties fraud score to claim size.

**High-risk segments.** Product and province groups with at least 30 claims, compared with their own product's average claim size and fraud rate, not the overall average. An earlier version used the overall average and Life topped the list simply because Life claims are big.

**Simplifications to know about:**

- Claims frequency divides by all policies, not only active ones.
- Loss ratio uses full written premium, not pro-rated earned premium.
- Date filters apply to claims only. Premium and policy counts always cover the whole book, so frequency and loss ratio are off for partial windows.
- Loss ratio by year buckets premium by policy start year and claims by loss year.

### Using the analytics from Python

The report is the normal way to run the analysis, but the functions can be called directly, for example from a notebook:

```python
from app.db import get_engine
from app.analytics import financial
from app.analytics.common import Filters

df = financial.loss_ratio(get_engine(), filters=Filters(product="Auto"))
```

Every function takes an engine and optional filters and returns a DataFrame. There's deliberately no command-line tool for single analyses.

## Excel report

`collect_claims_data.py` runs every analytics function once, works out ten headline numbers and a few plain-English highlights. It has no Excel code in it. `gen_excel_report.py` writes the workbook with XlsxWriter: native Excel tables and charts, so the file is self-contained.

Seven sheets: Overview, Claims Performance, Financial, Risk & Fraud, Adjudicators, Customer & Regional, Notes. Each sheet has charts on top and tables below. Charts follow a fixed palette (blue, orange, aqua, yellow), a single colour for single-series charts, no dual axes (so the monthly trend is two charts), and a legend only when there's more than one series. Empty selections still produce a valid file, with "No data for this selection" where a table would be.

Before building anything, the command runs `app.db.check_database_ready`. It raises `DatabaseNotReady` with a one-line fix when the database can't be reached, has no tables, or has no claims, and the command turns that into `error: ...` with exit code 1 and no traceback. The password in the database URL is masked in the message. This check lives in the command, not in `build_report`, so calling the report from code stays unaffected.

## Testing

`pytest` from the project root, about 105 tests in under 3 seconds. Three kinds:

- **Pure unit tests** for schemas and loading logic, including the lookup checks and the cross-table rules. No database.
- **Analytics, report and readiness-check tests** on an in-memory SQLite database seeded with a handful of rows where every expected number is known (`tests/conftest.py`). The report tests open the `.xlsx` as a zip and check sheets, charts and strings. The readiness-check tests (`tests/test_db_check.py`) use SQLite engines that are unreachable, empty, or have no claims, and check the messages, the exit behaviour of the report command, and that the password is masked.
- **Integration tests** for the pipeline and the schema, against a real Postgres. They include one test per trigger rule that writes straight to the database to prove it refuses bad data without the loader, and a test that adds a brand-new product through lookup rows only.

The integration tests run in a separate database named `<name>_test` (`ins_claims_test` by default, or `TEST_DATABASE_URL`). It's created automatically, the schema file is applied at the start of each session, and a guard refuses to run if the name doesn't end in `_test`. That exists because the pipeline test used to truncate the real tables. If Postgres isn't running, those tests skip instead of failing.

## Known limitations

- Synthetic data. The patterns come from how the generator was written, so some things are flat: approval rate is about 87% everywhere, and adjudicators don't differ from each other.
- Claims per policy is unrealistically high (see Calibration).
- The report folds products beyond the top four (by premium) into "Other" in multi-series charts, since only four colours are validated. Single-series charts show every product.
- A claim-count spike in September 2026 comes from the generator spreading policy starts over five years while claims land inside each policy's year.
- The loader only works with PostgreSQL (bulk `COPY`), and so do the triggers. The analytics don't depend on it.
- The loader isn't atomic across tables (see Loading pipeline).
- The data generator has no tests of its own. It's covered indirectly: its output has to load through the pipeline, and it validates its own dates, amounts and lookups.