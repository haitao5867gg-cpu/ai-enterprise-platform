# 数据工程节点、应用说明书和 ETL 演示应用

本文是 [flow-apps.md](flow-apps.md) 的补充：新增 5 种「数据工程」节点，把自然语言 ETL 做成编排应用；新增节点的讲解说明 `note` 和应用说明书接口；新增连接器工具 `read_rows`；新增演示用的零售示例库、两个知识库、应用模板和 `make demo`。设计文档：https://claude.ai/artifact/K45W27BX8CVu7Lzh1HYtC2

代码应用 `etl` 保持不变。新节点复用它的实现（`aep.etl.profile`、`aep.etl.plan`、`aep.etl.codegen`、`aep.databricks`），不另写一套规则。

## 节点公共字段：讲解说明 `note`

所有节点类型都可以写：

```jsonc
{"note": "平台按口径知识库检索指标定义，后面出方案时引用。"}   // 可选，最多 500 字符，原样保存，不做变量替换
```

- 只给人看，不参与运行。编辑页的节点表单里在标题下面编辑；画布节点详情、运行页流程视图的节点详情里显示（`design.note`）。
- 导出导入照常带上。

## 循环规则放宽

保存检查“每个循环至少经过一个人工、条件或分类节点”改为：**每个循环至少经过一个有分支的节点**（人工、条件、分类，以及本文的 `etl_plan_check`、`etl_code_check`、`databricks_dev_run`）。`on_error` 的 `error` 分支不算。提示文字改为“这个循环里没有能决定离开循环的节点（人工确认、条件判断、意图分类或带分支的校验节点），会一直循环下去”。

## 节点类型清单

`GET /node-types` 的 `category` 新增 `"data"`（前端显示“数据工程”）。5 种新节点都是 `category: "data"`、`error_handling: true`；流程视图的 `kind`：`etl_profile`、`databricks_ingest`、`databricks_dev_run` 为 `tool`，`etl_plan_check`、`etl_code_check` 为 `code`。

出错类型新增 `databricks_error`：没配置 Databricks 工作区，或 Databricks 接口、集群命令报错。连不上数据源、连接器报错仍是 `tool_error`。

平台没有配置 Databricks（`AEP_DATABRICKS_HOST`、`AEP_DATABRICKS_TOKEN`）时，两个 `databricks_*` 节点照样能保存，运行到它们时以 `databricks_error` 失败，`error_message` 写明缺哪个配置。抽取和结果预览还需要 `AEP_DATABRICKS_CLUSTER_ID` 和 `AEP_DATABRICKS_SOURCE_CATALOG`。

下面每种节点都写了设置、产出和分支。凡是写“整段引用”的设置，值恰好是一个引用（如 `"{{profile.tables}}"`）时传原始值，规则同工具参数。

### etl_profile 探查数据

```jsonc
{"datasource": "{{start.datasource}}",   // datasource_id 或整段引用，规则同 tool 节点
 "tables": "{{start.tables}}",           // 字符串或字符串数组，可以是整段引用；见下
 "sample_rows": 5}                       // 1–20
```

- `tables`：要探查哪些表。字符串按逗号、空白、换行拆开；每项是 `schema.table` 或 `schema.*`（这个 schema 下的全部表）。为空或空数组时探查数据源里的全部表。展开后超过 20 张表（`MAX_TABLES`）时失败，`tool_error`，提示指定表。
- 执行同 `aep.etl.profile.profile_tables`。

产出：

| 字段 | 内容 |
| --- | --- |
| `source_id` | 数据源的 `datasource_id` |
| `source_name` | 数据源名称 |
| `tables` | 每张表的探查结果，同 `profile_tables` 的返回值 |
| `names` | 表名列表，`["retail.orders", …]` |
| `description` | 给模型看的文字，同 `describe_for_model` |
| `summary` | 一句话，如“已探查 MySQL 示例库 的 4 张表：retail.customers、…” |

无分支。

### etl_plan_check 校验方案

```jsonc
{"reply": "{{plan.text}}",            // 模型的回答（必填）；按 llm.parse_json 解析成方案
 "profile": "{{profile.tables}}",     // 整段引用，etl_profile 的 tables
 "hits": ["{{glossary.hits}}"],       // 0–5 个整段引用，知识检索节点（或 agent）的 hits；用来把 [n] 换成依据
 "language": "{{start.language}}",    // 可选；值为 pyspark 或 sql 时覆盖方案里的 language
 "max_attempts": 3}                   // 1–5
```

- 执行：解析 `reply`，用 `aep.etl.plan.validate_plan` 校验，`source_tables` 是 `profile` 的表名，`known` 是 `known_values(profile)`，`references` 是 `hits` 合起来（按 `index` 去重）。`trust_assumptions`：这个节点上一次是 `retry` 时为 true，否则为 false（同代码应用：第一次让模型先看一眼真实取值）。
- 解析失败或校验不过走 `retry`；**连续** `max_attempts` 次不过时节点失败，`error_type` 为 `output_format`，`error_message` 是最后一次的问题。走过一次 `ok` 后连续次数清零（比如人驳回后重写）。

产出：

| 字段 | 内容 |
| --- | --- |
| `ok` | 是否通过 |
| `plan` | 通过时是校验后的方案（`EtlPlan` 的 JSON，含 `references`）；不通过时是解析出的原始 JSON，解析不了为 `null` |
| `problems` | 不通过的原因，一条一行；通过时为 `""` |
| `message` | 给人看的方案，Markdown：摘要；每张目标表的名称、加载方式、来源、做法、列映射表格、主键、质量规则；需要确认的假设；“依据的资料”列出 `[n]`《文件名》章节。不通过时是问题列表 |
| `sources` | 方案用到的源表（`schema.table`，不含其他目标表），去重、按名称排序 |
| `targets` | 目标表名列表 |

分支：`ok`（通过）、`retry`（重写）。

### databricks_ingest 抽取到湖仓

```jsonc
{"datasource": "{{start.datasource}}",
 "tables": "{{check_plan.sources}}",   // 写法同 etl_profile 的 tables，但不能为空
 "max_rows": 200000}                   // 每张表最多几行，1–1000000；超过时失败
```

- 目标：`<AEP_DATABRICKS_SOURCE_CATALOG>.<schema>.<table>`，名字和源表一样，所以生成的代码（按 `source_catalog` 读源表）直接读到。schema 不存在时创建；表已存在时**整表覆盖**。
- 步骤（每张表）：
  1. `describe_table` 拿列和类型，再用连接器的 `read_rows` 分页读出全部行（每页 5000）。行数超过 `max_rows` 时失败，不写任何东西。
  2. 写成 JSON Lines，用 Databricks Files API 上传到 Volume `/Volumes/<catalog>/<schema>/<AEP_DATABRICKS_INGEST_VOLUME>/<datasource_id>/<run_id>-<随机 8 位>/<table>.jsonl`（每次抽取一个文件夹，两个运行同时抽同一张表也不会读到对方的文件；Volume 默认名 `aep_landing`；schema 和 Volume 不存在时用 Unity Catalog REST API 创建，Volume 为 MANAGED）。
  3. 在 `AEP_DATABRICKS_CLUSTER_ID` 集群上用命令执行接口建表：所有列先按字符串读，再按下面的类型对照转换，`CREATE OR REPLACE TABLE`；表注释写“由 AEP 从数据源「名称」抽取，运行 run_id，时间”，表属性 `aep.datasource_id`、`aep.run_id`。
  4. 建表后（成功或失败）删掉这次的落地文件和文件夹；删不掉时只在 `warnings` 里写明。
- 类型对照（按源列 `data_type` 小写匹配）：数组（以 `[]` 结尾，如 PostgreSQL 的 `integer[]`）→ STRING（存数组的 JSON 文本）；`tinyint smallint mediumint int integer` → INT，其中 `int unsigned` → BIGINT；`bigint` → BIGINT，`bigint unsigned` → DECIMAL(20,0)；`decimal(p,s) numeric(p,s)` → DECIMAL(p,s)（没有精度时 DECIMAL(38,10)）；`float real double` → DOUBLE；`bool boolean` → BOOLEAN；`date` → DATE；`datetime timestamp*` → TIMESTAMP；其他 → STRING。
- 新配置 `AEP_DATABRICKS_INGEST_VOLUME`（默认 `aep_landing`）。

产出：

| 字段 | 内容 |
| --- | --- |
| `catalog` | 写入的 catalog |
| `tables` | `[{"source": "retail.orders", "target": "`icsd-ace-dev`.retail.orders", "rows": 15000, "columns": 7}]` |
| `summary` | 一句话，如“已把 4 张表共 52350 行抽取到 Databricks：…” |

无分支。不支持单节点试跑（会写 Databricks），`POST /apps/{app}/nodes/{node}/test` 返回 422（同 start、human、end）。

### etl_code_check 检查并打包

```jsonc
{"reply": "{{codegen.text}}",          // 必填；解析为 {"targets": {"目标表": "代码"}}，同代码应用
 "plan": "{{check_plan.plan}}",        // 整段引用，校验通过的方案
 "source_name": "{{profile.source_name}}",
 "name": "{{start.name}}",             // 可选，bundle 名；为空或不合法时用第一张目标表的名字，规则同代码应用
 "max_attempts": 3}                    // 1–5，连续不过的次数上限，规则同 etl_plan_check
```

- 执行：`codegen.check_code`；通过后 `codegen.render`（compute 和 job 模式按平台配置，同代码应用的 `build`）再 `codegen.check_bundle`。

产出：

| 字段 | 内容 |
| --- | --- |
| `ok` | 是否通过 |
| `code` | `{"目标表": "代码"}`，解析不了时为上一次的值或 `{}` |
| `problems` | 一条一行，通过时为 `""` |
| `bundle` | 通过时 `{"name", "language", "files": {"路径": "内容"}}`，否则 `null` |
| `summary` | 如“已生成 Databricks bundle dws_retail_sales_city_month：databricks.yml、…” |

分支：`ok`（通过）、`retry`（重写）。

### databricks_dev_run dev 运行

```jsonc
{"bundle": "{{check_code.bundle}}",   // 整段引用
 "max_runs": 3,                        // 1–3，同代码应用的 max_dev_runs
 "preview_rows": 20}                   // 0–100；0 不预览
```

- 执行：`DevTarget.run`，同代码应用的 verify。通过后读每张结果表的前 `preview_rows` 行：`job`、`pipeline` 模式在 `AEP_DATABRICKS_CLUSTER_ID` 集群上读结果表（没有集群时不预览，`warnings` 里写明）；`cluster` 模式由试跑程序直接带回转换结果的前几行。同名 bundle 的运行排队进行，读预览也在队内：后一个运行不会在前一个读完预览之前改写结果表。
- 结果表全名：`job`、`pipeline` 模式是 `<AEP_DATABRICKS_CATALOG>.<AEP_DATABRICKS_SCHEMA>.<目标表>`；`cluster` 模式不落表，预览取转换结果的前几行。

产出：

| 字段 | 内容 |
| --- | --- |
| `status` | `passed` 或 `failed` |
| `run` | `DevRun.as_dict()`（不含 `preview`），另加 `attempt`（这是第几次运行）；`catalog`、`schema` 是结果表写在哪（`job` 模式取 Job 的参数，`pipeline` 模式取平台配置，不知道时为 `null`） |
| `url` | Databricks 上这次运行的链接，没有时为 `null` |
| `tables` | `[{"name", "target", "rows", "dropped", "expectations"}]` |
| `preview` | `[{"table", "target", "columns": ["…"], "rows": [{…}]}]`，值按 JSON 转换（日期为 `yyyy-MM-dd`，时间为不带时区的 `yyyy-MM-dd HH:mm:ss`，和源库里的写法一样；小数为数字） |
| `errors` | 失败时的报错，一条一行，每条最多 1500 字符；通过时为 `""` |
| `report` | Markdown 报告：成功或失败、运行方式、链接；每张结果表的行数、被丢弃行数、质量规则结果；每张表的预览表格（最多 10 列，超出的列省略并注明）。表全名带反引号（如 `` `icsd-ace-dev`.sales.orders ``），在 Markdown 里写成双反引号的行内代码 |

分支：`passed`（通过）、`retry`（失败且 `fixable`，且这是第 `max_runs` 次以前）、`failed`（其他失败）。显示文字：通过、带着报错重写、失败。运行次数在这次运行里累计，不随回到写代码清零。

不支持单节点试跑。

## 连接器新工具 `read_rows`

MySQL、Postgres、上传文件三个连接器都加，只读：

```jsonc
// 参数
{"schema": "retail", "table": "orders", "offset": 0, "limit": 5000}   // offset ≥ 0；limit 1–5000，默认 1000
// 返回
{"schema": "retail", "table": "orders", "columns": ["order_id", "…"],
 "rows": [{"order_id": 1, "…": "…"}], "next_offset": 5000}            // 读完时 next_offset 为 null
```

- 按主键排序（没有主键时按全部列），保证分页稳定。值的编码和 `sample_rows` 一致。
- 权限和表名校验同其他工具，标识符一律校验或加引号，`offset`、`limit` 用绑定参数。

## 应用说明书

```
GET /apps/{app}/manual              当前生效版本
GET /apps/{app}/manual?version=3    指定版本
GET /apps/{app}/manual?draft=true   草稿（没有草稿时 404）
GET /runs/{id}/manual               这次运行用的版本
```

从配置里整理出“这个应用用了什么”，编排应用和代码应用都支持：

```jsonc
{
  "app": "retail-etl", "title": "零售数据 ETL", "summary": "……",
  "kind": "flow",                  // 或 "code"
  "version": 3,                    // 草稿时为 null
  "steps": [                       // 按流程顺序：从开始节点广度优先，回到已出现节点的边不再展开；代码应用按 NODES 的顺序
    {"id": "plan", "title": "出方案", "type": "llm", "type_label": "调用模型", "kind": "model",
     "note": "……",                 // 编排应用的 note；代码应用为 NodeDesign.summary
     "uses": ["Skill etl-plan v2", "模型 company"]}
  ],
  "skills": [{"name": "etl-plan", "version": 2, "description": "…", "nodes": [{"id": "plan", "title": "出方案"}]}],
  "knowledge_bases": [{"kb_id": "…", "name": "零售数据口径", "description": "…", "document_count": 6,
                       "documents": [{"document_id": "…", "filename": "01-有效订单定义.md", "status": "ready"}],
                       "nodes": [{"id": "glossary", "title": "查业务口径"}]}],
  "datasources": [{"datasource_id": "ds_…", "name": "MySQL 示例库", "kind": "mysql", "nodes": [...]},
                  {"datasource_id": null, "name": "运行时选择（开始节点的「数据源」）", "kind": null, "nodes": [...]}],
  "models": [{"alias": "company", "nodes": [...]}],          // null（平台默认模型）显示为默认模型的别名
  "systems": [{"key": "databricks", "name": "Databricks",
               "detail": "工作区 adb-…；源表 catalog icsd-ace-dev；结果写到 icsd-ace-dev.trac_b_test；集群 1204-…",
               "nodes": [...]}],                               // 用到 databricks_* 节点或代码应用的 verify 时才有；没配置时 detail 写“未配置”；代码应用的提交资产、上线审批另有 {"key": "assets", "name": "资产仓库（GitHub）"}
  "human": [{"id": "review", "title": "确认方案", "prompt": "…"}]
}
```

- 代码应用用到的数据源是运行时填写的，列为 `{"datasource_id": null, "name": "运行时选择（运行时填写的数据源）"}`。
- 用到的范围：编排应用看节点设置（llm、classifier、agent 的 `skills`、`model`；knowledge 节点和 agent 的 `knowledge`；tool、agent、`etl_profile`、`databricks_ingest` 的数据源；human 节点）；代码应用看 `CONFIG` 里每个节点的 skills、模型和 `knowledge_bases` 类型的参数。
- 已删除的 Skill 版本或知识库照样列出，`description` 写“已删除”。不出现密钥的值。

## 前端

- 「添加节点」多一组“数据工程”；5 种节点各有设置表单，引用字段用已有的变量选择器。
- 节点表单在标题下面多一个“讲解说明”文本框（`note`）。
- 画布节点上用小标签显示它用到的 Skill、知识库、数据源或 Databricks（来自说明书的 `steps[].uses`）；节点详情显示 `note`。
- 应用页（编排应用和代码应用）新增“应用说明书”面板，运行页新增可展开的同名面板（用 `GET /runs/{id}/manual`）：节点讲解（按 `steps` 顺序的编号列表）、Skill（链接到 Skill 页）、知识库（文档列表，链接到知识库页）、数据源、模型、外部系统、人工确认点。
- 运行结果里的字符串按 Markdown 显示（报告里的表格要能显示成表格）；人工节点的 `show` 已经按 Markdown 显示。

## 演示：零售示例库、知识库、模板和 `make demo`

### 零售示例库

`connectors/mysql/seed/003-retail.sql`：建库 `retail` 并给 `source_reader` 只读权限（`GRANT SELECT ON retail.*`），数据用递归 CTE 和取模确定性生成，每次一样。

| 表 | 行数 | 列 |
| --- | --- | --- |
| `customers` | 1200 | `customer_id` bigint 主键, `name` varchar, `city` varchar, `member_level` varchar, `registered_at` datetime |
| `products` | 150 | `product_id` bigint 主键, `name` varchar, `category` varchar, `list_price` decimal(12,2), `cost_price` decimal(12,2) |
| `orders` | 15000 | `order_id` bigint 主键, `customer_id` bigint, `channel` varchar, `status` varchar, `ordered_at` datetime, `shipping_fee` decimal(10,2) |
| `order_items` | 约 36000 | (`order_id`, `line_no`) 主键, `product_id` bigint, `quantity` int, `unit_price` decimal(12,2), `discount_rate` decimal(4,3) |

- 城市 8 个：上海、北京、杭州、南京、苏州、深圳、广州、成都。约 5% 写成“上海市”这种带“市”的、约 5% 前后带空格、约 3% 为空。
- 会员等级 `normal`、`silver`、`gold`，约 5% 大小写不一（`Gold`、`SILVER`），约 2% 为空。
- 5 个测试账号：`name` 以“测试”开头（“测试账号1”…），各有若干订单。
- 品类 5 个：家电、数码、个护、食品、家居，约 3% 为空。
- 渠道：`online`、`store`、`mini_program`。
- 状态：`paid`、`shipped`、`completed` 占约 85%，`cancelled` 约 8%，`refunded` 约 5%；另有约 2% 写成 `' Paid '`、`'COMPLETED'` 这种。
- `ordered_at` 从 2025-11-01 到 2026-10-31，每月订单量有起伏：11 月和 1 月明显高。
- 明细每单 1–4 行；约 1% 的 `quantity` 为 0 或负数；`discount_rate` 0–0.3，约 30% 为空（表示不打折）。
- 文件开头说明这些脏数据是故意的，和两份知识库的口径对应。

`docker-compose.yml` 里 `MySQL 示例库` 的说明改为“本地 MySQL 示例源库：sales（客户、商品、订单）和 retail（零售订单，演示用），都含脏数据”。

### 两个知识库

文档放在 `demo/knowledge/<知识库目录>/`，Markdown，每篇 1–3 屏，中文，写成企业内部文档的样子（有标题、适用范围、版本和维护部门）。口径里的取值必须和示例库完全一致（`paid`、`测试`、`gold` 等）。

- `retail-metrics/`，知识库名“零售数据口径”，说明“零售业务的指标定义和统计口径：有效订单、销售额、客单价、城市和会员等级的归一、时间口径、客户数和复购、毛利”。文档：`01-有效订单定义.md`、`02-销售额与客单价.md`、`03-城市与会员等级.md`、`04-时间口径.md`、`05-客户数与复购.md`、`06-毛利.md`，内容见设计文档“两个知识库”一节。
- `dw-standards/`，知识库名“数仓开发规范”，说明“数据平台团队的数仓开发规范：分层和命名、主键和字段类型、数据质量规则、加载策略、PySpark 写法”。文档：`01-分层与命名.md`、`02-主键与字段.md`、`03-数据质量规则.md`、`04-加载策略.md`、`05-PySpark编码规范.md`。PySpark 规范必须和 `aep.etl.codegen` 的静态检查一致，不能建议检查会拒绝的写法。

### 应用模板 `etl-lakehouse`

`TEMPLATES` 新增 `etl-lakehouse`，标题“数据 ETL（湖仓）”，说明“探查数据源、按口径知识库出方案、经人确认后抽取到 Databricks，按开发规范写 PySpark 代码并在 dev 运行，预览结果数据”。`needs` 为 `("datasource", "knowledge_base")`，新建时和 `knowledge-qa` 一样走“版本 1 空白、模板配置进草稿”。

节点（每个都写好 `note`）：

| id | 类型 | 标题 | 主要设置 |
| --- | --- | --- | --- |
| `start` | start | 开始 | 输入 `datasource`（数据源，必填）、`request`（需求，textarea，必填）、`tables`（表范围，text）、`language`（select：pyspark、sql，默认 pyspark）、`name`（bundle 名，text） |
| `profile` | etl_profile | 探查数据 | `datasource: {{start.datasource}}`，`tables: {{start.tables}}` |
| `glossary` | knowledge | 查业务口径 | `query: {{start.request}}`，`top_k` 6，混合检索；知识库留空 |
| `plan` | llm | 出方案 | Skill `etl-plan`（内置 Skill 的最新发布版本），`system` 为 `aep.etl.plan.OUTPUT_FORMAT`，输出 text，`max_visits` 10；提示词见下 |
| `check_plan` | etl_plan_check | 校验方案 | `reply: {{plan.text}}`，`profile: {{profile.tables}}`，`hits: ["{{glossary.hits}}"]`，`language: {{start.language}}`，`max_visits` 10 |
| `review` | human | 确认方案 | 确认模式，`show: {{check_plan.message}}`，选项确认、驳回，驳回必须写意见，`max_visits` 5 |
| `ingest` | databricks_ingest | 抽取到湖仓 | `datasource: {{start.datasource}}`，`tables: {{check_plan.sources}}` |
| `standards` | knowledge | 查开发规范 | 固定的 `query: "表命名、主键、字段类型、数据质量规则、加载策略、PySpark 写法"`（规范每次都要查全，不随需求变），`top_k` 8，混合检索；知识库留空 |
| `codegen` | llm | 写代码 | Skill `etl-codegen`，`system` 为 `aep.etl.codegen.OUTPUT_FORMAT`，输出 text，`max_visits` 10；提示词见下 |
| `check_code` | etl_code_check | 检查并打包 | `reply: {{codegen.text}}`，`plan: {{check_plan.plan}}`，`source_name: {{profile.source_name}}`，`name: {{start.name}}`，`max_visits` 10 |
| `dev_run` | databricks_dev_run | dev 运行 | `bundle: {{check_code.bundle}}`，`max_visits` 3 |
| `end` | end | 结束 | 输出 `report` ← `{{dev_run.report}}`，`plan` ← `{{check_plan.plan}}`，`ingested` ← `{{ingest.summary}}` |

连线：start → profile → glossary → plan → check_plan；check_plan `ok` → review，`retry` → plan；review `approve` → ingest，`reject` → plan；ingest → standards → codegen → check_code；check_code `ok` → dev_run，`retry` → codegen；dev_run `passed` → end，`retry` → codegen，`failed` → end。

`plan` 的提示词：

```
需求：{{start.request}}
期望的代码语言：{{start.language}}

源表（只能使用这些）：
{{profile.description}}

公司数据口径（参考资料）：
{{glossary.context}}

需求里的指标和口径资料有关时按资料定义，并在 summary、assumptions、目标表和字段说明里用 [n] 标注依据；没有资料依据的内容不要编号。资料和需求冲突时以需求为准，并写进 assumptions。

上一版方案（第一次出方案时为空，忽略）：{{check_plan.plan}}
平台校验发现的问题（为空时忽略）：{{check_plan.problems}}
用户的修改意见（为空时忽略）：{{review.comment}}
有上一版方案时，在它的基础上按问题和意见修改，输出完整方案。
```

`codegen` 的提示词：

```
已确认的方案：
{{check_plan.plan}}

源表：
{{profile.description}}

开发规范（参考资料）：
{{standards.context}}

上一版代码（第一次写时为空，忽略）：{{check_code.code}}
静态检查发现的问题（为空时忽略）：{{check_code.problems}}
Databricks dev 运行的报错（为空时忽略）：{{dev_run.errors}}
有上一版代码时，按问题和报错修改，输出全部目标表的完整代码。
```

### 演示应用和 `make demo`

`make demo` 一次准备好演示环境，可以重复执行，结果不变：

1. **检查**：API 能访问；模型网关里有 `AEP_DEMO_MODEL`（默认 `company`）这个别名；平台配了 Databricks 的 host、token、`AEP_DATABRICKS_CLUSTER_ID`、`AEP_DATABRICKS_SOURCE_CATALOG`。逐项打印 ✓ 或 ✗ 和缺什么；缺 Databricks 时只警告（应用照样建好，跑到抽取会失败）。新接口 `GET /health/demo` 返回这些检查项 `[{"key", "ok", "detail"}]`，不返回密钥。
2. **示例库**：MySQL 里没有 `retail` 库时，执行 `docker compose exec -T source-mysql sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot' < connectors/mysql/seed/003-retail.sql`（新卷会在初始化时自动执行这个文件，已有的卷靠这一步补）。
3. **知识库**：按名称找，没有就建；`demo/knowledge/` 里的文件按文件名对比，缺的上传、大小或内容变了的删掉重传、多余的不动；等全部文档处理完成（最多 5 分钟）。
4. **应用**：应用 id `retail-etl`，标题“零售数据 ETL”。不存在时用模板 `etl-lakehouse` 新建；把两个知识库填进 `glossary`、`standards`，`plan`、`codegen` 的模型设为 `AEP_DEMO_MODEL`，开始节点的默认值：数据源为“MySQL 示例库”，`tables` 为 `retail.*`，`request` 为设计文档里的演示需求；保存草稿并发布。生效版本的配置已经一样时不再发新版本。
5. 最后打印应用页、两个知识库页的地址和演示需求。

实现为后端包里的脚本 `aep.demo`（`uv run aep-demo --api http://localhost:8000`，只调平台接口，不直接连数据库），Makefile 的 `demo` 目标先跑第 2 步再调它。

## 测试

- 后端：5 种节点各自的单元测试（假连接器、假 `DevTarget`、`FakeGateway`）；模板 `etl-lakehouse` 用假模型从头跑到 `end`（含一次方案驳回、一次代码检查不过、一次可修的 dev 报错）；循环规则；说明书接口（编排应用和 `etl`）；`read_rows` 分页和权限（三个连接器）；`aep.demo` 用假 API 测幂等。
- 不连真实 Databricks 和真实模型；`DevTarget.ingest`、`preview` 用假 HTTP 测请求内容。
- 前端：类型检查通过；说明书面板和新节点表单有 Playwright 用例（沿用现有的 mock 方式）。
