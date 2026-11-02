-- Deterministic synthetic data, matching connectors/mysql/seed/001-source.sql.
-- Run by aep-sqlserver-seed (batches split on GO); skipped when the database exists.
CREATE DATABASE source;
GO
USE source;
GO
CREATE SCHEMA sales;
GO
CREATE TABLE sales.customers (
    tenant_id bigint NOT NULL DEFAULT 1,
    customer_id bigint NOT NULL,
    name nvarchar(200) NOT NULL,
    email nvarchar(200),
    city nvarchar(100),
    PRIMARY KEY (tenant_id, customer_id)
);
CREATE TABLE sales.products (
    tenant_id bigint NOT NULL DEFAULT 1,
    product_id bigint NOT NULL,
    name nvarchar(200) NOT NULL,
    category nvarchar(100),
    price decimal(12, 2),
    PRIMARY KEY (tenant_id, product_id)
);
CREATE TABLE sales.orders (
    tenant_id bigint NOT NULL DEFAULT 1,
    order_id bigint NOT NULL,
    customer_id bigint NOT NULL,
    product_id bigint NOT NULL,
    quantity int,
    status nvarchar(20),
    ordered_at datetime2(0) NOT NULL,
    PRIMARY KEY (tenant_id, order_id),
    FOREIGN KEY (tenant_id, customer_id) REFERENCES sales.customers (tenant_id, customer_id),
    FOREIGN KEY (tenant_id, product_id) REFERENCES sales.products (tenant_id, product_id)
);
GO
-- Missing values, extra spaces, inconsistent case, duplicate emails and
-- negative quantities are intentional ETL exercises, just like MySQL and Postgres.
WITH numbers(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM numbers WHERE i < 1000)
INSERT INTO sales.customers (customer_id, name, email, city)
SELECT i, CONCAT('Customer ', i),
       CASE WHEN i % 23 = 0 THEN NULL
            WHEN i % 31 = 0 THEN 'duplicate@example.test'
            WHEN i % 17 = 0 THEN CONCAT(' customer-', i, '@example.test ')
            ELSE CONCAT('customer-', i, '@example.test') END,
       CASE WHEN i % 19 = 0 THEN NULL
            WHEN i % 7 = 0 THEN ' Shanghai ' ELSE 'Shanghai' END
FROM numbers OPTION (MAXRECURSION 0);

WITH numbers(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM numbers WHERE i < 200)
INSERT INTO sales.products (product_id, name, category, price)
SELECT i, CONCAT('Product ', i),
       CASE WHEN i % 13 = 0 THEN NULL ELSE CONCAT('Category ', i % 5) END,
       CASE WHEN i % 29 = 0 THEN NULL ELSE i * 1.25 END
FROM numbers OPTION (MAXRECURSION 0);

WITH numbers(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM numbers WHERE i < 5000)
INSERT INTO sales.orders (order_id, customer_id, product_id, quantity, status, ordered_at)
SELECT i, ((i - 1) % 1000) + 1, ((i - 1) % 200) + 1,
       CASE WHEN i % 47 = 0 THEN -1 WHEN i % 37 = 0 THEN NULL ELSE (i % 5) + 1 END,
       CASE WHEN i % 41 = 0 THEN NULL WHEN i % 11 = 0 THEN ' Paid ' ELSE 'paid' END,
       DATEADD(minute, i, CAST('2026-01-01T00:00:00' AS datetime2(0)))
FROM numbers OPTION (MAXRECURSION 0);
GO
UPDATE STATISTICS sales.customers;
UPDATE STATISTICS sales.products;
UPDATE STATISTICS sales.orders;
