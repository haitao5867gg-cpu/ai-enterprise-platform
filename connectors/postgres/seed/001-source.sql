\set ON_ERROR_STOP on
\getenv reader_password AEP_POSTGRES_PASSWORD

BEGIN;

CREATE ROLE source_reader WITH LOGIN PASSWORD :'reader_password'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
REVOKE ALL ON DATABASE aep_source FROM PUBLIC;
GRANT CONNECT ON DATABASE aep_source TO source_reader;
REVOKE ALL ON SCHEMA public FROM PUBLIC;

CREATE SCHEMA sales;
CREATE TABLE sales.customers (
    tenant_id bigint NOT NULL DEFAULT 1,
    customer_id bigint NOT NULL,
    name text NOT NULL,
    email text,
    city text,
    PRIMARY KEY (tenant_id, customer_id)
);
CREATE TABLE sales.products (
    tenant_id bigint NOT NULL DEFAULT 1,
    product_id bigint NOT NULL,
    name text NOT NULL,
    category text,
    price numeric(12, 2),
    PRIMARY KEY (tenant_id, product_id)
);
CREATE TABLE sales.orders (
    tenant_id bigint NOT NULL DEFAULT 1,
    order_id bigint NOT NULL,
    customer_id bigint NOT NULL,
    product_id bigint NOT NULL,
    quantity integer,
    status text,
    ordered_at timestamptz NOT NULL,
    PRIMARY KEY (tenant_id, order_id),
    FOREIGN KEY (tenant_id, customer_id) REFERENCES sales.customers(tenant_id, customer_id),
    FOREIGN KEY (tenant_id, product_id) REFERENCES sales.products(tenant_id, product_id)
);

-- Deterministic synthetic data: missing values, extra spaces, inconsistent case,
-- negative quantities and duplicate email values are intentional ETL exercises.
INSERT INTO sales.customers (customer_id, name, email, city)
SELECT i, 'Customer ' || i,
       CASE WHEN i % 23 = 0 THEN NULL
            WHEN i % 31 = 0 THEN 'duplicate@example.test'
            WHEN i % 17 = 0 THEN ' customer-' || i || '@example.test '
            ELSE 'customer-' || i || '@example.test' END,
       CASE WHEN i % 19 = 0 THEN NULL
            WHEN i % 7 = 0 THEN ' Shanghai ' ELSE 'Shanghai' END
FROM generate_series(1, 1000) AS i;

INSERT INTO sales.products (product_id, name, category, price)
SELECT i, 'Product ' || i,
       CASE WHEN i % 13 = 0 THEN NULL ELSE 'Category ' || (i % 5) END,
       CASE WHEN i % 29 = 0 THEN NULL ELSE (i * 1.25)::numeric(12, 2) END
FROM generate_series(1, 200) AS i;

INSERT INTO sales.orders (order_id, customer_id, product_id, quantity, status, ordered_at)
SELECT i, ((i - 1) % 1000) + 1, ((i - 1) % 200) + 1,
       CASE WHEN i % 47 = 0 THEN -1 WHEN i % 37 = 0 THEN NULL ELSE (i % 5) + 1 END,
       CASE WHEN i % 41 = 0 THEN NULL WHEN i % 11 = 0 THEN ' Paid ' ELSE 'paid' END,
       timestamptz '2026-01-01 00:00:00+00' + i * interval '1 minute'
FROM generate_series(1, 5000) AS i;

GRANT USAGE ON SCHEMA sales TO source_reader;
GRANT SELECT ON ALL TABLES IN SCHEMA sales TO source_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA sales GRANT SELECT ON TABLES TO source_reader;
ALTER ROLE source_reader IN DATABASE aep_source SET default_transaction_read_only = on;
ALTER ROLE source_reader IN DATABASE aep_source SET statement_timeout = '3000ms';

ANALYZE sales.customers;
ANALYZE sales.products;
ANALYZE sales.orders;
COMMIT;
