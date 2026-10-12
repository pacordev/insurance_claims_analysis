-- =====================================================================
-- Insurance Claims Analytics Platform - Schema v2
-- Builds on database/claims_schema_tables.sql (kept as-is for reference).
-- Changes vs v1, per docs/claims_plat_planning.md:
--   - fraud fields + extra dates on claims (needed for fraud/processing-time metrics)
--   - adjudicators.workload_score renamed to capacity_score (it's a target/baseline,
--     real workload gets computed from the claims table in the analytics layer)
--   - CHECK constraints on status/type/enum-ish columns (chose this over Ref_* lookup
--     tables to keep things lean - see planning doc for the reasoning)
--   - sanity CHECKs on money columns and date ordering
--   - indexes on FK / commonly-filtered columns
--   - updated_at columns + trigger, since claims/policies change status over time
-- =====================================================================

-- drop in reverse dependency order so this script is safe to re-run locally
DROP TABLE IF EXISTS payments CASCADE;
DROP TABLE IF EXISTS claims CASCADE;
DROP TABLE IF EXISTS policies CASCADE;
DROP TABLE IF EXISTS adjudicators CASCADE;
DROP TABLE IF EXISTS products CASCADE;
DROP TABLE IF EXISTS customers CASCADE;

-- shared helper: bumps updated_at to now() whenever a row changes
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- =====================================================================
-- customers
-- =====================================================================
CREATE TABLE customers (
    customer_id      SERIAL PRIMARY KEY,
    first_name       VARCHAR(50) NOT NULL,
    last_name        VARCHAR(50) NOT NULL,
    dob              DATE,
    email            VARCHAR(100) UNIQUE,          -- v2: unique, this is our natural customer key
    phone            VARCHAR(30),
    address_line1    VARCHAR(100),
    city             VARCHAR(50),
    province         VARCHAR(50),
    postal_code      VARCHAR(20),
    created_at       TIMESTAMP DEFAULT NOW()
);

-- =====================================================================
-- products
-- =====================================================================
CREATE TABLE products (
    product_id       SERIAL PRIMARY KEY,
    product_name     VARCHAR(50) NOT NULL,
    line_of_business VARCHAR(50) NOT NULL,
    description      TEXT,
    base_premium     NUMERIC(10,2) CHECK (base_premium >= 0),
    created_at       TIMESTAMP DEFAULT NOW(),

    -- only the 4 lines we actually generate/support today - widen this list
    -- (or move to a lookup table) if new products get added later
    CONSTRAINT chk_products_name CHECK (product_name IN ('Home', 'Auto', 'Life', 'Travel')),
    CONSTRAINT chk_products_lob  CHECK (line_of_business IN ('Property', 'Auto', 'Life', 'Travel'))
);

-- =====================================================================
-- adjudicators
-- =====================================================================
CREATE TABLE adjudicators (
    adjudicator_id   SERIAL PRIMARY KEY,
    first_name       VARCHAR(50) NOT NULL,
    last_name        VARCHAR(50) NOT NULL,
    email            VARCHAR(100) UNIQUE,
    phone            VARCHAR(30),
    department       VARCHAR(50),
    seniority_level  VARCHAR(50),
    certification    VARCHAR(50),

    -- v2: renamed from workload_score. this is a target/capacity baseline set at
    -- hire time, NOT live workload - actual workload is COUNT(claims) per
    -- adjudicator, computed in the analytics layer so it's always accurate
    capacity_score   INTEGER CHECK (capacity_score >= 0),
    created_at       TIMESTAMP DEFAULT NOW(),

    CONSTRAINT chk_adj_department CHECK (department IN ('Claims', 'Fraud', 'Property', 'Auto')),
    CONSTRAINT chk_adj_seniority  CHECK (seniority_level IN ('Junior', 'Senior', 'Lead')),
    CONSTRAINT chk_adj_cert       CHECK (certification IN ('CIP', 'FCIP', 'None'))
);

-- =====================================================================
-- policies
-- =====================================================================
CREATE TABLE policies (
    policy_id        SERIAL PRIMARY KEY,
    policy_number    VARCHAR(50) UNIQUE NOT NULL,
    customer_id      INTEGER NOT NULL REFERENCES customers(customer_id),
    product_id       INTEGER NOT NULL REFERENCES products(product_id),
    effective_date   DATE NOT NULL,
    expiry_date      DATE NOT NULL,
    status           VARCHAR(20) NOT NULL,
    premium_amount   NUMERIC(10,2) CHECK (premium_amount >= 0),
    coverage_limit   NUMERIC(12,2) CHECK (coverage_limit >= 0),
    deductible       NUMERIC(10,2) CHECK (deductible >= 0),
    created_at       TIMESTAMP DEFAULT NOW(),
    updated_at       TIMESTAMP DEFAULT NOW(),      -- v2: status changes over time, track it

    CONSTRAINT chk_policies_status CHECK (status IN ('Active', 'Expired', 'Cancelled')),
    CONSTRAINT chk_policies_dates  CHECK (expiry_date > effective_date)
);

CREATE TRIGGER trg_policies_updated_at
    BEFORE UPDATE ON policies
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

-- =====================================================================
-- claims
-- =====================================================================
CREATE TABLE claims (
    claim_id         SERIAL PRIMARY KEY,
    claim_number     VARCHAR(50) UNIQUE NOT NULL,
    policy_id        INTEGER NOT NULL REFERENCES policies(policy_id),
    adjudicator_id   INTEGER REFERENCES adjudicators(adjudicator_id),

    -- v2: split "when did this happen" into 3 separate dates so we can actually
    -- measure adjudicator processing time (that was impossible with just claim_date)
    claim_date       DATE NOT NULL,                 -- date of loss / incident
    reported_date    DATE NOT NULL,                 -- date the customer reported it to us
    decision_date    DATE,                           -- date the adjudicator closed it out, null while open

    cause_of_loss    VARCHAR(50),
    claim_type       VARCHAR(50),
    description      TEXT,
    status           VARCHAR(20) NOT NULL,
    claim_amount     NUMERIC(12,2) CHECK (claim_amount >= 0),
    approved_amount  NUMERIC(12,2) CHECK (approved_amount >= 0),

    -- v2: fraud fields - nothing existed before to support the fraud-detection
    -- requirement. fraud_score is a 0-100 risk score, is_flagged_fraud is the
    -- adjudicator's actual call
    is_flagged_fraud BOOLEAN NOT NULL DEFAULT FALSE,
    fraud_score      NUMERIC(5,2) CHECK (fraud_score IS NULL OR (fraud_score BETWEEN 0 AND 100)),

    created_at       TIMESTAMP DEFAULT NOW(),
    updated_at       TIMESTAMP DEFAULT NOW(),

    -- v2: fixed claim_type to match what the generator actually produces
    -- (product_name: Home/Auto/Life/Travel), not the old "Property" placeholder
    CONSTRAINT chk_claims_type   CHECK (claim_type IN ('Home', 'Auto', 'Life', 'Travel')),
    CONSTRAINT chk_claims_status CHECK (status IN ('Pending', 'Approved', 'Denied', 'Closed')),
    CONSTRAINT chk_claims_cause  CHECK (cause_of_loss IN (
        'Fire', 'WaterDamage', 'Theft', 'Storm',                 -- Home
        'Collision', 'Vandalism', 'GlassBreakage',                -- Auto
        'Illness', 'Accident', 'NaturalCauses',                   -- Life
        'TripCancellation', 'MedicalEmergency', 'LostLuggage'     -- Travel
    )),
    -- approved amount can't exceed what was claimed
    CONSTRAINT chk_claims_approved_amount CHECK (approved_amount IS NULL OR approved_amount <= claim_amount),
    -- you have to report a loss on or after the day it happened
    CONSTRAINT chk_claims_reported_after_loss CHECK (reported_date >= claim_date),
    -- a decision can't be made before the claim was reported
    CONSTRAINT chk_claims_decision_after_report CHECK (decision_date IS NULL OR decision_date >= reported_date)

    -- note: "cause_of_loss matches the claim's product line" and "claim_date falls
    -- within the policy's effective/expiry window" are cross-table rules that
    -- Postgres CHECK constraints can't express (no subqueries allowed) - those get
    -- enforced in the pandera/pydantic validation step of the ingestion pipeline
);

CREATE TRIGGER trg_claims_updated_at
    BEFORE UPDATE ON claims
    FOR EACH ROW
    EXECUTE FUNCTION set_updated_at();

-- =====================================================================
-- payments
-- =====================================================================
CREATE TABLE payments (
    payment_id       SERIAL PRIMARY KEY,
    claim_id         INTEGER NOT NULL REFERENCES claims(claim_id),
    payment_date     DATE NOT NULL,
    payment_amount   NUMERIC(12,2) CHECK (payment_amount >= 0),
    payment_method   VARCHAR(30),
    payee            VARCHAR(100),
    created_at       TIMESTAMP DEFAULT NOW(),

    CONSTRAINT chk_payments_method CHECK (payment_method IN ('EFT', 'Cheque', 'Credit')),
    CONSTRAINT chk_payments_payee  CHECK (payee IN ('Customer', 'Vendor', 'Repair Shop', 'Hospital', 'TravelAgency'))
);

-- =====================================================================
-- indexes - every FK we join on all the time, plus the columns analytics
-- queries will filter/group by most often (status, dates)
-- =====================================================================
CREATE INDEX idx_policies_customer_id   ON policies(customer_id);
CREATE INDEX idx_policies_product_id    ON policies(product_id);
CREATE INDEX idx_policies_status        ON policies(status);

CREATE INDEX idx_claims_policy_id       ON claims(policy_id);
CREATE INDEX idx_claims_adjudicator_id  ON claims(adjudicator_id);
CREATE INDEX idx_claims_status          ON claims(status);
CREATE INDEX idx_claims_claim_date      ON claims(claim_date);
CREATE INDEX idx_claims_is_flagged_fraud ON claims(is_flagged_fraud);

CREATE INDEX idx_payments_claim_id      ON payments(claim_id);
CREATE INDEX idx_payments_payment_date  ON payments(payment_date);
