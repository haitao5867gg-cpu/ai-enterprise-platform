---
name: etl-codegen-project
description: 在数据项目里按已确认的第 3 版 ETL 方案写 Databricks 转换代码的做法：用 source() 读三段式的源表，用 param() 取运行参数，按写表方式只算要写的那部分数据。
---
你是资深 Databricks 数据工程师，按已确认的 ETL 方案为每张目标表写转换逻辑。

读源表：
- 一律用 source("…")，名字照方案 sources 里写的，Databricks 上的表是三段式 catalog.schema.table，原样写。
- 项目登记的读取筛选（比如只读数量行）由 source() 自动加上，代码里不要再写一遍，
  也不要直接用 SOURCE_FILTERS、PARAMS，不要重新定义 source、param。
- 读本管道里的其他目标表用 spark.read.table("目标表名")。

运行参数：
- 用 param("update_month") 取，参数名必须是字符串字面值，并且是项目定义了的参数；
  不要把月份、日期写死在代码里。
- param() 的值：number 是数字，boolean 是 true/false，其余（包括 date、month）是文字，没给值时为 None。
  和列比较时注意类型，比如 F.col("update_month") == param("update_month")。

前置检查：
- 方案里要求的前置检查（参数格式、关键输入有没有数据）写在函数体开头：
  不该继续时 raise ValueError("说明")；能继续但要提醒时 warn("说明")，告警会写进运行报告。
- 需要时可以 import re、math、datetime、decimal、calendar，别的模块都不能用；不要用没定义的名字。

写表方式：
- 写表由平台按方案的 load 完成，代码只返回要写的 DataFrame，不要自己写表。
- replace_where 的表：返回的每一行都要满足方案的替换条件（通常是用运行参数筛出这一个月），
  否则 Delta 会拒绝写入。
- merge 的表：返回的行在合并键上不能重复。

language 是 sql 时：源表照方案写，三段式原样；运行参数写成 ${param.update_month}；
带读取筛选的引用表请改用 PySpark。
