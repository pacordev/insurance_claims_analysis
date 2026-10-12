CREATE TABLE customers (
    customer_id      SERIAL PRIMARY KEY,
    first_name       VARCHAR(50),
    last_name        VARCHAR(50),
    dob              DATE,
    email            VARCHAR(100),
    phone            VARCHAR(30),
    address_line1    VARCHAR(100),
    city             VARCHAR(50),
    province         VARCHAR(50),
    postal_code      VARCHAR(20),
    created_at       TIMESTAMP DEFAULT NOW()
);

CREATE TABLE products (
    product_id       SERIAL PRIMARY KEY,
    product_name     VARCHAR(50),      -- Home, Auto, Life, Travel
    line_of_business VARCHAR(50),
    description      TEXT,
    base_premium     NUMERIC(10,2),
    created_at       TIMESTAMP DEFAULT NOW()
);

CREATE TABLE adjudicators (
    adjudicator_id   SERIAL PRIMARY KEY,
    first_name       VARCHAR(50),
    last_name        VARCHAR(50),
    email            VARCHAR(100),
    phone            VARCHAR(30),
    department       VARCHAR(50),      -- Claims, Fraud, Property, Auto
    seniority_level  VARCHAR(50),      -- Junior, Senior, Lead
    certification    VARCHAR(50),      -- e.g. CIP, FCIP
    workload_score   INTEGER,
    created_at       TIMESTAMP DEFAULT NOW()
);

CREATE TABLE policies (
    policy_id        SERIAL PRIMARY KEY,
    policy_number    VARCHAR(50) UNIQUE,
    customer_id      INTEGER NOT NULL REFERENCES customers(customer_id),
    product_id       INTEGER NOT NULL REFERENCES products(product_id),
    effective_date   DATE,
    expiry_date      DATE,
    status           VARCHAR(20),      -- Active, Expired, Cancelled
    premium_amount   NUMERIC(10,2),
    coverage_limit   NUMERIC(12,2),
    deductible       NUMERIC(10,2),
    created_at       TIMESTAMP DEFAULT NOW()
);

CREATE TABLE claims (
    claim_id         SERIAL PRIMARY KEY,
    claim_number     VARCHAR(50) UNIQUE,
    policy_id        INTEGER NOT NULL REFERENCES policies(policy_id),
    adjudicator_id   INTEGER REFERENCES adjudicators(adjudicator_id),
    claim_date       DATE,
    cause_of_loss    VARCHAR(50),      -- Collision, Fire, Theft, Illness, TripCancellation, etc.
    claim_type       VARCHAR(50),      -- Property, Auto, Life, Travel
    description      TEXT,
    status           VARCHAR(20),      -- Pending, Approved, Denied, Closed
    claim_amount     NUMERIC(12,2),
    approved_amount  NUMERIC(12,2),
    created_at       TIMESTAMP DEFAULT NOW()
);

CREATE TABLE payments (
    payment_id       SERIAL PRIMARY KEY,
    claim_id         INTEGER NOT NULL REFERENCES claims(claim_id),
    payment_date     DATE,
    payment_amount   NUMERIC(12,2),
    payment_method   VARCHAR(30),      -- EFT, Cheque, Credit
    payee            VARCHAR(100),     -- Customer, Vendor, Repair Shop, Hospital, etc.
    created_at       TIMESTAMP DEFAULT NOW()
);
