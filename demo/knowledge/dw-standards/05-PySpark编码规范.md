# PySpark 编码规范

- 适用范围：平台生成和人工编写的 Lakeflow 管道 PySpark 代码
- 版本：v2.2（2026-09-25 更新）
- 维护部门：数据平台组
- 说明：本规范与平台的代码静态检查一致，检查不通过的代码不能部署。
- 要点：每张目标表写一个最后 `return` DataFrame 的函数体；源表用 `source("retail.orders")` 读，其他目标表用 `spark.read.table("表名")` 读；不写 `import`，不写表，不拉数据到 driver，不用 `spark.sql` 和 Python UDF。

## 代码结构

- 每张目标表的代码是一个函数体（不写 `def` 那一行），最后 `return` 一个 DataFrame。
- 不写 `import`：`F`（`pyspark.sql.functions`）和 `Window` 已经可以直接用。
- 读源表只用 `source("schema.table")`，如 `source("retail.orders")`，而且只能读方案里列出的源表。
- 读同一个管道里的其他目标表用 `spark.read.table("目标表名")`，如汇总表读 `spark.read.table("dwd_retail_order_detail")`。
- 质量规则由平台按方案生成，函数体里不要再写过滤坏数据的检查。

## 禁止的写法

| 不要写 | 原因 |
| --- | --- |
| `.write`、`.writeStream`、`.saveAsTable`、`.insertInto`、`.save` | 写表由管道负责，代码只做读和转换 |
| `.collect()`、`.toPandas()` | 把数据拉到 driver，数据量大时会内存溢出 |
| `spark.sql(...)` | 不执行任意 SQL；用 DataFrame API 或 `F.expr` |
| `dbutils`、`os`、`sys`、`subprocess`、`open`、`eval`、`exec`、`__import__` | 不访问文件系统和运行环境，不动态执行代码 |
| Python UDF | 比内置函数慢得多，用 `F` 里的函数组合 |

## 推荐写法

- 字符串清洗：`F.lower(F.trim(c))`、`F.regexp_replace(c, "市$", "")`，空字符串先用 `F.nullif(c, F.lit(""))` 变成空值，再 `F.coalesce(..., F.lit("未知"))` 给默认值。`F.nullif`、`F.coalesce` 的参数都是列，常量必须包一层 `F.lit`：直接写 `""` 会被当成列名，运行时报 `UNRESOLVED_COLUMN`。
- 关联时写清楚条件和方式（`"inner"`、`"left"`），用 `.alias()` 给表起别名；关联后用 `select` 明确列出要的列，避免两张表的同名列（如客户和商品的 `name`）冲突。
- 空的折扣率：`F.coalesce(F.col("discount_rate"), F.lit(0))`。
- 金额：计算后 `F.round(x, 2).cast("decimal(18,2)")`。
- 汇总：`groupBy(...).agg(...)`，去重计数用 `F.countDistinct`。

## 例子：订单明细表

```python
orders = source("retail.orders").alias("o")
items = source("retail.order_items").alias("i")
customers = source("retail.customers").alias("c")
valid = (
    orders.join(customers, F.col("o.customer_id") == F.col("c.customer_id"), "inner")
    .where(F.lower(F.trim(F.col("o.status"))).isin("paid", "shipped", "completed"))
    .where(~F.col("c.name").startswith("测试"))
)
lines = valid.join(items, F.col("o.order_id") == F.col("i.order_id"), "inner")
amount = (
    F.col("i.quantity")
    * F.col("i.unit_price")
    * (F.lit(1) - F.coalesce(F.col("i.discount_rate"), F.lit(0)))
)
return lines.select(
    F.col("o.order_id").alias("order_id"),
    F.col("i.line_no").alias("line_no"),
    F.col("o.customer_id").alias("customer_id"),
    F.col("o.ordered_at").alias("ordered_at"),
    F.col("i.quantity").alias("quantity"),
    F.round(amount, 2).cast("decimal(18,2)").alias("sales_amount"),
)
```

## 例子：按月汇总表

```python
detail = spark.read.table("dwd_retail_order_detail")
return (
    detail.groupBy(F.date_format("ordered_at", "yyyy-MM").alias("stat_month"))
    .agg(
        F.sum("sales_amount").cast("decimal(18,2)").alias("sales_amount"),
        F.countDistinct("order_id").alias("order_count"),
    )
)
```

如果选的代码语言是 SQL，每张表写一条 `SELECT`（可以用 `WITH`），源表写成 `retail.orders`，其他目标表直接写表名，不写 `INSERT`、`CREATE` 等语句。
