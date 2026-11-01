# 项目、运行参数和数据外发策略

后端和前端按这份约定并行开发。字段名 snake_case，时间是 ISO 8601 字符串。错误格式同 [datasources.md](datasources.md)：`{"detail": "中文说明"}`；请求体不合格返回 422，`detail` 是一句中文。

「项目」是平台资源，不属于某个应用：任何应用的运行都可以绑定一个项目，节点按项目的配置读写数据。数据外发策略管发给模型的数据，有平台默认值，项目可以另设。设计文档：https://claude.ai/artifact/CCPMycsWqgaPzY3iYCMywK （“平台和应用的分工”一节）。

第一期只做项目的环境、表名前缀、表登记、运行参数和数据外发策略。代码仓库、工程布局、检查规则在第二期加，到时另写进本文；现在的接口不接受这些键（多出来的键返回 422）。

## 概念

- **环境**：项目在 Databricks 上写表的位置，每个环境一个 catalog 和 schema，可以指定集群。第一个环境是默认环境。工作区还是平台配置的那一个（`AEP_DATABRICKS_HOST`、`AEP_DATABRICKS_TOKEN`），按环境分工作区以后再加。
- **表名前缀**：项目写的每张表都要以它开头，和同一个 schema 里别的表分开。
- **表登记**：
  - `references`：项目只读引用的表，三段式名，可以跨 catalog。每张可以写一个读取筛选（比如只读数量行），平台读这张表时一律带上它（见 [data-nodes.md](data-nodes.md) 第三批）。
  - `adopted`：已经存在、归本项目所有的表。平台建的表会打上项目标记，不用登记；在平台之外建好的表要登记在这里，平台才肯写。
- **写表规则**：绑定项目的运行只能写当前环境的 `<catalog>.<schema>` 下、以前缀开头的表；表已经存在时，它必须带本项目的标记（表属性 `aep.project`），或者登记在 `adopted` 里。别的一律拒绝，不会覆盖不属于本项目的表。
- **运行参数**：项目定义一组参数（比如统计月、版本号）。新建运行时填值，节点里用 `{{param.<key>}}` 引用，生成的代码用 `param("<key>")` 读，所有入口的默认值都取这里，不会各写一份。
- **数据外发策略**：允许用哪些模型，以及从数据源读出来的值以什么程度发给模型，见“数据外发策略”。

## 数据结构

```jsonc
// Project
{
  "project_id": "jade-ibf",              // 2–40 个字符：小写字母开头，小写字母、数字和 -；建好后不能改
  "name": "IBF 入库运费",                 // 1–100 字符，去掉首尾空白；不能和别的项目重名
  "description": "……",                    // 可选，最多 2000 字符，默认 ""
  "table_prefix": "ibf_",                 // 2–30 个字符：小写字母开头，小写字母、数字和 _
  "environments": [Environment],          // 1–10 个，key 不重复；第一个是默认环境
  "references": [Reference],              // 0–100 个，name 不重复
  "adopted": ["ibf_table1_cw"],           // 0–200 个表名（不含 catalog、schema），都要以 table_prefix 开头，不重复
  "params": [Param],                      // 0–20 个，key 不重复
  "policy": Policy,                       // 见“数据外发策略”
  "created_at": "……",
  "updated_at": "……"
}

// Environment
{
  "key": "dev",                           // 规则同编排应用的节点 id：小写字母开头，小写字母、数字和 _，1–40
  "label": "开发",                         // 1–50 字符
  "catalog": "icsd-ace-dev",              // 1–255 字符，Unity Catalog 名，原样保存（带 - 的名字由平台加反引号）
  "schema": "jade_dev",                   // 1–255 字符
  "cluster_id": null                      // 可选，在哪个现有集群上运行和读表；null 用平台的 AEP_DATABRICKS_CLUSTER_ID
}

// Reference
{
  "name": "prev_catalog.sales.forecast",  // 三段式 catalog.schema.table，每段 1–255 字符，不含反引号和空白
  "label": "预测表",                       // 可选，1–50 字符，默认取表名最后一段
  "filter": "Type = 'Vol.'",              // 可选，读取筛选：一个 Spark SQL 布尔表达式，最多 2000 字符，默认 null
  "description": "上一期的需求预测，金额和数量各占一半"   // 可选，最多 2000 字符，默认 ""
}

// Param
{
  "key": "update_month",                  // 规则同 Environment.key
  "label": "统计月",                       // 1–50 字符
  "type": "text" | "number" | "boolean" | "date" | "month" | "select",
  "required": true,                       // 默认 false
  "default": "2026-10",                   // 可选，默认 null；有值时要符合类型
  "choices": [{"value": "a", "label": "A"}],   // 只用于 select，1–50 个，value 不重复
  "description": "按自然月，C+3 当天跑上个月"     // 可选，最多 500 字符
}

// Policy
{
  "samples": "full" | "masked" | "stats" | "schema" | null,   // null：用平台默认
  "models": ["company"] | null                                // 允许的模型别名，1–20 个；null：用平台默认
}
```

取值规则：

- `filter` 只做形状检查：不能有分号、注释（`--`、`/*`），不能出现 `SELECT` 关键字（不分大小写，按整词匹配），括号要配对。它会原样拼进 `WHERE (…)`，语义由 Databricks 判断，写错了在读表时报错。
- 参数值：`text` 1–500 字符；`number` 是 JSON 数字；`boolean` 是 true 或 false；`date` 是 `YYYY-MM-DD`；`month` 是 `YYYY-MM`；`select` 是 `choices` 里的某个 `value`。
- `models` 里的别名必须在模型网关上（同编排应用保存检查用的模型清单）；网关读不到清单时不检查。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/projects` | 项目列表，按名称排序：`[ProjectSummary]` |
| POST | `/projects` | 新建，请求体是 `Project` 去掉 `created_at`、`updated_at`；返回 201 和 `Project` |
| GET | `/projects/{project_id}` | `Project`；没有时 404“没有项目 …” |
| PUT | `/projects/{project_id}` | 整体替换，请求体同新建，`project_id` 要和路径一致；返回 `Project` |
| DELETE | `/projects/{project_id}` | 删除，返回 204；没有时 404 |
| GET | `/projects/options` | 新建表单用：`{"sample_levels": [{"value": "full", "label": "完整样本", "description": "…"}, …], "models": ["company", …] | null, "defaults": {"samples": "full", "models": null}}`，`models` 是网关上的别名（读不到为 null），`defaults` 是平台默认策略 |

```jsonc
// ProjectSummary
{"project_id": "jade-ibf", "name": "IBF 入库运费", "description": "……",
 "environments": [{"key": "dev", "label": "开发"}], "updated_at": "……"}
```

- 新建时 `project_id` 已存在：409“项目 jade-ibf 已经存在”；`name` 重名：409“已经有叫「IBF 入库运费」的项目”。
- 删除不影响已有的运行：运行在创建时保存了项目的快照（见下）。正在等人处理的运行照样能继续。
- 页面：导航加“项目”（在“数据源和工具”旁边）。列表页、详情页和编辑表单；编辑表单按上面的分组，环境、引用表、参数是可增删的列表，数据外发策略用 `GET /projects/options` 的说明文字。

## 运行绑定项目

### 应用声明用不用项目

编排应用的开始节点新增可选设置 `project`：

```jsonc
{"id": "start", "type": "start", "project": "required" | "optional", "inputs": [...]}   // 不写：这个应用不用项目
```

- 值只能是 `required` 或 `optional`，否则保存报错，`loc` 指到 `project`。
- 应用里引用了 `{{param.*}}` 或 `{{project.*}}` 而开始节点没写 `project` 时，保存报错“开始节点没有设置使用项目，不能引用项目和运行参数”。保存时不知道会绑定哪个项目，所以不检查参数名是否存在，只检查写法（同节点 id 规则）；`{{project.*}}` 的字段要在下面的清单里。
- `GET /apps/{app}` 的 `AppDetail` 新增 `project`（`"required"`、`"optional"` 或 null），新建运行页据此显示项目选择。代码应用（`etl`）总是 null。

### 新建运行

`POST /runs` 的请求体新增可选的 `project`：

```jsonc
{"app": "jade-etl", "input": {...},
 "project": {"project_id": "jade-ibf", "environment": "dev", "params": {"update_month": "2026-09"}}}
```

- `environment` 可选，不写用项目的第一个环境；`params` 可选，没给的参数用默认值。
- 检查顺序和报错（都是 422，`detail` 一句中文）：
  1. 应用不用项目却给了 `project`：“应用「…」不使用项目”。应用要求项目（`required`）却没给：“应用「…」需要选择一个项目”。
  2. 项目不存在：“没有项目 …”；环境不存在：“项目「…」没有环境 …”。
  3. 参数：多出来的键“项目「…」没有参数 …”；必填参数没值也没默认值“请填写参数「统计月」”；值不合类型“参数「统计月」要写成 YYYY-MM”（各类型的提示同取值规则）。
  4. 应用引用的 `{{param.<key>}}` 项目里没有：“应用用到了参数 version_id，项目「…」没有定义它”。
  5. 模型：有效策略（见“数据外发策略”）限定了模型时，应用里每个调模型的节点（`llm`、`classifier`、`agent`，以及知识检索节点所用知识库的向量模型）的模型都要在清单里，节点没写模型时按平台默认模型算：“项目「…」只允许用模型 company，节点「出方案」用的是 deepseek”；没绑定项目、由平台默认限定时写“平台只允许用模型 …”。这一步对所有运行都做，不绑定项目的运行和代码应用（`etl` 按它的节点设置）也一样。
- 通过后，运行保存一份项目快照：项目的 `name`、`table_prefix`、选中的环境、`references`、`adopted`、参数定义、填好的参数值（默认值已补上）和有效策略。之后改项目或删项目都不影响这次运行；运行的“重试”沿用快照，“重新运行”（rerun）按快照里的 `project_id`、环境和参数值重新绑定当前的项目（项目已删除时 422“项目 … 已经删除，不能重新运行”）。

### 运行记录

`RunSummary` 和 `RunDetail` 新增：

```jsonc
"project": {"project_id": "jade-ibf", "name": "IBF 入库运费",
            "environment": "dev", "environment_label": "开发",
            "params": {"update_month": "2026-09"}} | null,
"policy": {"samples": "stats", "models": ["company"] | null}     // 这次运行的有效策略，没绑定项目时是平台默认
```

运行页的标题下面显示项目、环境和参数值；有效策略不是 `full` 时显示一句“发给模型的数据：只发统计（项目设置）”。

### 变量引用 `{{param.<key>}}` 和 `{{project.<字段>}}`

- 两个新的保留前缀 `param`、`project`，规则同 `env`：在文字里换成值的文字（对象和数组插入为 JSON），整段引用时是值本身。
- 都不能用作节点 id（加进保留的 id），同 `env`、`secret`。
- `{{param.<key>}}`：快照里这个参数的值。
- `{{project.<字段>}}`：快照里的项目信息，字段只有这些：

| 字段 | 值 |
| --- | --- |
| `project_id`、`name`、`description`、`table_prefix` | 同项目 |
| `environment` | 环境的 `key` |
| `catalog`、`schema` | 这次运行写表的位置（环境的 catalog 和 schema） |
| `references` | 引用表列表，同项目的 `references` |
| `params` | `[{"key", "label", "type", "value", "description"}]`，`value` 是这次的值 |
| `brief` | 给模型看的一段 Markdown，由平台按快照写：项目名称和说明；写到哪里（`catalog.schema`）和表名前缀；“只能写以前缀开头、属于本项目的表”；引用表（全名、名称、读取筛选、说明）；运行参数（标识、名称、类型、这次的值、说明），以及生成的代码用 `param("标识")` 读、方案和检查里的条件用 `${param.标识}` 写 |

- 没绑定项目的运行里（只有 `optional` 的应用会这样）两种引用都是 null，`brief` 是 `""`，节点要自己处理空值。
- 这些都是人配的项目信息，不算数据值，不受数据外发策略的样本级别影响。

## 数据外发策略

### 有效策略

- 平台默认：`AEP_DATA_SAMPLES`（`full`、`masked`、`stats`、`schema`，默认 `full`，和现在的行为一样）、`AEP_ALLOWED_MODELS`（逗号分隔的模型别名，空为不限）。
- 调模型前平台再查一次模型（防止漏网，比如运行创建以后才出现的调用）：不在清单里时这个节点以 `input_error` 失败，`error_message` 同上面第 5 步的写法，不发出请求。
- 绑定项目的运行：`policy.samples` 不是 null 时用它，否则用平台默认；`policy.models` 同理。项目的设置可以比平台默认严，也可以松，第一期不设上限。

### 管什么

**数据值**：从数据源或 Databricks 表里读出来的值，也就是行、样本、取值分布和由它们算出来的结果。下表是会产出数据值的地方：

| 来源 | 字段 |
| --- | --- |
| `etl_profile` | `tables`（样本、取值分布）、`description` |
| `databricks_dev_run` | `preview`、`answer`、`report` |
| `data_check`（第三批新增） | `results`、`report` |
| `tool` 节点 | `result`，当工具所在的数据源是 `postgres`、`mysql`、`sqlserver`、`file` 或 `databricks` 时（工具服务 `mcp` 的结果不算） |
| `code`、`template` 节点 | 设置里引用了任何数据值时，它的全部产出都算数据值 |
| `agent` 节点调的工具 | 数据源上的工具返回的结果（同 `tool` 节点） |

模型的回答不算数据值：模型看到的已经是按策略处理过的内容。人工节点、页面上给人看的内容不受策略影响，人照样看到完整的数据。

**在哪里处理**：平台在把内容交给模型（和向量模型）之前统一处理，节点不各自处理：

- `llm`、`classifier`、`agent` 的提示词、系统提示和要分类的文本里引用到数据值时，换成按策略处理后的文字；知识检索节点的检索词同样处理。
- `agent` 调数据源工具拿到的结果，进对话前处理。
- `etl_profile` 的 `description` 本来就是写给模型的，节点按策略生成它（下表）；`tables` 原样保存（校验方案节点用完整的取值核对方案，不经过模型）。
- 运行页流程视图里，处理过的节点在详情里写一句“发给模型的数据按「只发统计」处理”。Langfuse 里记录的就是处理后真正发出去的内容。

### 四个级别

| 级别 | 显示名 | 样本行 | 取值分布 | 空值率、不同值个数 | 列名、类型、主外键 |
| --- | --- | --- | --- | --- | --- |
| `full` | 完整样本 | 原样 | 原样 | 有 | 有 |
| `masked` | 脱敏样本 | 脱敏 | 脱敏 | 有 | 有 |
| `stats` | 只发统计 | 不发 | 不发 | 有 | 有 |
| `schema` | 只发结构 | 不发 | 不发 | 不发 | 有 |

脱敏的规则：

- 文字换成同一次运行里稳定的记号 `‹文本#3f2a9c›`（运行 id 加原值做 SHA-256 取前 6 位），同一个值在这次运行里总是同一个记号，模型还能看出哪些值相同。
- 数字保留正负号，按一位有效数字取整（1385355.07 → 1000000，0.26 → 0.3），整数仍是整数。
- 日期只留到月（`2026-10-15` → `2026-10`），时间同样只留到月。
- true、false 和 null 原样。

不认识结构的数据值（比如工具结果）按值递归处理：`masked` 按上面规则逐个替换；`stats` 和 `schema` 换成一句说明，比如“（这里有 128 行数据，列为 order_id、amount、city；按项目的数据外发策略没有发给模型）”，是行列表时数行和列，不是时只写“这里有数据”。

### 不管什么

- 人写的内容（需求、意见、回答的题）和知识库文档不经过这道处理，它们受“允许的模型”约束。
- 代码和方案本身不是数据值。
- 数据值经过 `llm` 节点之后就不再追踪：模型看到的已经是处理后的内容，它的回答不算数据值。

## 测试

- 项目接口：增删改查、各字段的取值规则和报错、`adopted` 必须带前缀、`filter` 的形状检查、409。
- 运行绑定：五步检查各自的报错；快照不随项目修改变化；重试沿用快照，rerun 重新绑定、项目删除后 422；`RunDetail.project`、`policy`。
- 保存检查：`project` 的取值；没写 `project` 时引用 `{{param.*}}`、`{{project.*}}` 报错；`{{project.*}}` 的字段不在清单里报错；`param`、`project` 不能当节点 id。
- 引用：`{{param.x}}`、`{{project.brief}}` 等在文字和整段引用里的值；没绑定项目时为 null 和 `""`。
- 策略：四个级别下 `etl_profile.description` 的内容；`llm` 提示词里引用 `profile.tables`、`dev_run.preview`、数据源工具结果时的替换；`code` 节点引用数据值后产出也被替换；工具服务结果不替换；`agent` 的数据源工具结果被替换；模型不在清单里时运行创建 422（项目和平台默认两种写法）、调模型前的再检查以 `input_error` 失败且不发请求；脱敏结果稳定（同值同记号），数字和日期按规则取整。
