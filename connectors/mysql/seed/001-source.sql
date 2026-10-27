-- Deterministic synthetic data, matching connectors/postgres/seed/001-source.sql.
USE sales;
SET SESSION cte_max_recursion_depth = 5000;
SET SESSION time_zone = '+00:00';

CREATE TABLE customers (
    tenant_id bigint NOT NULL DEFAULT 1,
    customer_id bigint NOT NULL,
    name text NOT NULL,
    email text,
    city text,
    PRIMARY KEY (tenant_id, customer_id)
);
CREATE TABLE products (
    tenant_id bigint NOT NULL DEFAULT 1,
    product_id bigint NOT NULL,
    name text NOT NULL,
    category text,
    price numeric(12, 2),
    PRIMARY KEY (tenant_id, product_id)
);
CREATE TABLE orders (
    tenant_id bigint NOT NULL DEFAULT 1,
    order_id bigint NOT NULL,
    customer_id bigint NOT NULL,
    product_id bigint NOT NULL,
    quantity integer,
    status text,
    ordered_at timestamp NOT NULL,
    PRIMARY KEY (tenant_id, order_id),
    FOREIGN KEY (tenant_id, customer_id) REFERENCES customers(tenant_id, customer_id),
    FOREIGN KEY (tenant_id, product_id) REFERENCES products(tenant_id, product_id)
);

-- Missing values, extra spaces, inconsistent case, duplicate emails and
-- negative quantities are intentional ETL exercises, just like Postgres.
INSERT INTO customers (customer_id, name, email, city)
WITH RECURSIVE numbers(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM numbers WHERE i < 1000)
SELECT i, CONCAT('Customer ', i),
       CASE WHEN i % 23 = 0 THEN NULL
            WHEN i % 31 = 0 THEN 'duplicate@example.test'
            WHEN i % 17 = 0 THEN CONCAT(' customer-', i, '@example.test ')
            ELSE CONCAT('customer-', i, '@example.test') END,
       CASE WHEN i % 19 = 0 THEN NULL
            WHEN i % 7 = 0 THEN ' Shanghai ' ELSE 'Shanghai' END
FROM numbers;

INSERT INTO products (product_id, name, category, price)
WITH RECURSIVE numbers(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM numbers WHERE i < 200)
SELECT i, CONCAT('Product ', i),
       CASE WHEN i % 13 = 0 THEN NULL ELSE CONCAT('Category ', i % 5) END,
       CASE WHEN i % 29 = 0 THEN NULL ELSE i * 1.25 END
FROM numbers;

INSERT INTO orders (order_id, customer_id, product_id, quantity, status, ordered_at)
WITH RECURSIVE numbers(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM numbers WHERE i < 5000)
SELECT i, ((i - 1) % 1000) + 1, ((i - 1) % 200) + 1,
       CASE WHEN i % 47 = 0 THEN -1 WHEN i % 37 = 0 THEN NULL ELSE (i % 5) + 1 END,
       CASE WHEN i % 41 = 0 THEN NULL WHEN i % 11 = 0 THEN ' Paid ' ELSE 'paid' END,
       TIMESTAMP('2026-01-01 00:00:00') + INTERVAL i MINUTE
FROM numbers;

ANALYZE TABLE customers, products, orders;
