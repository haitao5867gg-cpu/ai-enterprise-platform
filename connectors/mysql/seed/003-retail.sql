-- 确定性合成零售数据；这里的脏数据是故意保留的 ETL 演示练习，不是生成错误。
-- 对应 demo/knowledge/retail-metrics（零售数据口径）和 dw-standards（数仓开发规范）：
-- 状态需 trim/lower 后识别 paid、shipped、completed；排除姓名以“测试”开头的账号；
-- 城市需去空格、归一“市”后缀，会员等级需转小写；空折扣表示不打折；数量 <= 0 需清理。
-- 所有数据仅由递归 CTE、序号和取模生成，无 RAND()；可重复执行，且不会改动 sales 库。
CREATE DATABASE IF NOT EXISTS retail CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin;
SET NAMES utf8mb4 COLLATE utf8mb4_0900_bin;
SET SESSION cte_max_recursion_depth = 40000;
SET SESSION time_zone = '+00:00';
USE retail;

DROP TABLE IF EXISTS order_items;
DROP TABLE IF EXISTS orders;
DROP TABLE IF EXISTS products;
DROP TABLE IF EXISTS customers;

CREATE TABLE customers (
    customer_id bigint NOT NULL PRIMARY KEY,
    name varchar(100) NOT NULL,
    city varchar(50),
    member_level varchar(20),
    registered_at datetime NOT NULL
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin;

CREATE TABLE products (
    product_id bigint NOT NULL PRIMARY KEY,
    name varchar(100) NOT NULL,
    category varchar(50),
    list_price decimal(12,2) NOT NULL,
    cost_price decimal(12,2) NOT NULL
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin;

CREATE TABLE orders (
    order_id bigint NOT NULL PRIMARY KEY,
    customer_id bigint NOT NULL,
    channel varchar(20) NOT NULL,
    status varchar(20) NOT NULL,
    ordered_at datetime NOT NULL,
    shipping_fee decimal(10,2) NOT NULL,
    FOREIGN KEY (customer_id) REFERENCES customers(customer_id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin;

CREATE TABLE order_items (
    order_id bigint NOT NULL,
    line_no int NOT NULL,
    product_id bigint NOT NULL,
    quantity int NOT NULL,
    unit_price decimal(12,2) NOT NULL,
    discount_rate decimal(4,3),
    PRIMARY KEY (order_id, line_no),
    FOREIGN KEY (order_id) REFERENCES orders(order_id),
    FOREIGN KEY (product_id) REFERENCES products(product_id)
) DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin;

-- 城市：5% “上海市”、5% “ 上海 ”、3% 空（NULL 或空字符串）。
-- 会员：5% Gold/SILVER、2% NULL。前 5 个客户为测试账号，每个都有订单。
INSERT INTO customers (customer_id, name, city, member_level, registered_at)
WITH RECURSIVE numbers(i) AS (
    SELECT 1 UNION ALL SELECT i + 1 FROM numbers WHERE i < 1200
)
SELECT i,
       CASE WHEN i <= 5 THEN CONCAT('测试账号', i) ELSE CONCAT('客户', LPAD(i, 4, '0')) END,
       CASE WHEN MOD(i - 1, 100) < 2 THEN NULL
            WHEN MOD(i - 1, 100) < 3 THEN ''
            WHEN MOD(i - 1, 100) < 8 THEN '上海市'
            WHEN MOD(i - 1, 100) < 13 THEN ' 上海 '
            ELSE ELT(1 + MOD(i - 1, 8), '上海', '北京', '杭州', '南京', '苏州', '深圳', '广州', '成都')
       END,
       CASE WHEN MOD(i - 1, 100) < 2 THEN NULL
            WHEN MOD(i - 1, 100) < 7 THEN IF(MOD(i, 2) = 0, 'Gold', 'SILVER')
            ELSE ELT(1 + MOD(i - 1, 3), 'normal', 'silver', 'gold') END,
       TIMESTAMP('2024-01-01') + INTERVAL MOD(i * 7, 670) DAY
                             + INTERVAL MOD(i * 137, 86400) SECOND
FROM numbers;

-- 5/150 = 3.33% 品类为空。
INSERT INTO products (product_id, name, category, list_price, cost_price)
WITH RECURSIVE numbers(i) AS (
    SELECT 1 UNION ALL SELECT i + 1 FROM numbers WHERE i < 150
), priced AS (
    SELECT i, 20 + MOD(i * 7919, 198000) / 100 AS price FROM numbers
)
SELECT i, CONCAT('商品', LPAD(i, 3, '0')),
       CASE WHEN MOD(i, 30) = 0 THEN NULL
            ELSE ELT(1 + MOD(i - 1, 5), '家电', '数码', '个护', '食品', '家居') END,
       price, ROUND(price * (0.55 + MOD(i, 15) / 100), 2)
FROM priced;

-- 85% 标准有效状态、8% cancelled、5% refunded、2% 大小写/空格变体。
-- 每 30 个订单分配 5 个给 11 月、5 个给 1 月，其余月份各 2 个：2500/2500/各1000。
INSERT INTO orders (order_id, customer_id, channel, status, ordered_at, shipping_fee)
WITH RECURSIVE numbers(i) AS (
    SELECT 1 UNION ALL SELECT i + 1 FROM numbers WHERE i < 15000
), weighted AS (
    SELECT i, CASE WHEN MOD(i - 1, 30) < 5 THEN 0
                   WHEN MOD(i - 1, 30) < 7 THEN 1
                   WHEN MOD(i - 1, 30) < 12 THEN 2
                   ELSE 3 + FLOOR((MOD(i - 1, 30) - 12) / 2) END AS month_no
    FROM numbers
), dated AS (
    SELECT i, TIMESTAMP('2025-11-01') + INTERVAL month_no MONTH AS month_start FROM weighted
)
SELECT i, 1 + MOD(i - 1, 1200),
       ELT(1 + MOD(i - 1, 3), 'online', 'store', 'mini_program'),
       CASE WHEN MOD(i - 1, 100) < 30 THEN 'paid'
            WHEN MOD(i - 1, 100) < 58 THEN 'shipped'
            WHEN MOD(i - 1, 100) < 85 THEN 'completed'
            WHEN MOD(i - 1, 100) < 93 THEN 'cancelled'
            WHEN MOD(i - 1, 100) < 98 THEN 'refunded'
            WHEN MOD(i - 1, 100) = 98 THEN ' Paid ' ELSE 'COMPLETED' END,
       month_start + INTERVAL MOD(FLOOR((i - 1) / 30), DAY(LAST_DAY(month_start))) DAY
                   + INTERVAL MOD(i * 137, 86400) SECOND,
       IF(MOD(i, 5) = 0, 0, 5 + MOD(i, 4) * 2)
FROM dated;

-- 每 5 单分别有 1、2、2、3、4 行，正好 36000 行。
-- item_no 是连续明细序号：恰好 1% 数量为 0/负数，30% 折扣为空，其余折扣在 0–0.3。
INSERT INTO order_items (order_id, line_no, product_id, quantity, unit_price, discount_rate)
WITH RECURSIVE line_numbers(line_no) AS (
    SELECT 1 UNION ALL SELECT line_no + 1 FROM line_numbers WHERE line_no < 4
), items AS (
    SELECT o.order_id, l.line_no,
           12 * FLOOR((o.order_id - 1) / 5)
             + CASE MOD(o.order_id - 1, 5) WHEN 0 THEN 0 WHEN 1 THEN 1
                                         WHEN 2 THEN 3 WHEN 3 THEN 5 ELSE 8 END
             + l.line_no AS item_no
    FROM orders AS o JOIN line_numbers AS l
      ON l.line_no <= CASE MOD(o.order_id - 1, 5) WHEN 0 THEN 1 WHEN 1 THEN 2
                                               WHEN 2 THEN 2 WHEN 3 THEN 3 ELSE 4 END
)
SELECT i.order_id, i.line_no, p.product_id,
       CASE WHEN MOD(i.item_no, 100) = 0 THEN IF(MOD(i.item_no, 200) = 0, 0, -1)
            ELSE 1 + MOD(i.item_no, 5) END,
       ROUND(p.list_price * (0.9 + MOD(i.item_no, 11) / 100), 2),
       CASE WHEN MOD(i.item_no - 1, 10) < 3 THEN NULL
            ELSE MOD(i.item_no * 17, 301) / 1000 END
FROM items AS i JOIN products AS p
  ON p.product_id = 1 + MOD(i.order_id * 7 + i.line_no * 13 - 1, 150);

ANALYZE TABLE customers, products, orders, order_items;
GRANT SELECT ON `retail`.* TO 'source_reader'@'%';
