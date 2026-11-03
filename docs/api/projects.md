# 项目、运行参数和数据外发策略

后端和前端按这份约定并行开发。字段名 snake_case，时间是 ISO 8601 字符串。错误格式同 [datasources.md](datasources.md)：`{"detail": "中文说明"}`；请求体不合格返回 422，`detail` 是一句中文。

「项目」是平台资源，不属于某个应用：任何应用的运行都可以绑定一个项目，节点按项目的配置读写数据。数据外发策略管发给模型的数据，有平台默认值，项目可以另设。设计文档：https://claude.ai/artifact/CCPMycsWqgaPzY3iYCMywK （“平台和应用的分工”一节）。

第一期只做项目的环境、表名前缀、表登记、运行参数和数据外发策略；试点后加了验收检查（`checks`，见下）。代码仓库、工程布局在第二期加，到时另写进本文；现在的接口不接受这些键（多出来的键返回 422）。

## 概念

- **环境**：项目在 Databricks 上写表的位置，每个环境一个 catalog 和 schema，可以指定集群。第一个环境是默认环境。工作区还是平台配置的那一个（`AEP_DATABRICKS_HOST`、`AEP_DATABRICKS_TOKEN`），按环境分工作区以后再加。
- **表名前缀**：项目写的每张表都要以它开头，和同一个 schema 里别的表分开。
- **表登记**：
  - `references`：项目只读引用的表，三段式名，可以跨 catalog。每张可以写一个读取筛选（比如只读数量行），平台读这张表时一律带上它（见 [data-nodes.md](data-nodes.md) 第三批）。
  - `adopted`：已经存在、归本项目所有的表。平台建的表会打上项目标记，不用登记；在平台之外建好的表要登记在这里，平台才肯写。
- **写表规则**：绑定项目的运行只能写当前环境的 `<catalog>.<schema>` 下、以前缀开头的表；表已经存在时，它必须带本项目的标记（表属性 `aep.project`），或者登记在 `adopted` 里。别的一律拒绝，不会覆盖不属于本项目的表。
- **运行参数**：项目定义一组参数（比如统计月、版本号）。新建运行时填值，节点里用 `{{param.<key>}}` 引用，生成的代码用 `param("<key>")` 读，所有入口的默认值都取这里，不会各写一份。
- **数据外发策略**：允许用哪些模型，以及从数据源读出来的值以什么程度发给模型，见“数据外发策略”。
- **验收检查**：项目自己的验收标准（比如说明书里的核对项），格式同 `data_check` 节点的检查（[data-nodes.md](data-nodes.md)“data_check 数据检查”）。绑定项目的运行在数据检查节点里引用 `{{project.checks}}` 就会每次都跑，不用每个应用、每次方案各写一份；方案只补充它没有的检查。

## 数据结构

```jsonc
// Project
{
  "project_id": "jade-ibf",              // 2–40 个字符：小写字母开头，小写字母、数字和 -；不能是 new（页面地址用了它）；建好后不能改
  "name": "IBF 入库运费",                 // 1–100 字符，去掉首尾空白；不能和别的项目重名
  "description": "……",                    // 可选，最多 2000 字符，默认 ""
  "table_prefix": "ibf_",                 // 2–30 个字符：小写字母开头，小写字母、数字和 _
  "environments": [Environment],          // 1–10 个，key 不重复；第一个是默认环境
  "references": [Reference],              // 0–100 个，name 不重复
  "adopted": ["ibf_table1_cw"],           // 0–200 个表名（不含 catalog、schema），都要以 table_prefix 开头，不重复
  "params": [Param],                      // 0–20 个，key 不重复
  "checks": [Check],                      // 0–50 个验收检查，Check 同 data_check 节点；可以不写，默认 []
  "conventions": [Convention],            // 0–30 条约定，key 不重复；可以不写，默认 []（jadeflow 第 4 轮复测后新增）
  "model_check_max_severity": "warn",     // error、warn 或 info：方案里模型建议的检查最高按什么级别跑；可以不写，默认 error（jadeflow 第 4 轮复测后新增）
  "policy": Policy,                       // 见“数据外发策略”
  "created_at": "……",
  "updated_at": "……"
}

// Environment
{
  "key": "dev",                           // 规则同编排应用的节点 id：小写字母开头，小写字母、数字和 _，1–32 个字符
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
  "pattern": "FY\\d{2}_\\d{2}_M",            // 只用于 text，可选：整个值要符合的正则表达式（RE2），1–200 字符，默认 null
  "description": "按自然月，C+3 当天跑上个月",    // 可选，最多 500 字符
  "scheduled": Scheduled                  // 可选，上线后定时运行时怎么取这个参数，默认 null，见“定时运行时参数怎么取”
}

// Scheduled（jadeflow 复测后新增）
{
  "use": "date" | "run_id" | "this_run",
  "format": "FY%y_%m_M",                  // 只用于 date 和 text 参数，必填：日期格式，只能用 %Y %y %m %d（和 %%），1–50 字符
  "months": -1,                           // 只用于 date：从运行那天往前（负数）或往后推几个月，-24–24，默认 0
  "day": "first" | "last" | null          // 只用于 date：取那个月的第一天、最后一天，null 是同一天（没有这一天时取月底）
}

// Convention（jadeflow 第 4 轮复测后新增）：这个项目的运行要遵守的规则
{
  "key": "by_version",                    // 规则同 Environment.key
  "title": "读 table1 的地方都先按 version_id 筛选",   // 1–100 字符，去掉首尾空白
  "text": "table1 里有多个版本的数据……",     // 可选，补充说明和怎么判断，最多 2000 字符，默认 ""
  "applies_to": "plan" | "code" | "both"  // 写方案时、写代码时还是都要遵守，默认 both
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
- `pattern`（试点后新增）：`text` 参数的值要整个符合它（相当于前后加了 `^`、`$`），不符合时“参数「版本」要符合格式 FY\d{2}_\d{2}_M”，默认值和新建运行时填的值都查。语法是 RE2（Python 的 `re` 的常用写法都能用，不支持反向引用和环视）：匹配时间和值的长度成正比，项目写什么样的格式都不会让接口卡住。别的类型写了它报“参数「统计月」不是文本，不能写格式”；写错了报“参数「版本」的格式不是有效的正则表达式（missing ): (a）”，括号里是 RE2 的原话。以前保存的参数没有这个键，等于没有格式。
- `scheduled`：`use` 是 `run_id` 或 `this_run` 时不能写 `format`、`months`、`day`（“只有按日期推算（date）才写 format、months、day”）；`run_id` 只能用在 `text` 参数上；`date` 只能用在 `text`、`date`、`month` 参数上，`text` 参数要写 `format`（“文本参数要写日期格式 format，如 FY%y_%m_M”），`date` 和 `month` 参数的格式固定是 `YYYY-MM-DD`、`YYYY-MM`，不能写 `format`，`month` 参数不能写 `day`；`format` 里有别的 `%` 写法报“日期格式只能用 %Y（四位年份）、%y（两位年份）、%m（月）、%d（日）”，一个都没有报“日期格式里至少要有 %Y、%y、%m、%d 中的一个”。报错写成“参数「版本」定时运行时的取法不对：……”。以前保存的参数没有这个键，等于 null。
- `conventions`：约定会出现在两个地方：给模型看的项目说明（`brief`）里有一节“约定”，写方案和写代码的模型都能看到；模型审查节点（`llm_review`）用 `{{project.plan_rules}}`、`{{project.code_rules}}` 把它们当审查清单，审查出没遵守的就让那一步重写（见 flow-apps.md“llm_review 模型审查”）。key 重复报“约定 by_version 重复”。保存以前建的项目没有这个键，读出来是 `[]`。约定是项目的通用写法要求（数据怎么读、口径怎么算），不要写成某次需求的答案。
- `model_check_max_severity`：方案里模型建议的数据检查（`etl_plan_check` 第 3 版的 `checks`）最高按什么级别跑，写得更高的降到它，记进方案的 `cleaned`（见 data-nodes.md）。项目自己的验收检查 `checks` 不受影响，照它们写的级别跑。模型建议的检查条件和方向可能写错，一条错的 `error` 检查会挡住对的结果；需要人先核对的项目设成 `warn`，确认方案时觉得哪条该挡住交付，写意见让模型改成 `error`、或者把它加进项目的验收检查。不是这三个值时 422。保存以前建的项目没有这个键，读出来是 `error`（以前的行为）。
- `models` 里的别名必须在模型网关上（同编排应用保存检查用的模型清单）；网关读不到清单时不检查。
- `checks`：每条按 `data_check` 的规则检查（`aep.etl.checks.problems`），报第一个问题，写成“验收检查 c1（where）：条件不能为空”（没有 id 时“第 2 条验收检查：…”）；另外 id 不能重复（“验收检查 id c1 重复”）、不能用 `golden`（“验收检查的 id 不能用 golden，它留给数据检查节点和老系统的对比”）；条件和表达式里的 `${param.<key>}` 要是本项目的参数（“验收检查 c1（where）用的运行参数 ${param.month} 不是本项目定义的参数”）；不带点的表名是这次运行在环境里写的表，要以表名前缀开头（“验收检查 c1（table）：不带 catalog、schema 的 t2 是本项目在运行环境里的表，要以表名前缀 ibf_ 开头”）。保存以前建的项目没有这个键，读出来是 `[]`。

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
- 页面：导航加“项目”（在“数据源和工具”旁边）。列表页、详情页和编辑表单；编辑表单按上面的分组，环境、引用表、参数是可增删的列表，数据外发策略用 `GET /projects/options` 的说明文字。文字参数多一栏“格式（选填）”；详情页的类型写成“文字，格式 …”；新建运行页在参数下面提示“格式：…”，填的值不符合时先在页面上报错（页面用 JavaScript 的正则判断，两边读法不同的格式只由后端判断）。

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
  5. 模型：有效策略（见“数据外发策略”）限定了模型时，应用里每个调模型的节点（`llm`、`classifier`、`agent`、`llm_review`，以及知识检索所用知识库的向量模型和检索设置里的重排模型；只用关键词检索时不调向量模型，不算它）的模型都要在清单里，节点没写模型时按平台默认模型算；平台自己算的 `mock-embed`、`mock-rerank` 不出平台，不受清单限制：“项目「…」只允许用模型 company，节点「出方案」用的是 deepseek”；没绑定项目、由平台默认限定时写“平台只允许用模型 …”。这一步对所有运行都做，不绑定项目的运行和代码应用（`etl` 按它的节点设置）也一样。
- 通过后，运行保存一份项目快照：项目的 `name`、`table_prefix`、选中的环境、`references`、`adopted`、参数定义、填好的参数值（默认值已补上）、验收检查和有效策略。约定、模型检查的最高级别（`model_check_max_severity`，旧快照里没有时按 `error`）也在快照里。第五批起另存项目当时的全部环境（`environments`），上线节点 `databricks_release` 按它找上线的环境（data-nodes.md）；以前的运行快照里没有。之后改项目或删项目都不影响这次运行；运行的“重试”沿用快照，“重新运行”（rerun）按快照里的 `project_id`、环境和参数值重新绑定当前的项目（项目已删除时 422“项目 … 已经删除，不能重新运行”）。

### 运行记录

`RunSummary` 和 `RunDetail` 新增：

```jsonc
"project": {"project_id": "jade-ibf", "name": "IBF 入库运费",
            "environment": "dev", "environment_label": "开发",
            "params": {"update_month": "2026-09"},
            "param_labels": {"update_month": "统计月"},                // 参数的显示名，页面用
            "policy": {"samples": "stats", "models": null}} | null,   // 项目自己的策略设置，null 的项用平台默认
"policy": {"samples": "stats", "models": ["company"] | null}     // 这次运行的有效策略，没绑定项目时是平台默认
```

运行页的标题下面显示项目、环境和参数值；有效策略不是 `full` 或限定了模型时显示一句“发给模型的数据：只发统计（项目设置）”，括号里按 `project.policy` 写这一项来自项目设置还是平台设置。

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
| `checks` | 项目的验收检查（同项目的 `checks`），给数据检查节点的 `include` 用；快照里没有时（加这个字段以前的运行）是 `[]` |
| `can_release` | 能不能上线（jadeflow 复测后新增）：项目有 key 为 `prod` 的环境，而且这次运行不在 `prod` 里时为 true；快照里没有 `environments`（第五批以前的运行）时为 false |
| `release_ready` | `can_release` 为 true，而且每个参数在定时运行时都取得到（见下一节）时为 true |
| `release_brief` | `can_release` 为 true 时，给人看的上线说明（Markdown），否则 `""`：上线到哪个环境、写到哪个 `catalog.schema`、只写前缀开头的本项目表；平台在 Databricks 上建（或更新）一个 notebook Job，按选的时间运行，上线时不马上运行；有参数时列一张表：每个参数定时运行时怎么取、按今天（`AEP_DATABRICKS_PROD_TIMEZONE` 的日期）运行的话是什么值，算出的值不符合参数格式时注明“这样的日子定时运行会报错、不写表”；有参数取不到时写“现在不能上线”和原因；最后是怎么撤销（删掉或暂停 Job；写进表的数据不跟着撤销，Delta 表可以 `RESTORE TABLE … TO VERSION AS OF …`） |
| `conventions` | 项目的约定（同项目的 `conventions`）；快照里没有时（加这个字段以前的运行）是 `[]` |
| `plan_rules`、`code_rules` | 给模型审查节点当清单用：`applies_to` 是 `plan`（或 `code`）和 `both` 的约定，写成 `[{"key", "title", "instruction"}]`，`instruction` 是约定的 `text`；没有时是 `[]` |
| `brief` | 给模型看的一段 Markdown，由平台按快照写：项目名称和说明；写到哪里（`catalog.schema`）和表名前缀；“只能写以前缀开头、属于本项目的表”；引用表（全名、名称、读取筛选、说明）；运行参数（标识、名称、类型（有格式时写成“text，格式 `…`”）、这次的值、说明），以及生成的代码用 `param("标识")` 读、方案和检查里的条件用 `${param.标识}` 写；有验收检查时再列一张表（id、名称、级别、检查什么），并说“方案里建议的数据检查不要重复这些，id 也不能和它们相同”；`model_check_max_severity` 不是 `error` 时再写一句“方案里建议的数据检查最高只能是 warn（项目规定），写得更高的按 warn 运行。”；有约定时最后加一节“约定（必须遵守，平台会按它们审查）”，每条写名称、什么时候遵守（写方案时、写代码时、写方案和写代码时）和说明 |

- 没绑定项目的运行里（只有 `optional` 的应用会这样）两种引用都是 null，`brief` 和 `release_brief` 是 `""`，`conventions`、`plan_rules`、`code_rules` 是 `[]`（模型审查节点因此没有清单、直接跳过），节点要自己处理空值。
- 单节点试跑（`POST /apps/{app}/nodes/{node}/test`）的测试值可以给 `param.<key>`、`project.<字段>`，没给的按没绑定项目算。
- 这些都是人配的项目信息，不算数据值，不受数据外发策略的样本级别影响。

### 定时运行时参数怎么取（jadeflow 复测后新增）

绑定项目的运行上线到 prod 后（data-nodes.md 的 `databricks_release`），Databricks 按定时运行这个 Job，每次运行的参数按参数的 `scheduled` 取，不再一直用上线那次运行的值（每月换一次的版本号照搬会每天重算同一个旧版本）：

| `scheduled` | 定时运行时的值 |
| --- | --- |
| `{"use": "date", "format": "FY%y_%m_M", "months": -1}` | 按运行那天（`AEP_DATABRICKS_PROD_TIMEZONE`，和定时同一个时区）推算：上个月，写成 `FY26_10_M`；`date` 参数写成 `YYYY-MM-DD`，`month` 参数写成 `YYYY-MM` |
| `{"use": "run_id"}` | 这次 Databricks 作业运行的 ID（数字），同写进表属性的 `run_id` |
| `{"use": "this_run"}` | 一直用上线那次运行的值 |
| 不写（null） | 有默认值用默认值；不是必填的为空值；必填又没有默认值的取不到，不能上线 |

- 取不到的参数在上线说明里写明，平台不部署，上线节点走 `failed`：“参数「运行批次」没有写定时运行时怎么取，也没有默认值：在项目的运行参数里给它写上定时运行时的取法（或者默认值），再重新发起运行”。运行用的是绑定时的快照，改了项目要重新发起运行。
- 每次运行在集群上算值（`aep.cluster_harness.scheduled_params`）。参数有 `pattern` 时，算出的值要整个符合它，不符合时这次运行报错、不写表：“定时运行在 2027-01-01 算出参数「版本」= FY26_12_M，不符合它的格式（月度版本 …），这次不运行，没有写表”（括号里是参数的说明，没有说明时是“要整段符合 <正则>”）。
- 上线时平台把不用每次算的值（默认值、空值、`this_run`）写进 Job 的 `params`，把要每次算的写进 Job 的 `param_rules`（`{"timezone": "Asia/Shanghai", "params": [{"key", "label", "type", "use", "format", "months", "day", "pattern", "description"}]}`）。

## 数据外发策略

### 有效策略

- 平台默认：`AEP_DATA_SAMPLES`（`full`、`masked`、`stats`、`schema`，默认 `full`，和现在的行为一样）、`AEP_ALLOWED_MODELS`（逗号分隔的模型别名，空为不限）。
- 调模型前平台再查一次模型（防止漏网，比如运行创建以后才出现的调用，或者运行创建以后知识库换了向量模型）：知识检索的向量模型和重排模型也在发请求前查；不在清单里时这个节点以 `input_error` 失败，`error_message` 同上面第 5 步的写法，不发出请求。
- 运行创建时定下的策略保存在运行上，之后一直按它（确认后恢复、平台重启后恢复都一样），平台默认改了也不影响已有的运行；代码应用 `etl` 同样。
- 绑定项目的运行：`policy.samples` 不是 null 时用它，否则用平台默认；`policy.models` 同理。项目的设置可以比平台默认严，也可以松，第一期不设上限。

### 管什么

**数据值**：从数据源或 Databricks 表里读出来的值，也就是行、样本、取值分布和由它们算出来的结果。下表是会产出数据值的地方：

| 来源 | 字段 |
| --- | --- |
| `etl_profile` | `tables`（样本、取值分布）；`description` 由节点按策略生成，引用时不再处理（见下） |
| `databricks_dev_run` | `preview`、`answer`、`report` |
| `data_check`（第三批新增） | `results`、`report` |
| `tool` 节点 | `result`，当工具所在的数据源是 `postgres`、`mysql`、`sqlserver`、`file` 或 `databricks` 时（工具服务 `mcp` 的结果不算） |
| `code`、`template` 节点 | 设置里引用了任何数据值时，它的全部产出都算数据值 |
| `agent` 节点调的工具 | 数据源上的工具返回的结果（同 `tool` 节点） |

**带取值的报错**：运行代码或读数据时的报错可能引用到数据里的值（比如“The value 'abc' of the type "STRING" cannot be cast”）。这些文字不当作整块数据值（整块换掉就没法据此修代码了），而是把其中引号里的取值单独处理：

| 来源 | 字段 |
| --- | --- |
| `databricks_dev_run` | `errors`，以及也带着报错的 `run` |
| 任何节点失败后走 `error` 分支时 | `error_message` |
| `agent` 节点调数据源工具出错 | 发回给模型的出错说明 |
| 代码应用 `etl` | dev 试跑的报错（发回给写代码的模型时） |
| `template`、`code` 节点 | 引用了上面这些字段（没引用数据值）时的产出；引用了数据值时整块按数据值处理 |

处理办法：`full` 原样；`masked` 把单引号或双引号里的内容（不论多长，可以跨行，里面可以有另一种引号，比如 `"O'Reilly"`）换成记号（`'abc'` → `'‹文本#…›'`），全大写的类型名（`"STRING"`、`"DECIMAL(10,2)"`）和已经是记号的不换，模型照抄这些记号时同样换回原值；`stats` 和 `schema` 把引号里的内容换成 `‹已隐去›`。引号外面的文字（报错类型、列名、行号）不动。

报错被截短时（`databricks_dev_run` 和代码应用 `etl` 每条最多 1500 个字符，有的报错在更早的地方就截短了），截断处可能正好在一个取值中间：平台截短时在截断处补上“…”和同样的引号，这个取值照样按上面的办法处理，后面接着的报错也不受影响；文字末尾还开着的引号（别处截短的）算到文字末尾。代码应用 `etl` 先处理完整的报错再截短，所以脱敏时整个取值换成一个记号，模型照抄后能换回完整的原值。

**校验方案节点的问题清单**：方案里拿某列和数据里没有的值比较时，问题清单会列出这一列的实际取值；策略不是 `full` 时不列，只说“数据里没有这个取值（按数据外发策略不列出实际取值）”。脱敏时模型可以对照表描述里脱敏后的取值分布改。

模型的回答不算数据值：模型看到的已经是按策略处理过的内容（脱敏时回答里换回的原值另有规则，见下）。人工节点、页面上给人看的内容不受策略影响，人照样看到完整的数据。

**在哪里处理**：平台在把内容交给模型（和向量模型）之前统一处理，节点不各自处理：

- `llm`、`classifier`、`agent` 的提示词、系统提示和要分类的文本里引用到数据值时，换成按策略处理后的文字；知识检索节点的检索词同样处理。
- `agent` 调数据源工具拿到的结果，进对话前处理。
- `etl_profile` 的 `description` 本来就是写给模型的，节点按策略生成它（下表），提示词引用它时原样放进去；`tables` 原样保存（校验方案节点用完整的取值核对方案，不经过模型）。代码应用 `etl` 给模型的表描述同样按平台默认的级别生成。
- 脱敏的记号换回原值：模型的回答里原样出现的记号（`‹文本#3f2a9c01d4›`），平台在节点拿到回答之前换回这次运行里对应的原值，所以方案、代码和条件里写的是真实取值，校验方案节点照常核对，交给 Databricks 的代码也是真实取值。回答里带 JSON 对象时（按解析回答的同样办法找：先找代码块，再取第一个 `{` 到最后一个 `}`，能解析才算），记号在 JSON 字符串里，换回时按 JSON 转义原值里的引号、反斜杠和换行，之后解析回答的节点照常解析。对不上的记号原样留着；万一这次运行里两个不同的值算出同一个记号，这个记号也原样留着，不换回任何一个，所以不会换成错的值（之后的校验或试算会因为它报错）。
- 换回的原值发给模型前重新换成记号：平台记下这次运行里换回过哪些记号和原值（存在运行状态里）。之后任何节点的产出被引用进发给模型的内容时（提示词、系统提示、要分类的文本、检索词、Agent 的任务），里面出现的这些原值都重新换成记号；匹配时原值前后不能紧挨英文字母、数字或下划线（中文没有词的边界，照样替换）；原值写在 JSON 或代码字符串里、引号反斜杠换行被转义的写法也一样换回记号。所以方案经过校验方案节点、写进代码，再发给写代码或重写方案的模型时，仍然是记号。人写的内容（开始节点的输入、人工节点的产出）和常量不换。
- 给人看的提示是运行页标题下面那一句（见“运行记录”），节点详情里不再逐个标注。Langfuse 里记录的就是处理后真正发出去的内容。

### 四个级别

| 级别 | 显示名 | 样本行 | 取值分布 | 空值率、不同值个数 | 列名、类型、主外键 |
| --- | --- | --- | --- | --- | --- |
| `full` | 完整样本 | 原样 | 原样 | 有 | 有 |
| `masked` | 脱敏样本 | 脱敏 | 脱敏 | 有 | 有 |
| `stats` | 只发统计 | 不发 | 不发 | 有 | 有 |
| `schema` | 只发结构 | 不发 | 不发 | 不发 | 有 |

脱敏的规则：

- 文字换成同一次运行里稳定的记号 `‹文本#3f2a9c01d4›`（运行 id 加原值做 SHA-256 取前 10 位，一次运行里上百万个不同的值才会有一半的机会撞上；撞上时按上面的规则不换回），同一个值在这次运行里总是同一个记号，模型还能看出哪些值相同。
- 数字保留正负号，按一位有效数字取整（1385355.07 → 1000000，0.26 → 0.3），整数仍是整数。
- 日期只留到月（`2026-10-15` → `2026-10`），时间同样只留到月。
- true、false 和 null 原样。

不认识结构的数据值（比如工具结果）按值递归处理：`masked` 按上面规则逐个替换；`stats` 和 `schema` 换成一句说明，比如“（这里有 128 行数据，列为 order_id、amount、city；按项目的数据外发策略没有发给模型）”，是行列表时数行和列，不是时只写“这里有数据”。

### 不管什么

- 人写的内容（需求、意见、回答的题）和知识库文档不经过这道处理，它们受“允许的模型”约束。
- 代码和方案本身不是数据值，但其中换回过的原值照上面的规则重新换成记号。
- 人看到的报错（运行页的出错信息、节点详情）是完整的，只有发给模型的那一份按上面处理。
- 模型的回答不当作数据值追踪：模型看到的已经是处理后的内容；回答里只有换回过的原值需要再处理，规则见上。

## 测试

- 项目接口：增删改查、各字段的取值规则和报错、`adopted` 必须带前缀、`filter` 的形状检查、409；验收检查的各条规则和报错、没写时为 `[]`。
- 参数格式：整个值要符合、默认值不符合时 422、不是文字参数时 422、RE2 不认的写法（环视、反向引用）422、病态的格式（`(\w+)+\w*\w*\w*x`）配 500 字的值也立刻有结果、`brief` 里写上格式、没有这个键的旧参数照旧；页面上填格式、默认值不符合时的提示、详情页显示格式、新建运行时的提示和报错（Playwright）。
- 验收检查：快照带上它，`{{project.checks}}` 取到列表，`brief` 列出它；旧快照为 `[]`，没绑定项目为 null。
- 模型检查的最高级别：不写时为 `error`、别的值 422；快照带上它，`brief` 里的那一句只在不是 `error` 时写；页面上能选，详情页显示（Playwright）。
- 运行绑定：五步检查各自的报错；快照不随项目修改变化；重试沿用快照，rerun 重新绑定、项目删除后 422；`RunDetail.project`、`policy`。
- 保存检查：`project` 的取值；没写 `project` 时引用 `{{param.*}}`、`{{project.*}}` 报错；`{{project.*}}` 的字段不在清单里报错；`param`、`project` 不能当节点 id。
- 引用：`{{param.x}}`、`{{project.brief}}` 等在文字和整段引用里的值；没绑定项目时为 null 和 `""`。
- 策略：四个级别下 `etl_profile.description` 的内容（代码应用 `etl` 同样）；回答里的记号换回原值，换回的原值经过校验方案、模板、代码节点再进提示词时重新换成记号（含英文边界和中文的情况），人写的内容不换；`llm` 提示词里引用 `profile.tables`、`dev_run.preview`、数据源工具结果时的替换；`code` 节点引用数据值后产出也被替换；工具服务结果不替换；`agent` 的数据源工具结果被替换；模型不在清单里时运行创建 422（项目和平台默认两种写法，含知识库的向量模型和重排模型）、调模型前的再检查以 `input_error` 失败且不发请求；带取值的报错在各级别下的处理（引号里的取值、类型名不换）；校验方案的问题清单在非 `full` 时不列实际取值；脱敏结果稳定（同值同记号），数字和日期按规则取整。
