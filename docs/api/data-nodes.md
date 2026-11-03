# 数据工程节点、应用说明书和 ETL 演示应用

本文是 [flow-apps.md](flow-apps.md) 的补充：新增 5 种「数据工程」节点，把自然语言 ETL 做成编排应用；新增节点的讲解说明 `note` 和应用说明书接口；新增连接器工具 `read_rows`；新增演示用的零售示例库、两个知识库、应用模板和 `make demo`。设计文档：https://claude.ai/artifact/K45W27BX8CVu7Lzh1HYtC2

代码应用 `etl` 的流程和结果保持不变（第二批带来的几处小变化见下一段）。新节点复用它的实现（`aep.etl.profile`、`aep.etl.plan`、`aep.etl.codegen`、`aep.databricks`），不另写一套规则。

第二批新增的方案第 2 版（业务说明、问题、规则、答案设计）只用在 `format` 为 2 的 `etl_plan_check` 和内置 Skill `etl-plan-brief` 上。代码应用 `etl` 继续用第 1 版：`aep.etl.plan` 里的 `EtlPlan`、`validate_plan` 和 `OUTPUT_FORMAT` 一个字不改，它的方案、结果和评测也不变。它能看到的变化只有这几处：`pending` 多一个 `changed_branch: null`（和编排应用共用同一个 `Pending`）；resume 的报错文字和 `actor` 的长度限制按 flow-apps.md 的新规则（选了不能选的选项返回“这一步现在不能选「…」”，`actor` 最多 100 个字符）；resume 多接受 `seen` 和 `notes`：`etl` 的 `pending` 没有 `details.seen`，所以 `seen` 不比，带了非空的 `notes` 因为找不到条目返回 422；运行记录（列表和详情）多一个 `outcome`，代码应用总是 null；`GET /runs/{id}/files.zip` 没有代码时，404 的文字从 “this run has no files” 改成“这次运行没有可下载的代码”；`job` 模式 bundle 里的 `src/aep_harness.py` 跟着试跑程序变（见“答案怎么算”）。

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

`GET /node-types` 的 `category` 新增 `"data"`（前端显示“数据工程”）。5 种新节点都是 `category: "data"`、`error_handling: true`；流程视图的 `kind`：`etl_profile`、`databricks_ingest`、`databricks_dev_run` 为 `tool`，`etl_plan_check`、`etl_code_check` 为 `code`。第三批新增的 `data_check` 同样是 `category: "data"`、`error_handling: true`，`kind` 为 `tool`。

出错类型新增 `databricks_error`：没配置 Databricks 工作区，或 Databricks 接口、集群命令报错。连不上数据源、连接器报错仍是 `tool_error`。节点设置或运行输入有问题时是 `input_error`（表名写法不对、没有可读的表、展开后超过 `MAX_TABLES`、抽取超过 `max_rows`、`databricks_dev_run` 的 `bundle` 不是检查并打包节点的 bundle、没选数据源），见 flow-apps.md：不按 `on_error.retry` 重试，运行页也不提供重试。

平台没有配置 Databricks（`AEP_DATABRICKS_HOST`、`AEP_DATABRICKS_TOKEN`）时，两个 `databricks_*` 节点照样能保存，运行到它们时以 `databricks_error` 失败，`error_message` 写明缺哪个配置。抽取和结果预览还需要 `AEP_DATABRICKS_CLUSTER_ID` 和 `AEP_DATABRICKS_SOURCE_CATALOG`。

下面每种节点都写了设置、产出和分支。凡是写“整段引用”的设置，值恰好是一个引用（如 `"{{profile.tables}}"`）时传原始值，规则同工具参数。

### etl_profile 探查数据

```jsonc
{"datasource": "{{start.datasource}}",   // datasource_id 或整段引用，规则同 tool 节点
 "tables": "{{start.tables}}",           // 字符串或字符串数组，可以是整段引用；见下
 "sample_rows": 5}                       // 1–20
```

- `datasource`：不接受工具服务（`mcp`，见 [datasources.md](datasources.md)）：写死的 id 保存时报错，运行时选到的以 `input_error` 失败。
- `tables`：要探查哪些表。字符串按逗号、空白、换行拆开；每项是 `schema.table` 或 `schema.*`（这个 schema 下的全部表）。为空或空数组时探查数据源里的全部表。展开后超过 20 张表（`MAX_TABLES`）时失败，`input_error`，提示指定表。
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
 "max_attempts": 3,                   // 1–5
 "on_stuck": "fail",                  // 可选，验收后新增：连续 max_attempts 次不过时 fail 节点失败（不写时），branch 走 stuck 分支交给人
 "format": 2,                         // 可选，第二批新增：1 第 1 版方案（不写时），2 第 2 版方案；第三批加 3 第 3 版方案
 "answered": "{{review.record}}"}     // 可选，第二批新增，只用于 format 2、3：人已经答过的题，整段引用人工节点的 record
```

- 执行：解析 `reply`，用 `aep.etl.plan.validate_plan` 校验，`source_tables` 是 `profile` 的表名，`known` 是 `known_values(profile)`，`references` 是 `hits` 合起来（按 `index` 去重）。`trust_assumptions`：这个节点上一次是 `retry` 时为 true，否则为 false（同代码应用：第一次让模型先看一眼真实取值）。`format` 为 2 时另有一套检查和整理，见下面的“方案第 2 版”。
- 解析失败或校验不过走 `retry`；**连续** `max_attempts` 次不过时节点失败，`error_type` 为 `output_format`，`error_message` 是最后一次的问题。走过一次 `ok` 后连续次数清零（比如人驳回后重写）。
- `on_stuck`（验收后新增）：`fail`（不写时，同上）或 `branch`。`branch` 时节点多一个分支 `stuck`（连线标签“交给人”），连续 `max_attempts` 次不过时不失败，走 `stuck`：产出和不通过时一样，只是 `message` 换成给人看的一段话（“模型连续 3 次没写出能通过平台检查的方案。最后一次的问题：……”，接着说明可以写意见让模型重写或终止，最后附上没通过的那一版回答，最多 2 万字），连续次数清零，所以人写了意见后模型又有 `max_attempts` 次机会。保存时 `on_stuck` 只能是这两个值（“连续不过之后 on_stuck 只能是 fail（报错停下）或 branch（交给人）”），是 `branch` 时 `stuck` 要有连线（同别的分支）。以前一次运行常因为格式上的小问题连续不过、整条终止，前面的人工确认全部作废；交给人后人可以指出怎么改，或者自己决定终止。
- `format` 只能是 1、2 或 3（“方案版本 format 只能是 1、2 或 3”）；`answered` 必须恰好是一个引用，只有 `format` 为 2 或 3 时能写（“只有方案版本 format 为 2 或 3 时才能写已答的题”）。不合格时保存报错，`loc` 指到这个设置。以前的配置没有这两个键，照第 1 版运行；下表里新加的产出，只有标了“两版都有”的有值，其余按第 1 版的写法为空。

产出：

| 字段 | 内容 |
| --- | --- |
| `ok` | 是否通过 |
| `plan` | 通过时是校验后的方案（`EtlPlan` 的 JSON，含 `references`；第 2 版见下）；不通过时是解析出的原始 JSON，解析不了为 `null` |
| `problems` | 不通过的原因，一条一行；通过时为 `""` |
| `message` | 给人看的方案，Markdown：摘要；每张目标表的名称、加载方式、来源、做法、列映射表格、主键、质量规则；需要确认的假设；“依据的资料”列出 `[n]`《文件名》章节。第 2 版先写业务说明（目标和粒度、指标表、维度、筛选、时间范围、要回答的问题、已按口径处理的规则、最后给你的答案），再写“技术方案”（每张目标表同上），不再有“需要确认的假设”。不通过时是问题列表 |
| `sources` | 方案用到的源表（`schema.table`，不含其他目标表），去重、按名称排序 |
| `targets` | 目标表名列表 |
| `references` | 两版都有：方案引用了的资料，同 `plan.references` |
| `summary` | 两版都有：一句话，第 1 版如 `1 张目标表、2 条假设待确认`，第 2 版如 `2 张目标表、4 个指标、2 个问题待回答` |
| `brief` | 第 2 版：整理后的业务说明，同 `plan.brief`；第 1 版为 null |
| `questions` | 第 2 版：要回答的题，已经写成人工节点确认模式 `fields` 的样子，可以整段给人工节点的 `fields`（一定能通过人工节点对题的检查，见下）：`[{"key": "q1", "label": "统计哪段时间？", "type": "select", "required": true, "choices": [{"value", "label"}], "suggested": "all", "why": "…", "affects": ["m1"]}]`；第 1 版为 `[]` |
| `answer` | 第 2 版：答案设计，见“答案设计和可加性”；没有设计时和第 1 版都为 null |
| `uncited` | 第 2 版：说明里没有有效 `[n]` 的指标、维度和筛选的 id，页面标“没找到口径文档，按需求理解”；第 1 版为 `[]` |
| `cleaned` | 第 2 版：平台自动整理过的地方，一条一句，如 `去掉了 4 条规则开头的英文前缀“advance:”`；第 1 版为 `[]` |
| `result_table` | 第 3 版（第三批）：结果表的表名，见“方案第 3 版”；第 1、2 版为 `""` |
| `result_keys` | 第 3 版：结果表的 `primary_key`；第 1、2 版为 `[]` |
| `checks` | 第 3 版：方案建议的数据检查；第 1、2 版为 `[]` |
| `dropped` | 三版都有（第二轮验收 I-15 后新增）：方案被平台退回过、这次通过时，退回的那几版里有、通过的这一版没有了的检查（按 `id` 或 `label` 认，项目的验收检查不算，它们照样会跑）和列（这张表还在、列没了；整张表改了名不算），一条一句，如 `检查「k1 子查询检查」`、`t2p_table2 的列 platform_original`；没有时为 `[]`。不为空时 `message` 开头多一段“注意：方案被平台校验退回后，这一版去掉了…请对照需求确认还要不要”。退回的版本里有什么由节点自己记着（`private.rejected`），通过一次就清空 |

不通过时新加的这些字段都为空：`references`、`questions`、`uncited`、`cleaned`、`result_keys`、`checks`、`dropped` 为 `[]`，`brief`、`answer` 为 null，`summary`、`result_table` 为 `""`。

分支：`ok`（通过）、`retry`（重写）。

#### 方案第 2 版

`format` 为 2 时，模型按 `aep.etl.plan.OUTPUT_FORMAT_V2` 回答（第二批新增，`OUTPUT_FORMAT` 不动）。第 1 版的字段照旧，新字段都是可选的：

```jsonc
{
  "summary": "一两句话说明方案",                 // 没写时取 brief.goal
  "language": "pyspark",
  "brief": {                                    // 业务说明：不写代码、源表名和 bundle 名
    "goal": "按月、按城市、按会员等级统计有效订单的 4 个指标",
    "grain": "每个统计月 × 城市 × 会员等级一行",
    "metrics": [{"id": "m1", "name": "销售额", "unit": "元",
                 "definition": "有效订单里每行商品的成交金额之和：数量 × 成交单价 × (1 − 折扣)，不含运费 [2]",
                 "column": "sales_amount"}],
    "dimensions": [{"id": "d2", "name": "城市", "definition": "下单客户档案里的城市 [3]",
                    "column": "city", "examples": ["上海", "北京", "未知"]}],     // examples 可选，最多 5 个
    "filters": [{"id": "f1", "text": "只算有效订单：已支付、已发货或已完成，并且不是测试账号 [1]"}],
    "time_range": {"text": "2025-11 至 2026-10，按下单时间 [4]", "column": "stat_month"}   // 可以为 null
  },
  "targets": [{"name": "ads_sales_city_member_m", "…": "同第 1 版",
               "columns": [{"name": "sales_amount", "label": "销售额（元）", "source": "sum(line_amount)", "description": "…"}]}],   // 列新增可选的 label
  "rules": [{"id": "r1", "text": "城市名去掉末尾的「市」[3]", "basis": "knowledge"}],
  "questions": [{"id": "q1", "text": "统计哪段时间？", "why": "需求没说时间范围",
                 "options": [{"value": "all", "label": "源库现有的 12 个月"}, {"value": "y2026", "label": "只看 2026 年"}],
                 "suggested": "all", "affects": ["m1", "m2"]}],
  "answer": {"target": "ads_sales_city_member_m",
             "filter": [{"column": "stat_month", "pick": "latest"}],
             "group_by": ["city", "member_level"],
             "measures": ["sales_amount", "order_count", "customer_count", "avg_order_value"],
             "order_by": [{"column": "sales_amount", "desc": true}],
             "top_n": 10,
             "chart": {"type": "bar", "label": ["city", "member_level"], "value": "sales_amount"}},
  "assumptions": []                              // 旧字段仍然接受，平台并入 rules
}
```

字段规则：
- **必填和可选**：`brief`、`rules`、`questions`、`answer` 都可以不写（`brief`、`answer` 不写时由平台生成，见下）。写了的按 `OUTPUT_FORMAT_V2` 的结构检查：`brief` 的 `goal`，指标和维度的 `name`、`definition`，筛选和规则的 `text`，`time_range` 不为 null 时它的 `text`，题的 `text`，`answer` 的 `target` 和 `measures` 都必填；文字要是文字，列表要是列表（题的 `options` 没写或少于 2 个时改成规则，见下）；`brief` 的 `metrics`、`dimensions`、`filters` 不写为 `[]`。指标的 `unit`、`column`，维度的 `column`、`examples`，`time_range` 的 `column`，题的 `why`、`affects` 可选。id 没写或写法不对不算结构问题，由平台改（见下）。没写 `column` 的指标和维度不进平台生成的答案设计，也不拿来给列补 `label`。
- **结果表**：`answer.target`；没有 `answer` 时是 `targets` 里最后一张。`answer` 不是对象、或者 `answer.target` 不是某张目标表时（这本身会让模型重写，见下），其余用到结果表的整理和检查都按 `targets` 里最后一张算。`brief` 里指标、维度和时间范围的 `column` 都要是结果表的列。
- **id**：指标、维度、筛选、规则和题的 `id` 写法同节点 id，在这几类之间也不能重复。
- **rules**：`basis` 是依据：`knowledge`（口径资料）、`request`（需求）、`data`（数据里看到的）、`assumption`（假设，不写时）、`confirmed`（人已经确认过）。旧的 `assumptions` 每条并入 `rules`，`basis` 为 `assumption`；输出的 `plan.assumptions` 总是 `[]`。
- **questions**：要人拍板的地方，最多 5 题；`text` 最多 50 个字符（解释写在 `why` 里，最多 500 个字符）；2–6 个选项，`value` 是 1–64 个字符的文字、不重复，`label` 1–50 个字符；`suggested` 是方案现在的做法，必须是某个选项的 `value`；`affects` 是受影响的指标、维度、筛选或规则的 id，最多 20 个。
- **answer**：最后在结果页给人看的那张答案卡怎么算。
  - `target` 是某张目标表；`group_by` 0–3 列、`measures` 1–8 列，`order_by`、`filter` 的列都要是这张表的列，`order_by` 的列还要在 `group_by` 或 `measures` 里。
  - `filter` 第一阶段只能写一条 `{"column", "pick": "latest"}`（取这一列最新的值）。
  - `top_n` 1–50，不写为 10。
  - `chart.type` 是 `bar`、`line` 或 `none`；不写 `chart` 时 `group_by` 不为空就是 `bar`，否则 `none`。`chart.value` 要在 `measures` 里，不写时取第一个指标；`chart.label` 要在 `group_by` 里，不写时等于 `group_by`。
  - `order_by` 不写时按第一个指标从高到低。
- **references** 仍由平台定：`[n]` 的清理和统计也看 `brief`（目标、粒度、指标和维度的说明、筛选、时间范围）、`rules`、`questions` 的 `text`、`why` 和选项的 `label`。

平台按这个顺序处理：先做下面的自动整理（“第 6 题起改成规则”除外）；再按 `answered` 把答过的题改成规则（见下）；然后还多于 5 题时，第 6 题起改成规则；最后检查。发现的不合格之处分两种处理。**让模型重写**的情况（每条一行写进 `problems`，算一次不过）：
- 第 1 版的全部检查（表、列、取值、主键、质量规则等）。
- 结构不对：类型不对或缺了上面说的必填字段，`problems` 写明位置，如 `brief.metrics[1].name 不能为空`。
- 业务文字里有代码。业务文字指：模型写的 `brief` 的 `goal`、`grain`，指标和维度的 `name`、`definition`、`unit`，筛选和时间范围的 `text`（不查 `column` 和 `examples`：一个是列名，一个是数据里的取值；平台生成的 `brief` 不查）；`rules` 的 `text`；`questions` 的 `text`、`why` 和选项的 `label`；模型写的列 `label`。先去掉 `[n]`，再按这几条找：反引号；`col(`；`F.` 加函数名和括号；`CASE WHEN`（不分大小写）；常见函数名紧跟半角括号，如 `lower(`、`upper(`、`trim(`、`coalesce(`、`cast(`、`nvl(`、`ifnull(`、`concat(`、`substr(`、`regexp_replace(`、`date_format(`、`date_trunc(`、`to_date(`、`sum(`、`count(`、`avg(`（不分大小写；前面不能紧挨着半角英文字母、数字或下划线，汉字、全角字符和标点不算，所以“按count(订单)”照样算代码）。全角括号和 `GMV(含税)` 这类缩写不算。
- 目标表列的 `source` 或质量规则的 `condition` 写成了 PySpark（字符串字面值以外出现 `F.` 加函数名和括号、`col(`、`lit(`、`.alias(`、`.cast(`、`.otherwise(`）：这两处要写 Spark SQL 表达式，答案的可加性和生成代码都按 SQL 读。每张目标表一条，写明是哪几列、哪几条质量规则。
- `brief` 的 `column` 不是结果表的列。
- id 重复。
- 题的选项超过 6 个、选项 `value` 重复或不是 1–64 个字符的文字、`text` 为空（去掉编号和前缀后只剩空白）、`text` 或选项的 `label` 超过 50 个字符、选项的 `label` 为空、`suggested` 没写或不在选项里。
- `answer` 不合上面的规则：`target` 不是目标表、列不在这张表里、`top_n` 超过 50、`chart.type` 不认识、`filter` 写了别的或不止一条等。

**平台自动整理**的情况（不让模型重写、不算不过，每处在 `cleaned` 里记一句）：
- 下面各条按列出的顺序依次做，一条对整个方案做完再做下一条（其中“第 6 题起改成规则”放在按 `answered` 处理之后，见上）。
- 质量规则名不是小写字母、数字和下划线（比如写成中文、带大写或横线），或者同一张表里重复时，改成代码能用的名字（验收后新增：以前让模型重写，模型常因此用完重写次数）：转成小写，其余字符换成下划线；什么都不剩时用 `rule_<n>`（n 是这条规则在这张表里的序号），不以字母开头时前面加 `rule_`；和这张表别的规则重名时加 `_2`、`_3`。每张表一句：“order_facts 的质量规则名改成了代码能用的名字：「数量为正」→ rule_1”。
- 接下来三条只管这几组文字，每组算一个列表：`brief.filters[].text`、`rules[].text`、`questions[].text`、每道题的 `options[].label`（选项不去重）。指标和维度的名称、说明，以及列的 `label` 不动。
- 同一个列表里有 2 条以上用同一个小写英文词加 `:`、`：`、`-`、`—` 开头（比如 `advance:`），把这个前缀从这些条目里去掉。大写缩写不去。
- 去掉条目开头的编号（`1.`、`1、`、`(1)`、`①`、`- `）和“假设：”“规则：”“问题：”“说明：”这类前缀。
- 同一个列表里去掉重复的条目（去掉空格和标点后一样就算重复），重复的题留第一个。
- 规则的 `basis` 不是上面那五个词之一时按 `assumption` 处理。
- 模型没写 `brief` 时由平台生成（在补 `label` 之前）：`goal` 取 `summary`；结果表的主键列当维度、其余列当指标，按列的顺序编号 `d1`、`d2`……和 `m1`、`m2`……（跳过已经被别的 id 用了的编号），`column` 是列名，`name` 取模型写的列 `label`（没写时用列名），`definition` 取列的 `description`，`unit` 为 null；`grain` 写成“每个{主键各列的名称，用 × 连接}一行”，名称同样取列的 `label` 或列名；没有筛选和时间范围。平台生成的 `brief` 不查“业务文字里有代码”：列的说明里常有 `sum(...)` 这类写法，不该为了一份模型没写的说明让它重写。
- 列没有 `label` 时补上：`brief` 里对应指标的“名称（单位）”或维度的名称（只看写了 `column` 的），都没有时用列名。
- 写法不合格的 id 按类别改成还没被任何 id 用过的最小编号（`m1`、`d1`、`f1`、`r1`、`q1` 这样），`affects` 里的旧 id 跟着改成新的；改完以后，`affects` 里不存在的 id 去掉。
- 题的选项 `value` 和 `suggested` 是数字或 true、false 时转成文字（`2026` → `"2026"`）；`why` 超过 500 个字符时截断；`affects` 去掉重复的，超过 20 个时只留前 20 个。
- 选项少于 2 个的题改成规则，`text` 写成“{题目}——按「{选项名称}」处理”（没有选项时只写题目），`basis` 为 `assumption`。第 6 题起的题（按 `answered` 处理之后）同样改成规则，按建议的选项写；`suggested` 没写或不在选项里时只写题目（这些题不再经过对题的检查）。题改成规则时，规则的 `id` 同样取还没被任何 id 用过的最小 `r` 编号。
- 维度的 `examples` 只留数据里真有的：去掉首尾空格、不分大小写后，等于这一列的 `source` 用到的源列在探查里列出的取值，或者等于 `source` 里的字符串字面值（如 `'未知'`）。其余去掉，`cleaned` 里写“去掉了探查里没见过的 …”。只有用到的源列取值都被探查列全了（一个没截断、个数等于去重数，如城市、状态）才这样查；用到时间、金额这类取值没列全的列，或者维度没写 `column`、探查里找不到用到的列时，平台判断不了，例子原样保留。最多留 5 个；模型没给时不补。
- 模型没写 `answer`、但有 `brief` 时由平台生成，只用写了 `column`、而且是结果表里有的列：`target` 是结果表（`targets` 里最后一张）；`time_range.column` 有值就按它 `pick: latest`；维度列去掉时间列后取前 3 个做 `group_by`；指标列取前 8 个做 `measures`；按第一个指标从高到低，`top_n` 10，`group_by` 不为空时画 `bar`。这样生成的设计一定合上面的规则。连指标都没有时 `answer` 为 null，不让模型重写。

`answered`：引用的值应是人工节点确认模式的 `record`。取到 null（第一次跑时就是）、不是对象、或者没有 `answers` 列表时都不管（节点不失败，也不记）；`answers` 里不是对象、或者 `label`、`value_label` 不是文字的项跳过。
- `record` 只有那个节点最近一次的答案，所以校验方案节点把每次读到的 `record.answers` 连同那次的 `actor` 记在自己的私有状态里，按题目（`label` 去掉空格和标点后）记，同一题新的答案覆盖旧的。
- 新方案里的题，题目（`text` 去掉空格和标点后）和记下的某一题一样，就从题里拿掉，改成一条规则 `{"id": "r5", "text": "韩梅梅已确认：统计哪段时间？——只看 2026 年", "basis": "confirmed"}`（`id` 的取法见上；`actor` 为 null 时写“已确认：……”）。方案里已经有一条 `basis` 为 `confirmed`、文字和它一样（去掉空格和标点后）的规则时，只拿掉这题，不再加规则。
- 只比题目，不比 id：模型每次重写都可能重新编号，同一个 `q1` 下一版可能是另一道题。
- 这样人答过的题，不管是哪一轮答的，模型再问也不会再让人答一遍。

数据取值的放行口子（`trust_assumptions`，第 1 版看 `assumptions` 的文字）：第 2 版看 `rules` 的文字、`questions` 的 `text`、`why` 和选项的 `label`。上一次是 `retry` 时，方案拿一个探查里没见过的取值做比较、而这些文字里提到了这个值，就放行。两版都一样：和空字符串 `''` 比较是判空（如 `trim(city) = ''`），不算拿取值做比较，不查。

读取筛选里的取值（验收后新增，各版都一样）：带读取筛选探查的表只看得到筛选后的行，筛掉的取值探查里没有，可方案常要提到它（比如“去掉 BU_X”）。所以 `known_values` 把这张表的 `filter` 拿来和列比较的字符串字面值（`=`、`<>`、`IN (…)`、`NOT IN (…)`，写法同上面判断方案的取值）也算成这一列见过的取值，方案拿它们做比较不算编造。退回给模型的问题里列出实际取值时照数据原样写，不转小写（以前写成“只有 bu_a、bu_b”，模型照着把维度转成了小写），并提醒“大小写照数据写”“不要为了通过校验改口径，比如把维度转成小写”。比较照旧不分大小写、去掉首尾空格。

第 2 版通过时：
- `plan` 是第 1 版 `EtlPlan` 的超集：`summary` 一定有值，`assumptions` 为 `[]`，另有整理后的 `brief`、`rules`、`questions` 和补齐了默认值的 `answer`（没有时为 null）；`plan.targets[].columns[]` 保留补齐后的 `label`。第 2 版用 `aep.etl.plan` 里另加的模型输出，`EtlPlan` 不动；`etl_code_check` 照旧用 `EtlPlan` 读它，多出的键不管。
- `brief` 产出同 `plan.brief`；`questions` 产出是把 `plan.questions` 改写成人工节点题目的样子：`key` 取 `id`，`label` 取 `text`，`type` 为 `select`，`choices` 取 `options`，`suggested` 照抄；`why`、`affects` 有值时照抄，模型没写时不带这个键（不写成 null）。按上面的规则整理和检查过的题，`text` 都不为空、最多 50 个字符，有 2–6 个选项，一定能通过人工节点对题的检查（见 flow-apps.md 的“确认模式的题 `fields`”），所以模板里 `dropped_fields` 总是 `[]`。
- 运行过程里那句话见 flow-apps.md 的“运行”。

#### 答案设计和可加性

`answer` 产出是方案里的答案设计，另加平台整理的两项：

```jsonc
{"target": "ads_sales_city_member_m", "filter": [{"column": "stat_month", "pick": "latest"}],
 "group_by": ["city", "member_level"], "measures": ["sales_amount", "order_count", "customer_count", "avg_order_value"],
 "order_by": [{"column": "sales_amount", "desc": true}], "top_n": 10,
 "chart": {"type": "bar", "label": ["city", "member_level"], "value": "sales_amount"},
 "columns": [{"name": "city", "label": "城市", "role": "dimension"},
             {"name": "member_level", "label": "会员等级", "role": "dimension"},
             {"name": "sales_amount", "label": "销售额（元）", "role": "metric", "agg": "sum", "unit": "元"},
             {"name": "order_count", "label": "订单数", "role": "metric", "agg": "sum", "unit": null},
             {"name": "customer_count", "label": "下单客户数", "role": "metric", "agg": "none", "unit": null},
             {"name": "avg_order_value", "label": "客单价（元）", "role": "metric", "agg": "ratio", "unit": "元",
              "numerator": "sales_amount", "denominator": "order_count"}],
 "text": "最近一个统计月里，各「城市 × 会员等级」组合按销售额从高到低排的前 10 组，附合计和柱状图。"}
```

- `columns`：先是 `group_by` 的列（`role` 为 `dimension`），再是 `measures` 的列（`role` 为 `metric`）。`label` 取列的 `label`；`unit` 取 `brief` 里对应指标的单位，没有时取 `label` 末尾括号里的字，都没有为 null。这里还没有 `format`：列的类型要运行以后才知道，见“答案怎么算”。
- `text`：确认页上“最后给你的答案”那句话，由平台按模板写：“最近一个{时间列名称}里，各「{分组列名称用 × 连接}」组合按{排序列名称}从高到低排的前 {top_n} 组，附合计和柱状图。”没有 `filter` 时去掉“最近一个……里，”；从低到高时写“从低到高”；`line` 写“折线图”，`none` 不写图；没有 `group_by` 时写成“最近一个{时间列名称}的{指标名称用、连接}合计。”名称取 `brief` 里对应指标或维度的 `name`，没有时取列的 `label`。

`agg` 说这个指标能不能跨组相加，由平台按结果表这一列的 `source` 判断，不信模型自己写的：
1. 先剥掉不改值的外壳，可以剥好几层：`round(x)`、`round(x, n)`、`bround(x, n)`、`cast(x AS 类型)`、`try_cast(x AS 类型)`、`coalesce(x, 0)`、`nvl(x, 0)`、`ifnull(x, 0)`（第二个参数是字面值 0）。
2. 剩下的最外层是 `sum(…)`（不带 DISTINCT）、`count(*)`、`count(1)`、`count(列)`（不带 DISTINCT）或 `count_if(…)`：`sum`。
3. 剩下的是 `a / b`、`try_divide(a, b)` 或 `a / nullif(b, 0)`，并且 a、b 剥壳后各自是这张表里一个 `sum` 列：写的是那一列的列名，或者和那一列的 `source` 一字不差（忽略大小写、空格和反引号）：`ratio`，`numerator`、`denominator` 记下这两列的列名。
4. 其他一律 `none`：`count(DISTINCT …)`、`sum(DISTINCT …)`、`avg`、`min`、`max`、`approx_count_distinct`、窗口函数、不聚合的列等。

所以“订单数”写成 `count(DISTINCT order_id)` 时是 `none`：平台不知道一单只落在一组里。方案先建一张一单一行的明细表、结果表里写 `count(order_id)`，就是 `sum`，客单价 `sales_amount / order_count` 也就是 `ratio`。`etl-plan-brief` 这个 Skill 会这样引导模型。

**一组有多行时**（验收后新增，jadeflow I-13）：`none` 的指标每组只取唯一的一行（`CASE WHEN count(*) = 1 THEN first(x) END`），一组有多行就算不出。以前这种组在答案里是空值，结论句再从剩下的组里挑最高的，结论是错的。现在：
- 方案可以写 `answer.add_up`：可以逐行相加、但来源不是 sum 或 count 的指标列（重量、金额、件数这类一行的值，如 `base × allocation`）。这些列按 `sum` 汇总，`columns` 里这一列 `agg` 为 `sum` 并带 `"declared": true`，`text` 末尾加一句“{指标名称}按方案说明逐行相加。”，人确认方案时看得到。来源里有不能相加的聚合时（`avg`、`min`、`max`、`count(DISTINCT …)`、`sum(DISTINCT …)`、`approx_count_distinct`、`percentile`、`first` 等）不接受：“answer.add_up 的 x 是用 avg 算出来的，不能跨行相加，请从 add_up 里去掉”；不在 `measures` 里也报错。
- 校验方案时：结果表写了 `primary_key`，主键没有全部落在 `group_by` 和 `filter` 的列里（一组会有多行），`measures` 里还有 `agg` 为 `none` 又不在 `add_up` 里的指标，就作为问题退回：“answer 按 bu、platform 汇总时一组会有多行（结果表 t 的主键是 …），x 不是 sum 或 count 算出来的，平台不知道它能不能相加，这样的组算不出来：能相加（重量、金额、件数这类）就把 x 写进 answer.add_up；不能相加就把主键都放进 group_by 或 filter，或者换一个指标”。没写主键时不查。
- 结论句：排序指标在第一组就是空值时写“各组的{指标}都算不出（一组有多行，这个指标不能直接相加）。”（值本身为空时括号里写“这些组的值是空的”）；有的组算不出时在句末加“；另有 n 组算不出（…）”，答案只取了前 `top_n` 组时写“前 n 组里另有 …”。没有分组、合计为空时写“{指标}合计算不出（…）。”

### databricks_ingest 抽取到湖仓

```jsonc
{"datasource": "{{start.datasource}}",
 "tables": "{{check_plan.sources}}",   // 写法同 etl_profile 的 tables，但不能为空
 "max_rows": 200000,                   // 每张表最多几行，1–1000000；超过时失败
 "reuse": false}                       // 可选，第二批新增：同一次运行里不重复抽取，见下；不写为 false
```

- `datasource`：不接受工具服务（`mcp`）：写死的 id 保存时报错，运行时选到的以 `input_error` 失败。
- 目标：`<AEP_DATABRICKS_SOURCE_CATALOG>.<schema>.<table>`，名字和源表一样，所以生成的代码（按 `source_catalog` 读源表）直接读到。schema 不存在时创建；表已存在时**整表覆盖**。
- 步骤（每张表）：
  1. `describe_table` 拿列和类型，再用连接器的 `read_rows` 分页读出全部行（每页 5000）。行数超过 `max_rows` 时失败，不写任何东西。
  2. 写成 JSON Lines，用 Databricks Files API 上传到 Volume `/Volumes/<catalog>/<schema>/<AEP_DATABRICKS_INGEST_VOLUME>/<datasource_id>/<run_id>-<随机 8 位>/<table>.jsonl`（每次抽取一个文件夹，两个运行同时抽同一张表也不会读到对方的文件；Volume 默认名 `aep_landing`；schema 和 Volume 不存在时用 Unity Catalog REST API 创建，Volume 为 MANAGED）。
  3. 在 `AEP_DATABRICKS_CLUSTER_ID` 集群上用命令执行接口建表：所有列先按字符串读，再按下面的类型对照转换，`CREATE OR REPLACE TABLE`；表注释写“由 AEP 从数据源「名称」抽取，运行 run_id，时间”，表属性 `aep.datasource_id`、`aep.run_id`。
  4. 建表后（成功或失败）删掉这次的落地文件和文件夹；删不掉时只在 `warnings` 里写明。
- 类型对照（按源列 `data_type` 小写匹配）：数组（以 `[]` 结尾，如 PostgreSQL 的 `integer[]`）→ STRING（存数组的 JSON 文本）；`tinyint smallint mediumint int integer` → INT，其中 `int unsigned` → BIGINT；`bigint` → BIGINT，`bigint unsigned` → DECIMAL(20,0)；`decimal(p,s) numeric(p,s)` → DECIMAL(p,s)（没有精度时 DECIMAL(38,10)）；`float real double` → DOUBLE；`bool boolean` → BOOLEAN；`date` → DATE；`datetime timestamp*` → TIMESTAMP；其他 → STRING。
- 新配置 `AEP_DATABRICKS_INGEST_VOLUME`（默认 `aep_landing`）。
- **同一次运行里不重复抽取**（第二批新增，`reuse` 为 true 时）：
  - 什么时候沿用：这个节点在这次运行里已经从同一个数据源成功抽取过，这次要的表（展开 `schema.*` 之后）和上次抽取的一样或是其中一部分。这时不读表数据、不写 Databricks（有 `schema.*` 时仍要列一次表名），`catalog` 和 `tables` 取上次这些表的记录，`reused` 为 true。
  - 什么时候重新抽：换了数据源，或者只要多了一张表，就把这次要的表全部重新抽取一遍（和第一次一样先全部读完再写，保证这些表是同一时刻的数据），`reused` 为 false。
  - 沿用时不核对 Databricks 上的表是不是还是这次运行抽的：表名和源表一样，各运行共用，可能已经被别的运行整表覆盖（同一个应用的另一次运行，或者别的数据源里同名的 schema 和表）；模板第 2 版选“改代码”时也不经过抽取，情况一样。第一阶段接受这一点，表属性 `aep.run_id` 留着以后核对用。
  - 记在哪：上次成功抽取的数据源 id、`catalog` 和完整的 `tables` 记录存在节点自己的私有状态里，抽取成功后才记；沿用时不改。
  - 为什么要：模板第 2 版里审核时选“改方案”会再经过这个节点，表没变就不用再搬一遍数据。
  - `reuse` 必须是 true 或 false，不合格时保存报错。不写时为 false，和以前一样每次都重新抽取，所以以前的配置（包括有意绕回来重新抽取的）不受影响。

产出：

| 字段 | 内容 |
| --- | --- |
| `catalog` | 写入的 catalog |
| `tables` | `[{"source": "retail.orders", "target": "`icsd-ace-dev`.retail.orders", "rows": 15000, "columns": 7}]` |
| `summary` | 一句话，如“已把 4 张表共 52350 行抽取到 Databricks：…”；沿用时为“要的 4 张表这次运行已经抽取过，沿用（共 52350 行）” |
| `reused` | 第二批新增：这次是不是沿用了这次运行上次抽取的表；`reuse` 不是 true 时总是 false |

无分支。不支持单节点试跑（会写 Databricks），`POST /apps/{app}/nodes/{node}/test` 返回 422（同 start、human、end）。

### etl_code_check 检查并打包

```jsonc
{"reply": "{{codegen.text}}",          // 必填；解析为 {"targets": {"目标表": "代码"}}，同代码应用
 "plan": "{{check_plan.plan}}",        // 整段引用，校验通过的方案
 "source_name": "{{profile.source_name}}",
 "name": "{{start.name}}",             // 可选，bundle 名；为空或不合法时用第一张目标表的名字，规则同代码应用
 "max_attempts": 3,                    // 1–5，连续不过的次数上限，规则同 etl_plan_check
 "on_stuck": "fail"}                   // 可选，验收后新增：同 etl_plan_check；stuck 时给人看的那段话放在 summary
```

- 执行：`codegen.check_code`；通过后 `codegen.render`（compute 和 job 模式按平台配置，同代码应用的 `build`）再 `codegen.check_bundle`。

产出：

| 字段 | 内容 |
| --- | --- |
| `ok` | 是否通过 |
| `code` | `{"目标表": "代码"}`，解析不了时为上一次的值或 `{}` |
| `problems` | 一条一行，通过时为 `""` |
| `bundle` | 通过时 `{"name", "language", "files": {"路径": "内容"}}`，否则 `null` |
| `files` | 第二批新增：bundle 里的文件路径，按名称排序，只有路径没有内容；不通过时为 `[]`。给人工节点的 `data` 用（意见可以挂在某个文件上），代码本身不放进 `pending` |
| `summary` | 如“已生成 Databricks bundle dws_retail_sales_city_month：databricks.yml、…” |

分支：`ok`（通过）、`retry`（重写）。

### databricks_dev_run dev 运行

```jsonc
{"bundle": "{{check_code.bundle}}",   // 整段引用
 "max_runs": 3,                        // 1–3，同代码应用的 max_dev_runs；每一轮最多运行几次，见下
 "preview_rows": 20,                   // 0–100；0 不预览
 "plan": "{{check_plan.plan}}"}        // 可选，第二批新增：整段引用校验方案通过时的 plan
```

- `plan`：必须恰好是一个引用，不合格时保存报错。节点从方案里取每张目标表的主键（给预览排序）、质量规则的处理方式（`warn`、`drop`、`fail`，给质量小结）、列的 `label`，以及第 2 版方案的 `answer` 设计（`agg` 由 `aep.etl.answer` 按上面的规则重新判断，和校验方案节点用同一个函数）。不写 `plan` 时和以前一样：不排序、不算答案。运行时取到的不是方案对象时，节点不失败，只是不排序、不算答案，`warnings` 里写明。

- 执行：`DevTarget.run`，同代码应用的 verify。通过后读每张结果表的前 `preview_rows` 行：`job`、`pipeline` 模式在 `AEP_DATABRICKS_CLUSTER_ID` 集群上读结果表（没有集群时不预览，`warnings` 里写明）；`cluster` 模式由试跑程序直接带回转换结果的前几行。同名 bundle 的运行排队进行，读预览也在队内：后一个运行不会在前一个读完预览之前改写结果表。
- 结果表全名：`job`、`pipeline` 模式是 `<AEP_DATABRICKS_CATALOG>.<AEP_DATABRICKS_SCHEMA>.<目标表>`；`cluster` 模式不落表，预览取转换结果的前几行。
- 预览排序（第二批新增，设了 `plan` 时）：每张表的预览按这张目标表的主键从小到大排，空值在前；没有主键时不排。`cluster` 模式在试跑程序里排，`job`、`pipeline` 模式在读预览的集群任务里排。主键里有列不在结果表里时，这张表的预览不排序，`sorted_by` 为 `[]`，并在 `warnings` 里写明。没设 `plan` 时预览和以前一模一样（不排序，也没有 `sorted_by`）。
- `cluster` 模式的时间值改成和另外两种模式一样不带时区的 `yyyy-MM-dd HH:mm:ss`。这一条不看 `plan`，所有 `cluster` 模式的运行都这样：下面 `preview` 一栏本来就这样写，`cluster` 模式以前没做到。
- 答案（第二批新增）：运行通过、`plan` 里有答案设计时，在集群上用 DataFrame API 算答案，见下面的“答案怎么算”；`preview_rows` 为 0 也照算。
- 每一轮最多运行几次（第二批起）：带着报错回去重写代码、再回到这个节点，还算同一轮；节点走了 `passed` 或 `failed`，这一轮就结束，下次再进来（比如审核时选“改代码”）从第 1 次重新数。计数记在节点自己的私有状态里，`on_error` 的自动重试不算次数。以前的运行里没有这个计数时，按这个节点已经跑过的次数算（同以前）。

产出：

| 字段 | 内容 |
| --- | --- |
| `status` | `passed` 或 `failed` |
| `ok` | 第二批新增：`status` 是不是 `passed`（布尔），给人工节点选项的 `requires` 用 |
| `run` | `DevRun.as_dict()`（不含 `preview`），另加 `attempt`（这一轮第几次运行）；`catalog`、`schema` 是结果表写在哪（`job` 模式取 Job 的参数，`pipeline` 模式取平台配置，不知道时为 `null`） |
| `url` | Databricks 上这次运行的链接，没有时为 `null` |
| `tables` | `[{"name", "target", "rows", "dropped", "expectations"}]` |
| `preview` | `[{"table", "target", "columns": ["…"], "rows": [{…}], "sorted_by": ["order_id"]}]`，值按 JSON 转换（日期为 `yyyy-MM-dd`，时间为不带时区的 `yyyy-MM-dd HH:mm:ss`，和源库里的写法一样；小数为数字）。`sorted_by` 第二批新增，只在设了 `plan` 时有：按哪几列排的，没有主键时为 `[]`；没设 `plan` 时没有这个键 |
| `quality` | 第二批新增：所有结果表的质量小结 `{"rules": 5, "failed_rules": 0, "dropped": 12, "text": "5 条质量规则全部通过，丢弃 12 行"}`，见下；没有任何表结果时为 null |
| `answer` | 第二批新增：答案，见“答案怎么算”；没有答案设计或运行没通过时为 null |
| `errors` | 失败时的报错，一条一行，每条最多 1500 字符；通过时为 `""` |
| `report` | Markdown 报告：成功或失败、运行方式、链接；每张结果表的行数、被丢弃行数、质量规则结果；第二批起有答案时加“### 答案”一节（结论一句话；`rows` 全部行的表格，表头用 `columns[].label`，数字按 `columns[].format` 写（同结论），列不省略，下面预览表格的“最多 10 列”不管这里；有 `group_by` 时最后加一行合计，第一个分组列写“{period} 合计”（没有 `period` 时写“合计”），其余分组列留空，`none` 指标写“—”；`truncated` 时表格下面注明“共 27 组，显示前 10”；`rows` 为空时不画表格；算不出来时写“答案没算出来：原因”）；每张表的预览表格（最多 10 列，超出的列省略并注明）。表全名带反引号（如 `` `icsd-ace-dev`.sales.orders ``），在 Markdown 里写成双反引号的行内代码 |

`quality`：`rules` 是 `tables[].expectations` 的条数之和（没跑到质量规则的表不算）；`dropped` 是 `tables[].dropped` 之和；`failed_rules` 是其中有不通过的行、而处理方式是 `warn` 或 `fail` 的规则条数（处理方式按规则名对方案里这张表的质量规则；`drop` 规则不通过的行已经算在 `dropped` 里；运行通过时 `fail` 规则不会有不通过的行，运行失败时它照样算进来，免得报错说质量规则要求整次更新失败、小结却写“全部通过”）。没设 `plan`、或者方案里找不到同名的规则时不知道处理方式，有不通过的行的都算进 `failed_rules`。`text` 的写法：“5 条质量规则全部通过”“5 条质量规则里 1 条有不通过的行”，丢弃了行时接“，丢弃 12 行”；没有质量规则时为“没有质量规则”。

分支：`passed`（通过）、`retry`（失败且 `fixable`，且这是这一轮第 `max_runs` 次以前）、`failed`（其他失败）。显示文字：通过、带着报错重写、失败。第二批起按轮计数（见上）；以前写的“运行次数在这次运行里累计，不随回到写代码清零”不再成立。模板第 1 版里 `passed`、`failed` 都直接到结束，一次运行只有一轮，结果和以前一样。

#### 答案怎么算

`answer` 产出：

```jsonc
{"status": "ok",                                   // ok 或 error；error 时见下
 "target": "`icsd-ace-dev`.trac_b_test.ads_sales_city_member_m",   // job、pipeline 模式是结果表全名，不知道 catalog 或 schema 时写目标表名；cluster 模式是目标表名
 "period": "2026-10",                              // `pick: latest` 取到的值，转成文字；没有 filter 时为 null
 "columns": [{"name": "city", "label": "城市", "role": "dimension", "format": null},
             {"name": "member_level", "label": "会员等级", "role": "dimension", "format": null},
             {"name": "sales_amount", "label": "销售额（元）", "role": "metric", "agg": "sum", "unit": "元", "format": "decimal"},
             {"name": "order_count", "label": "订单数", "role": "metric", "agg": "sum", "unit": null, "format": "integer"},
             {"name": "customer_count", "label": "下单客户数", "role": "metric", "agg": "none", "unit": null, "format": "integer"},
             {"name": "avg_order_value", "label": "客单价（元）", "role": "metric", "agg": "ratio", "unit": "元",
              "numerator": "sales_amount", "denominator": "order_count", "format": "decimal"}],   // 同校验方案产出的 answer.columns，另加 format
 "rows": [{"city": "上海", "member_level": "gold", "sales_amount": 1234567.0, "order_count": 5035,
           "customer_count": 812, "avg_order_value": 245.19702085402184}],      // 集群算出的原值，不舍入
 "totals": {"sales_amount": 6709854.12, "order_count": 55188, "customer_count": null, "avg_order_value": 121.58175907806044},
 "mins": {"sales_amount": 18342.6, "order_count": 61},   // 这一期各组里每个 sum 指标的最小值，判断占比用
 "groups": 27,                                     // 这一期一共几组
 "row_count": 324,                                 // 整张结果表的行数
 "truncated": true,                                // groups 比 top_n 多
 "conclusion": "2026-10，上海 · gold 的销售额最高：1,234,567.00 元，占合计 18.4%。",
 "chart": {"type": "bar", "label": ["city", "member_level"], "value": "sales_amount"},   // 照抄答案设计
 "quality": {"rules": 5, "failed_rules": 0, "dropped": 12, "text": "5 条质量规则全部通过，丢弃 12 行"},   // 同节点的 quality
 "sql": "SELECT `city`, `member_level`, sum(`sales_amount`) AS `sales_amount`, … LIMIT 10",
 "url": "https://…",                               // 同节点的 url
 "error": null}
```

`status` 为 `error` 时，`target`、`columns`、`chart`、`quality`、`sql`、`url`、`error` 照常有（来自答案设计和节点，不靠集群算；列的类型不知道，`format` 都为 null）；`rows` 为 `[]`，`totals`、`mins` 为 `{}`，`period`、`groups`、`row_count`、`truncated` 为 null，`conclusion` 为 `""`。

计算规则：
- **分工**：答案设计作为数据传给集群，集群上用 DataFrame API 算出 `period`、`rows`、`totals`、`mins`、`groups`、`row_count`，并带回每列的类型；`columns[].format`、结论、`sql` 由平台的 `aep.etl.answer` 写，不经过模型。`cluster` 模式在试跑程序里算（转换结果），`job`、`pipeline` 模式在读预览的集群任务里读结果表算。`job`、`pipeline` 模式读不了结果表时，不管什么原因（没配集群、不知道结果表在哪个 catalog 和 schema、读预览的集群任务出错），`status` 都是 `error`，`error` 写明原因；预览照旧只在 `warnings` 里写明。
- 两份集群程序（`cluster_harness.py` 和 `cluster_tasks.py`）不能导入 `aep`，各带一份一模一样的读行和算答案的辅助函数，有测试保证两份一致。
- `cluster_harness.py` 会原样打进 `job` 模式 bundle 的 `src/aep_harness.py`，代码应用 `etl` 的也一样。所以第二批以后，同样的代码打出的 bundle 里这个文件会变：结果的 `files`、`files.zip` 和资产仓库的 PR 里都会看到这个文件的改动，上线到 prod 的 job 跑的也是新文件。`run_notebook` 写表的行为不变：新加的排序和算答案只在 spec 带 `preview` 或 `answer` 时才执行，job 的 notebook 不带。
- **列名复核**：集群先拿设计里的列和 `df.columns` 对，缺列时 `status` 为 `error`，`error` 写“结果表里没有列 xxx”，节点照常走 `passed`。算的过程中出错也一样。
- **`pick: latest`**：取这一列非空值里最大的，按列的类型比较（文字按字符串比，所以 Skill 要求月份写成 `2026-10` 这种定长写法），在整张表上取。`period` 是这个值转成的文字（日期 `yyyy-MM-dd`，时间 `yyyy-MM-dd HH:mm:ss`，数字原样）。
- **值的写法**：`rows`、`totals`、`mins` 里的值按 `preview` 的规则转成 JSON：日期 `yyyy-MM-dd`，时间不带时区的 `yyyy-MM-dd HH:mm:ss`，DECIMAL 等数字为 JSON 数字，不舍入。分组列和 `none` 指标的值也一样。
- **分组**：筛出这一期的行，按 `group_by` 分组：`sum` 的列相加；`ratio` 的列等于分子列之和除以分母列之和，分母为 0 或空时为 null；`none` 的列一组只有一行时取它的值，否则为 null。`ratio` 的分子、分母列不在 `measures` 里也照样参与计算。所以答案最好指向粒度正好是“时间列 + `group_by`”的结果表，这时每组只有一行，`none` 的指标也照常显示。没有 `group_by` 时整期算一组。
- **这一期没有行**（表是空的，或者 `pick` 的列全是空值、`period` 为 null）：`rows` 为 `[]`，`groups` 为 0，`truncated` 为 false，`totals` 和 `mins` 里每个指标为 null。没有 `group_by` 时也一样：Spark 不分组的聚合在零行上会返回一行全是空值，这一行不算一组。
- **排序和截取**：按 `order_by` 排（空值在后）；`order_by` 的值都相同时，再按 `group_by` 各列从小到大排（空值在前），所以 `rows` 和结论每次都一样，`sql` 的 ORDER BY 也这样写。取前 `top_n` 组放进 `rows`。`groups` 是这一期一共几组，`truncated` 是 `groups` 是否大于 `top_n`。
- **合计 `totals`**：这一期的全部组，不是前 `top_n` 组：`sum` 相加，`ratio` 用分子、分母的合计重新算，`none` 为 null（页面显示“—”）。
- **最小值 `mins`**：这一期全部组里每个 `sum` 指标的最小值。平台只拿到前 `top_n` 组，看不到其余组的值，靠它判断占比的第二个条件。
- **列的类型 `columns[].format`**：指标列按集群算出的这一列的类型定：整数类型（TINYINT、SMALLINT、INT、BIGINT，`count` 出来的就是）为 `integer`；其他数字类型（DECIMAL、FLOAT、DOUBLE）为 `decimal`，`ratio` 列一律 `decimal`。维度列和不是数字的指标列为 null。
- **结论 `conclusion`**：
  - 有 `group_by`、`order_by` 的第一列是指标时：`{period}，{这一组各分组列的值用“ · ”连接}的{指标名称}最高：{值} {单位}，占合计 {百分比}%。`（有单位时数字和单位之间空一格，没有单位时不写）；从低到高时写“最低”。
  - 占比只在三个条件都成立时写：这个指标是 `sum`、这一期每组的值都不小于 0（看 `mins`）、合计大于 0；否则去掉“，占合计 …%”。
  - 几种写法按这个顺序选，前一种用上了就不看后面的：
    1. 这一期没有行（`groups` 为 0）：`{period}没有符合条件的数据。`
    2. 没有 `group_by`：`{period}，{指标名称}合计：{值} {单位}。`指标取 `order_by` 的第一列（不写 `order_by` 时就是第一个指标），值取 `rows` 里唯一那一行的这一列（不取 `totals`：`none` 指标整期只有一行时 `rows` 里有值，`totals` 里总是 null）。
    3. `order_by` 的第一列是分组列：`{period}共 {groups} 组，按{列名称}排序。`
    4. 其余（有 `group_by`、`order_by` 的第一列是指标）：上面第一条的“最高”“最低”写法。
  - 没有 `period` 时去掉开头的“{period}，”或“{period}”；值为 null 时写“—”，不写单位和占比。
  - 指标名称取 `brief` 里对应指标的 `name`，没有时取列的 `label` 去掉末尾括号里的字；分组列的值照 `rows` 里转换后的文字写。
  - 数字按列的 `format`，不按值：`integer` 加千分位；`decimal` 一律保留 2 位小数并加千分位，值正好是整数也一样（`1234567.0` 写成 `1,234,567.00`）；`format` 为 null 时原样写。百分比保留 1 位小数。结果页和审核页上答案的表格、合计一行也按 `format` 显示：JSON 到了浏览器分不出 `1234567.0` 和 `1234567`，所以页面同样不看值里有没有小数点。下载的 CSV 用原值，不格式化。
- **`sql`**：给人复制看的等价查询，只展示，平台不执行。标识符一律加反引号，字面值按 SQL 规则转义。`pick: latest` 只在 `period` 就是原值时写成 `= period` 的字面值（列是文字、日期或整数）；其他类型（时间只写到秒，小数和浮点数经过 JSON 的浮点数）和不知道 `period` 时写成 `= (SELECT max(列) FROM 表)`。没有 `group_by` 时加 `HAVING count(*) > 0`：不分组的聚合在零行上也返回一行空值，而答案这时没有行。
- 页面上“合计”一行写成“{period} 合计”，没有 `period` 时写“合计”。

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

文档放在 `demo/knowledge/<知识库目录>/`，Markdown，每篇 1–3 屏，中文，写成企业内部文档的样子（有标题、适用范围、版本和维护部门），开头的“要点”一行用一两句话写出这篇最关键的规则（文档按小标题切片，各段离开标题后不好检索，多主题的需求主要靠开头这段命中）。口径里的取值必须和示例库完全一致（`paid`、`测试`、`gold` 等）。

- `retail-metrics/`，知识库名“零售数据口径”，说明“零售业务的指标定义和统计口径：有效订单、销售额、客单价、城市和会员等级的归一、时间口径、客户数和复购、毛利”。文档：`01-有效订单定义.md`、`02-销售额与客单价.md`、`03-城市与会员等级.md`、`04-时间口径.md`、`05-客户数与复购.md`、`06-毛利.md`，内容见设计文档“两个知识库”一节。
- `dw-standards/`，知识库名“数仓开发规范”，说明“数据平台团队的数仓开发规范：分层和命名、主键和字段类型、数据质量规则、加载策略、PySpark 写法”。文档：`01-分层与命名.md`、`02-主键与字段.md`、`03-数据质量规则.md`、`04-加载策略.md`、`05-PySpark编码规范.md`。PySpark 规范必须和 `aep.etl.codegen` 的静态检查一致，不能建议检查会拒绝的写法。

### 应用模板 `etl-lakehouse`

`TEMPLATES` 新增 `etl-lakehouse`，标题“数据 ETL（湖仓）”，说明“探查数据源、按口径知识库出方案、经人确认后抽取到 Databricks，按开发规范写 PySpark 代码并在 dev 运行，预览结果数据”。`needs` 为 `("datasource", "knowledge_base")`，新建时和 `knowledge-qa` 一样走“版本 1 空白、模板配置进草稿”。

这一节写的是现在的第 1 版。第二批改成第 2 版，见下一节“应用模板 `etl-lakehouse` 第 2 版”；实现第 2 版的 PR 合并时，删掉这一节的节点表、连线和两段提示词，换成下一节的内容。

节点（每个都写好 `note`）：

| id | 类型 | 标题 | 主要设置 |
| --- | --- | --- | --- |
| `start` | start | 开始 | 输入 `datasource`（数据源，必填）、`request`（需求，textarea，必填）、`tables`（表范围，text）、`language`（select：pyspark、sql，默认 pyspark）、`name`（bundle 名，text） |
| `profile` | etl_profile | 探查数据 | `datasource: {{start.datasource}}`，`tables: {{start.tables}}`，出错重试 2 次 |
| `glossary` | knowledge | 查业务口径 | `query: {{start.request}}`，`top_k` 12，混合检索；知识库留空 |
| `standards` | knowledge | 查开发规范 | 固定的 `query: "分层与命名、主键与字段类型、数据质量规则、加载策略、PySpark 编码规范"`（规范每次都要查全，不随需求变：检索词是各篇的主题，`top_k` 20 几乎取全），混合检索；知识库留空。放在出方案之前：方案里的表名、主键、字段类型、质量规则和加载方式都按规范定，写代码时再用一次 |
| `plan` | llm | 出方案 | Skill `etl-plan` 版本 1（内置；`make demo` 改成最新发布的版本），`system` 为 `aep.etl.plan.OUTPUT_FORMAT`，输出 text，`max_visits` 10，出错重试 1 次，`quiet: true`（回答是给校验节点的 JSON，不写进运行过程）；提示词见下 |
| `check_plan` | etl_plan_check | 校验方案 | `reply: {{plan.text}}`，`profile: {{profile.tables}}`，`hits: ["{{glossary.hits}}", "{{standards.hits}}"]`，`language: {{start.language}}`，`max_visits` 10 |
| `review` | human | 确认方案 | 确认模式，`show: {{check_plan.message}}`，选项确认、驳回，驳回必须写意见，`max_visits` 5 |
| `ingest` | databricks_ingest | 抽取到湖仓 | `datasource: {{start.datasource}}`，`tables: {{check_plan.sources}}`，出错重试 2 次 |
| `codegen` | llm | 写代码 | Skill `etl-codegen`，`system` 为 `aep.etl.codegen.OUTPUT_FORMAT`，输出 text，`max_visits` 10，出错重试 1 次，`quiet: true`（回答是给校验节点的 JSON，不写进运行过程）；提示词见下 |
| `check_code` | etl_code_check | 检查并打包 | `reply: {{codegen.text}}`，`plan: {{check_plan.plan}}`，`source_name: {{profile.source_name}}`，`name: {{start.name}}`，`max_visits` 10 |
| `dev_run` | databricks_dev_run | dev 运行 | `bundle: {{check_code.bundle}}`，`max_visits` 3，出错重试 2 次 |
| `end` | end | 结束 | 输出 `report` ← `{{dev_run.report}}`，`plan` ← `{{check_plan.plan}}`，`ingested` ← `{{ingest.summary}}` |

每个节点在运行过程里说什么，见 [flow-apps.md](flow-apps.md#运行)。

“出错重试 n 次”是 `on_error: {"retry": {"max": n, "interval_seconds": 5}, "then": "stop"}`（模型节点间隔 2 秒）：连接器或集群一时忙、模型网关超时这类会自己好的错误，先自动再试，仍不行才停下。

连线：start → profile → glossary → standards → plan → check_plan；check_plan `ok` → review，`retry` → plan；review `approve` → ingest，`reject` → plan；ingest → codegen → check_code；check_code `ok` → dev_run，`retry` → codegen；dev_run `passed` → end，`retry` → codegen，`failed` → end。

`plan` 的提示词：

```
需求：{{start.request}}
期望的代码语言：{{start.language}}

源表（只能使用这些）：
{{profile.description}}

公司数据口径（参考资料）：
{{glossary.context}}

数仓开发规范（参考资料）：
{{standards.context}}

需求里的指标和口径资料有关时按口径定义；目标表的分层和命名、主键、字段类型、质量规则和加载方式按开发规范。在 summary、assumptions、目标表和字段说明里用 [n] 标注依据；没有资料依据的内容不要编号。资料和需求冲突时以需求为准，并写进 assumptions。

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

### 应用模板 `etl-lakehouse` 第 2 版（第二批）

模板 id、标题和 `needs` 不变，说明改为“探查数据源、按口径知识库出方案，确认方案后抽取到 Databricks，按开发规范写 PySpark 代码，在开发环境试算并算出答案，审核通过后交付”。模板配置是复制进应用的，所以已经用第 1 版建好的应用和以前的运行都不变；`make demo` 会把 `retail-etl` 发布成第 2 版。

第一阶段整个流程只有数据团队一方：提需求、确认方案、审核试算结果都是数据团队的人做，名字自己填。只给业务方看的业务语言页面放到第二阶段。

和第 1 版比：出方案改用 Skill `etl-plan-brief`、输出方案第 2 版；确认方案时要回答问题，改了建议答案或写了意见就按意见重写方案；表没变时不重复抽取；`dev_run` 的标题改成“开发环境试算”（节点类型名和运行过程里的那句话仍叫“dev 运行”），按轮计数、预览按主键排序、算出答案；试算后新加一步“审核试算结果”，可以通过、改代码、改方案或终止；两个结束节点分别是交付和终止。

上线顺序：保存检查会拒绝引用节点没有的设置和产出（“节点 dev_run 没有产出 answer”），所以模板引用到的都要在模板之前或和它一起上。第二批按“方案第 2 版 → 人工节点扩展和模板第 2 版 → 算答案 → 专门页面和结果页”的顺序做，每步一个 PR：
- `databricks_dev_run` 的 `plan` 设置和 `ok`、`quality`、`answer` 产出，以及 `etl_code_check` 的 `files` 产出，和模板第 2 版一起上。
- 算答案上线之前，`answer` 总是 null，设了 `plan` 的预览也还不排序、不带 `sorted_by`；其他产出的形状和最终一样。
- 校验方案节点的 `answered` 在方案第 2 版那个 PR 里只用节点单测验证（直接给状态），带它的配置要等人工节点有了 `record` 才能保存。
- 确认模式答题的通用页面也和模板第 2 版一起上：就是 flow-apps.md “页面怎么用这些”里所有编排应用通用的部分，显示 `pending.fields`、每题点选、标“建议”和“为什么问”，有 `changed_branch` 时换主按钮，提交时带 `values` 和 `seen`，收到 409 “内容已经更新”就重新读运行（“你的名字”框现在就有）。现在的运行页确认时只发 `comment`，不一起上的话，`make demo` 发布的 `retail-etl` 和用模板新建的应用一出题就卡在“确认方案”：每题必答，页面答不了，提交都返回 422。两种 `view` 的专门页面、挂在条目上的意见和结果页留到最后一步；在那之前两个人工步骤照旧显示 `show`，只能写总体意见。
- 开始节点输入的 `advanced`、`examples`（保存检查和报错、编辑页和新建运行页上的显示）和文案清理一起做，可以和这几个 PR 并行。现在的输入检查不看多出来的键，`AppDetail.inputs`、`RunDetail.inputs` 本来就原样带上开始节点的输入，所以模板第 2 版先写上 `advanced: true`、`make demo` 先填 `examples` 也能保存；在那之前新建运行页照旧显示全部输入，不收起，也不显示例子。

节点（每个都写好 `note`，不再提“假设”“驳回”）：

| id | 类型 | 标题 | 主要设置（和第 1 版一样的不再写） |
| --- | --- | --- | --- |
| `start` | start | 开始 | 输入同第 1 版；`request` 的 `examples` 为 `[]`（`make demo` 填演示需求）；`tables`、`language`、`name` 设 `advanced: true` |
| `profile` | etl_profile | 探查数据 | 同第 1 版 |
| `glossary` | knowledge | 查业务口径 | 同第 1 版 |
| `standards` | knowledge | 查开发规范 | 同第 1 版 |
| `plan` | llm | 出方案 | Skill `etl-plan-brief` 版本 1（内置；`make demo` 改成最新发布的版本），`system` 为 `aep.etl.plan.OUTPUT_FORMAT_V2`；提示词见下；`max_visits` 10、出错重试 1 次、`quiet: true` 同第 1 版 |
| `check_plan` | etl_plan_check | 校验方案 | 同第 1 版，另加 `format: 2`、`answered: {{review.record}}`（`record` 只有最近一次的答案，以前各轮的由校验方案节点自己记着）、`on_stuck: "branch"`（验收后新增） |
| `plan_stuck` | human | 方案校验没通过 | 验收后新增，见下 |
| `review` | human | 确认方案 | 见下 |
| `ingest` | databricks_ingest | 抽取到湖仓 | 同第 1 版，另加 `reuse: true`，`max_visits` 6 |
| `codegen` | llm | 写代码 | 同第 1 版，提示词见下 |
| `check_code` | etl_code_check | 检查并打包 | 同第 1 版，另加 `on_stuck: "branch"`（验收后新增） |
| `code_stuck` | human | 代码检查没通过 | 验收后新增，见下 |
| `dev_run` | databricks_dev_run | 开发环境试算 | `bundle: {{check_code.bundle}}`，`plan: {{check_plan.plan}}`，`max_runs` 3，`preview_rows` 20，`max_visits` 9，出错重试 2 次 |
| `data_review` | human | 审核试算结果 | 见下 |
| `end` | end | 交付 | `outcome: "delivered"`，输出见下 |
| `stopped` | end | 终止 | `outcome: "stopped"`，输出见下 |

两个人工节点：

```jsonc
{"id": "review", "type": "human", "title": "确认方案", "mode": "approval",
 "prompt": "请检查方案：业务说明（指标、维度、筛选、时间范围）、技术方案和已按口径处理的规则，并回答每道问题。确认后平台把用到的源表抽取到 Databricks；改了建议答案或写了意见，平台会按意见重写方案，再请你确认一次。",
 "view": "etl_plan_review",
 "show": "{{check_plan.message}}",
 "data": {"brief": "{{check_plan.brief}}", "plan": "{{check_plan.plan}}", "answer": "{{check_plan.answer}}",
          "uncited": "{{check_plan.uncited}}", "cleaned": "{{check_plan.cleaned}}",
          "dropped": "{{check_plan.dropped}}",
          "returned": "{{data_review.record}}", "previous": "{{review.record}}"},
 "fields": "{{check_plan.questions}}",
 "changed_branch": "revise",
 "options": [{"key": "approve", "label": "确认方案，开始做"}, {"key": "revise", "label": "按我的意见修改方案"}],
 "comment_required": ["revise"],
 "max_visits": 5}

{"id": "data_review", "type": "human", "title": "审核试算结果", "mode": "approval",
 "prompt": "请看开发环境试算的结果、代码和方案。通过后交付；改代码、改方案时写明意见，可以挂在某一列、某条质量规则或某个文件上；终止时写明原因。",
 "view": "etl_dev_review",
 "show": "{{dev_run.report}}",
 "data": {"brief": "{{check_plan.brief}}", "plan": "{{check_plan.plan}}", "uncited": "{{check_plan.uncited}}",
          "confirmed": "{{review.record}}", "status": "{{dev_run.status}}", "tables": "{{dev_run.tables}}",
          "preview": "{{dev_run.preview}}", "answer": "{{dev_run.answer}}", "quality": "{{dev_run.quality}}",
          "url": "{{dev_run.url}}", "errors": "{{dev_run.errors}}", "files": "{{check_code.files}}"},
 "options": [{"key": "approve", "label": "通过", "requires": "{{dev_run.ok}}"},
             {"key": "fix_code", "label": "改代码"}, {"key": "fix_plan", "label": "改方案"},
             {"key": "stop", "label": "终止"}],
 "comment_required": ["fix_code", "fix_plan", "stop"],
 "max_visits": 4}
```

两个交给人的节点（验收后新增，第 3 版模板同样有）：校验方案或检查并打包连续 3 次不过时，不再整条运行报错终止，而是把最后一版和问题交给人。人写意见让模型重写（意见和平台的问题一起交给模型，重写次数重新算），或者终止。

```jsonc
{"id": "plan_stuck", "type": "human", "title": "方案校验没通过", "mode": "approval",
 "prompt": "模型连续几次没写出能通过平台检查的方案。请看问题和最后一版方案：写明怎么改，让模型按意见重写；或者终止这次运行并写明原因。",
 "show": "{{check_plan.message}}",
 "data": {"problems": "{{check_plan.problems}}", "plan": "{{check_plan.plan}}"},
 "options": [{"key": "rewrite", "label": "按意见重写"}, {"key": "stop", "label": "终止"}],
 "comment_required": ["rewrite", "stop"],
 "max_visits": 3}

{"id": "code_stuck", "type": "human", "title": "代码检查没通过", "mode": "approval",
 "prompt": "模型连续几次没写出能通过平台检查的代码。请看问题和最后一版代码：写明怎么改，让模型按意见重写；或者终止这次运行并写明原因。",
 "show": "{{check_code.summary}}",
 "data": {"problems": "{{check_code.problems}}", "code": "{{check_code.code}}"},
 "options": [{"key": "rewrite", "label": "按意见重写"}, {"key": "stop", "label": "终止"}],
 "comment_required": ["rewrite", "stop"],
 "max_visits": 3}
```

两个结束节点的输出：

| 结束节点 | 输出 |
| --- | --- |
| `end`（交付） | `report` ← `{{dev_run.report}}`，`plan` ← `{{check_plan.plan}}`，`ingested` ← `{{ingest.summary}}`，`brief` ← `{{check_plan.brief}}`，`answer` ← `{{dev_run.answer}}`，`code` ← `{{check_code.bundle}}`，`quality` ← `{{dev_run.quality}}`，`dev_url` ← `{{dev_run.url}}`，`confirmed` ← `{{review.record}}`，`reviewed` ← `{{data_review.record}}` |
| `stopped`（终止） | `reason` ← `["{{data_review.comment}}", "{{data_review.feedback}}", "{{code_stuck.comment}}", "{{plan_stuck.comment}}"]`（只挂了意见、没写总体意见时取 `feedback`；后两项是验收后新增的交给人的节点选终止时写的原因），`brief`、`plan`、`report`、`confirmed`、`reviewed` 同上；没有 `answer` 和 `code`：没通过审核的数字和代码不作为结果交付（代码仍可以用 `files.zip` 下载） |

连线：

```text
start → profile → glossary → standards → plan → check_plan
check_plan   ok → review            | retry → plan         | stuck → plan_stuck
plan_stuck   rewrite → plan         | stop → stopped
review       approve → ingest       | revise → plan
ingest → codegen → check_code       check_code ok → dev_run | retry → codegen | stuck → code_stuck
code_stuck   rewrite → codegen      | stop → stopped
dev_run      passed → data_review   | failed → data_review | retry → codegen
data_review  approve → end          | fix_code → codegen   | fix_plan → plan | stop → stopped
```

- 次数上限：`plan_stuck`、`code_stuck` 各 3 次；`review` 5 次（最多改 4 轮方案）；`data_review` 4 次（最多退回 3 次）；`ingest` 6 次（每次确认方案后经过一次）；`dev_run` 一次运行总共最多 9 次（每轮最多 3 次）：前几轮用得多时，后面的轮可能一次都跑不了，运行失败；`plan`、`check_plan`、`codegen`、`check_code` 照旧 10 次（`max_visits` 的上限）。到上限时运行失败，不能重试（见 flow-apps.md 的公共字段）。
- 循环里的引用（第一次跑时为空）：出方案的提示词读 `{{plan_stuck.feedback}}`、`{{review.feedback}}`、`{{data_review.feedback}}`；写代码的提示词读 `{{code_stuck.feedback}}`、`{{data_review.feedback}}`；`check_plan.answered` 读 `{{review.record}}`（只有最近一次的答案，以前读到的由校验方案节点自己记着）；`review.data` 读 `{{data_review.record}}` 和它自己的 `{{review.record}}`。
- 意见怎么传：两个人工节点选“确认方案，开始做”或“通过”时 `feedback` 清空，所以旧意见不会被反复发给模型。选“按我的意见修改方案”或“改方案”时，`plan` 带着意见重写方案，回到“确认方案”再确认一次；选“改代码”时 `codegen` 带着意见改代码，重新检查、试算，再回到“审核试算结果”；选“终止”时运行停在 `stopped`，`reason` 是意见。
- 选“改方案”后再次确认，要的表上次都抽取过时 `ingest` 不再复制数据（见 databricks_ingest）。`dev_run` 每进来一轮都从第 1 次数起。

`plan` 第 2 版的提示词：

```
需求：{{start.request}}
期望的代码语言：{{start.language}}

源表（只能使用这些）：
{{profile.description}}

公司数据口径（参考资料）：
{{glossary.context}}

数仓开发规范（参考资料）：
{{standards.context}}

需求里的指标和口径资料有关时按口径定义；目标表的分层和命名、主键、字段类型、质量规则和加载方式按开发规范。在 brief、rules、questions、目标表和字段说明里用 [n] 标注依据；没有资料依据的内容不要编号。资料和需求不一致时不要自己选：需求明确说这次要改掉资料里的做法的，按需求写，写进 rules 并说明和资料哪里不同；否则写成 questions，列出资料的写法和需求的说法，方案先按资料写。需求没说清、要人拍板的地方写成 questions，方案按建议的选项写。需求里要的东西（一条检查、一列、一种算法）平台不支持、校验不通过时，不要悄悄删掉或换掉：写成 questions，说明需求要的是什么、平台能怎么做、建议怎么替代，方案按建议的替代做法写。

上一版方案（第一次出方案时为空，忽略）：{{check_plan.plan}}
平台校验发现的问题（为空时忽略）：{{check_plan.problems}}
校验连续没通过、交给人时人的意见（为空时忽略）：{{plan_stuck.feedback}}
确认方案时的意见（为空时忽略）：{{review.feedback}}
审核试算结果时的意见（为空时忽略）：{{data_review.feedback}}
意见开头写着第几轮、哪一步、什么决定。校验连续没通过时人的意见是针对上一版方案和这些问题写的，按它和问题一起改，只改格式和写法，不要为了通过校验改口径。两段意见都有时，确认方案时的意见是最新的，两者冲突时以它为准；审核试算结果时的意见上一版方案已经按它改过。只处理最新一轮、和方案有关的意见；上一版方案里已经按它改过的，不要重复处理。有上一版方案时，在它的基础上按问题和意见修改，输出完整方案。
```

`codegen` 第 2 版的提示词：

```
已确认的方案：
{{check_plan.plan}}

源表：
{{profile.description}}

开发规范（参考资料）：
{{standards.context}}

上一版代码（第一次写时为空，忽略）：{{check_code.code}}
静态检查发现的问题（为空时忽略）：{{check_code.problems}}
检查连续没通过、交给人时人的意见（为空时忽略）：{{code_stuck.feedback}}
Databricks 开发环境试算的报错（为空时忽略）：{{dev_run.errors}}
审核试算结果时的意见（为空时忽略）：{{data_review.feedback}}
意见开头写着第几轮、哪一步、什么决定。只处理和代码有关的意见；上一版代码里已经按它改过的，不要重复处理。有上一版代码时，按问题、报错和意见修改，输出全部目标表的完整代码。
```

#### 内置 Skill `etl-plan-brief`

第二批新增的内置 Skill，版本 1，写出方案第 2 版的做法（`OUTPUT_FORMAT_V2` 管 JSON 的样子，Skill 管怎么做，两者分开维护）：
- 先写业务说明 `brief`：目标、粒度、每个指标怎么算（引用口径 `[n]`）、维度、筛选、时间范围。业务说明给人看，不写代码、源表名和 bundle 名，也不用英文前缀。
- `rules`：已经按口径资料、开发规范或数据定下的处理（比如城市名去掉“市”），写明依据。
- `questions`：只问会改变结果、需求又没说清的事，最多 5 题；每题 2–6 个选项，标出建议的选项（就是方案现在的做法），写明为什么问、影响哪些指标。
- `answer`：指向粒度正好是“时间列 + 分组列”的结果表；月份这类时间列写成 `2026-10` 这样的定长文字；指标尽量可加：计数在一行一个对象的明细表上数（`count(order_id)`，不用 `count(DISTINCT …)`），比值写成两个可加列相除。
- 不再写 `assumptions`，要说明的假设写进 `rules`，`basis` 写 `assumption`。第 1 版的取值检查原样沿用，平台退回的问题里可能还说“在 assumptions 里说明”，也照这样写进 `rules`（平台两种写法都收，旧的 `assumptions` 会并入 `rules`）。

为什么用新名字，而不是把 `etl-plan` 发布成版本 2：平台启动时只在内置 Skill 的名字还不存在时登记版本 1（见 apps-and-skills.md），已有环境里早就有 `etl-plan`，内置内容改了也到不了那里；新名字在下次启动时就会登记。`etl-plan` 不变，代码应用 `etl` 和模板第 1 版照旧用它。

#### 两个人工步骤的页面

两种 `view` 都显示在运行页主栏顶部，占满宽度，右栏只留小结和按钮。`view` 只管页面怎么显示；平台按 flow-apps.md 的通用规则检查 `data` 和意见，不懂 ETL。

`etl_plan_review`（确认方案）读 `data` 的：

| 键 | 来自 | 页面上 |
| --- | --- | --- |
| `brief` | 校验方案的 `brief` | “我们理解你要的是”：目标和粒度；指标表（名称、单位、怎么算）；维度（说明和例子）；筛选；时间范围。`[n]` 显示成可以点的小标签 |
| `uncited` | 校验方案的 `uncited` | 这些指标、维度、筛选标成黄色，写“没找到口径文档，按需求理解” |
| `plan` | 校验方案的 `plan` | “技术方案”：每张目标表的列（中文名、Spark 表达式用等宽字体、说明）、主键、加载方式、质量规则；`rules` 是“已按口径处理”，按依据分组、默认收起；`references` 是“依据的资料”，只列方案引用了的 |
| `answer` | 校验方案的 `answer` | “最后给你的答案”：`text`，以及“结果表长这样”：答案用的那张表的列，维度列给 `brief` 里的例子，指标列写“运行后得出”；为 null 时不显示这块 |
| `cleaned` | 校验方案的 `cleaned` | 平台自动整理过的地方，默认收起 |
| `dropped` | 校验方案的 `dropped` | 不为空时顶部写“方案被平台校验退回后，这一版去掉了一些内容”，列出每一条，请人对照需求确认（第二轮验收 I-15 后新增；以前模型被退回后悄悄删掉需求里要的检查，人看不出来） |
| `returned` | 审核试算结果的 `record` | 它的 `decision` 是 `fix_plan`，而且它的 `at` 比 `previous.at` 晚（或者 `previous` 为 null）时，顶部写“审核要求改方案，请再确认一次”，列出它的意见 |
| `previous` | 这个节点上一次的 `record` | 只用来和 `returned` 比先后；第一次为 null |

问题在 `pending.fields` 里，每题都要点选。“这里不对”可以挂在：指标 `metric:<id>`、维度 `dimension:<id>`、筛选 `filter:<id>`、规则 `rule:<id>`、问题 `question:<id>`、列 `column:<目标表>.<列名>`。

`etl_dev_review`（审核试算结果）分 4 个标签：

| 标签 | 读 `data` 的 | 页面上 |
| --- | --- | --- |
| 口径与方案 | `brief`、`plan`、`uncited` | 同确认方案，只读 |
| 代码 | `files` | 文件列表；内容在打开这个标签时从 `GET /runs/{id}/flow` 取：`steps` 里最后一个有 `bundle` 的检查并打包步骤（`steps[].output.vars[节点 id].bundle`，哪一步是检查并打包看 `nodes[].type` 是不是 `etl_code_check`），取它的 `bundle.files`，和 `files.zip` 用同一条规则；可以复制，也可以用 `GET /runs/{id}/files.zip` 下载 |
| 试算结果 | `status`、`tables`、`quality`、`answer`、`preview`、`errors`、`url` | 每张表的行数、丢弃的行数、每条质量规则是否通过；答案卡预览（同结果页）；按主键排好序的前 20 行；报错原文和 Databricks 链接。`status` 为 `failed` 时顶部写“试算没通过” |
| 确认记录 | `confirmed` | 谁、什么时候确认的方案；每题选了什么，是“选了建议”还是“改选”；写过的意见和挂的意见；确认时所见内容的指纹 `seen` |

意见可以挂在：列 `column:<目标表>.<列名>`、质量规则 `quality:<目标表>.<规则名>`、文件 `file:<路径>`。“通过”在试算没通过时不出现，提交了也不接受；“改代码”“改方案”“终止”都要写意见或者至少挂一条意见。

#### 结果页

运行 `done` 以后，版式按 `result` 的形状定，不看键名（用户自己建的编排应用，结束节点的输出也可能叫 `code`、`brief` 或 `answer`）：
- **第 2 版的结果**：`result.brief` 是对象、有文字 `goal`；或者 `result.code` 是对象、它的 `files` 是“路径 → 文字”的对象（检查并打包节点的 bundle，和 `files.zip` 认的一样）。
- **第 1 版的结果**：不是第 2 版，`result.plan` 能按 `EtlPlan` 读出来（同现在的 `readEtlPlan`），`result.report` 是文字。

按下面的顺序，前一条成立就不再往下看：
1. 第 2 版的结果、`outcome` 为 `stopped`：顶部写“已终止”和 `reason`，下面是方案、运行报告和开发者视图三个标签，没有答案和代码。
2. 第 2 版的结果，其他情况：分 5 个标签：答案（默认打开）、方案、代码、运行报告、开发者视图。版式不看 `answer`。
3. 第 1 版的结果（模板第 1 版的以前的运行）：只显示方案（主栏按 Markdown 显示）、运行报告和开发者视图。
4. 其他结果（如 `hello` 的 `answer` 是文字，或者用户自己建的应用）照以前的样子显示，不按答案卡。

5 个标签的内容：
- **答案**：`answer` 不是对象时（方案没有答案设计，`answer` 为 null）只写“这次方案没有设计答案”，其他四个标签照常。是对象时：
  - 卡片上方写“开发环境试算 · 已由 {reviewed.actor} 审核 · 尚未上线”（没有名字时写“已审核”）。
  - `status` 为 `error` 时写“答案没算出来：{error}”，下面只有质量说明和技术细节。
  - 结论一句话；表格显示 `rows`，表头取 `columns[].label`，数字按 `columns[].format` 显示（见“答案怎么算”），`truncated` 时注明“共 27 组，显示前 10”。
  - “{period} 合计”一行显示 `totals`，数字同样按 `format`，`none` 的指标显示“—”，表格下面写一次“合计里的「—」：{这些指标的名称用、连接}不能把各组直接相加。”
  - `chart.type` 为 `bar` 画柱状图、`line` 画折线图（内联 SVG，不加新依赖），`none` 不画。每个点或柱子用 `chart.label` 的列取值命名（用“ · ”连接）；这样会有两组同名时（比如只写了城市，同一个城市有几个会员等级），改用全部分组列命名。
  - 质量说明 `quality.text`；可以把 `rows` 下载成 CSV（原值）。
  - “技术细节”默认收起：表的位置和复制按钮、Databricks 链接、复制查询 SQL（注明只展示、不会执行）。
- **方案**：业务说明（`brief`）、确认记录（`confirmed`）和审核记录（`reviewed`），技术方案（`plan`）默认收起。
- **代码**：`code.files` 的文件列表和 zip 下载。
- **运行报告**：`report`。
- **开发者视图**：原始 JSON、流程图、Langfuse 链接、run_id、全部检索结果。

### 演示应用和 `make demo`

`make demo` 一次准备好演示环境，可以重复执行，结果不变：

1. **检查**：API 能访问；模型网关里有 `AEP_DEMO_MODEL`（默认 `company`）这个别名；知识库的向量模型 `AEP_DEMO_EMBEDDING_MODEL`（默认 `company-embed`）在 `AEP_EMBEDDING_MODELS` 里且模型网关上有（`mock-embed` 不经网关）；登记了数据源“MySQL 示例库”且能读到 `retail` 库；平台配了 Databricks 的 host、token、`AEP_DATABRICKS_CLUSTER_ID`（并查一次集群状态，令牌失效在这里就能发现）、`AEP_DATABRICKS_SOURCE_CATALOG`、`AEP_DATABRICKS_CATALOG` 和 `AEP_DATABRICKS_SCHEMA`。逐项打印 ✓、✗（必需项）或 !（只警告）和缺什么；缺 Databricks 时只警告（应用照样建好，跑到抽取会失败）。新接口 `GET /health/demo` 返回这些检查项 `[{"key", "ok", "detail", "required", "value"}]`：`key` 依次为 `api`、`model`、`embedding`、`datasource`、`databricks`，配了工作区时再有 `databricks_cluster`、`databricks_source_catalog`、`databricks_target`；`required` 为 false 的是 Databricks 各项；`value` 只有 `model`、`embedding` 有，是模型别名。不返回密钥。
2. **示例库**：MySQL 里没有 `retail` 库时，执行 `docker compose exec -T source-mysql sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot' < connectors/mysql/seed/003-retail.sql`（新卷会在初始化时自动执行这个文件，已有的卷靠这一步补）。
3. **知识库**：按名称找，没有就用检查过的向量模型建（说明不一样时改成上面的说明）；已有的知识库向量模型不一样时，里面只有演示文档就全部删掉、改向量模型、重新上传，有别的文档就停下并说明（换模型要先清空文档，别人加的文档不删）；`demo/knowledge/<目录>/` 里除 `README.md` 外的 `.md` 文件按文件名对比，缺的上传、大小或内容（下载原文件比较）变了的删掉重传、处理出错的重新处理、多余的不动；等全部文档处理完成（最多 5 分钟），有出错的就停下并打印原因。
4. **应用**：应用 id `retail-etl`，标题“零售数据 ETL”。不存在时用模板 `etl-lakehouse` 新建；从 `GET /app-templates` 取模板配置，把两个知识库填进 `glossary`、`standards`，`plan`、`codegen` 的模型设为 `AEP_DEMO_MODEL`、Skill 设为各自最新发布的版本，开始节点的默认值：数据源为“MySQL 示例库”，`tables` 为 `retail.*`，`request` 为设计文档里的演示需求；模板第 2 版起，`request` 的 `examples` 设为三句：演示需求、“统计每个品类每月的销售额和毛利率，只看线上渠道。”、“统计每月新客户数和复购率，按城市拆开。”；保存草稿，保存后的草稿和生效版本的配置一样时丢弃草稿、不发新版本，否则发布。原来有草稿时会被覆盖，打印里写明。
5. 最后打印应用页、两个知识库页的地址和演示需求。

实现为后端包里的脚本 `aep.demo`（`uv run aep-demo --api http://localhost:8000 [--web http://localhost:3000]`，只调平台接口，不直接连数据库），Makefile 的 `demo` 目标先跑第 2 步再调它（`make demo API=… WEB=…`）。

模板里 `plan`、`codegen` 的 Skill 写的是内置版本 1（第 2 版的 `plan` 是 `etl-plan-brief` 版本 1）；两个知识检索节点的 `min_score` 为 0.2（混合检索的分数比纯向量低，口径文档要尽量都检索到）。

## 第三批：绑定项目读写 Databricks

改造方案第一期（设计文档 https://claude.ai/artifact/CCPMycsWqgaPzY3iYCMywK ）的数据部分：读 Databricks 上已有的表、按项目写到对的位置、按方式写表、运行参数、数据检查。项目、运行参数和数据外发策略本身见 [projects.md](projects.md)，Databricks 只读连接器见 [datasources.md](datasources.md)。每一项都写明落在哪一层：

| 改动 | 层 |
| --- | --- |
| Databricks 只读连接器 | 连接器 |
| 项目、运行参数、数据外发策略 | 平台资源、平台策略 |
| 探查、抽取认识 Databricks 的表和读取筛选 | 节点（`etl_profile`、`databricks_ingest`） |
| 代码读源表、读参数，写到项目环境、只写本项目的表、按方式写表 | 节点（`etl_code_check`、`databricks_dev_run`）和平台的集群程序 |
| 数据检查 | 新节点 `data_check` |
| 方案第 3 版 | 节点 `etl_plan_check` 的格式 |
| 已有的表加列要在方案里确认（验收后新增） | 节点（`etl_profile`、`etl_plan_check`）和平台的集群程序 |
| 模板 `etl-lakehouse` 第 3 版（模板 id `etl-lakehouse-project`）、两个新的内置 Skill | 应用 |

节点从运行的项目快照里取项目信息（环境、前缀、引用表、参数），不用在设置里写；没绑定项目的运行和以前完全一样。

### 表名的写法

- 数据库和上传文件的数据源照旧是 `schema.table`。Databricks 数据源是三段式 `catalog.schema.table`，`schema.*` 写成 `catalog.schema.*`。拆表名时按最后一个 `.` 分出表名，前面是连接器的 `schema`。写法不对时 `input_error`“表 … 要写成 catalog.schema.table 或 catalog.schema.*”（数据库数据源是“… schema.table 或 schema.*”）；保存时只查形状（至少一个、最多两个 `.`），因为整段引用的数据源要到运行时才知道类型。名字里带 `-` 的 catalog（如 `icsd-ace-dev`）照写，平台拼语句时加反引号。
- 三段式的名字在方案的 `sources`、代码检查、`source()` 和数据检查里原样使用。

### etl_profile：探查 Databricks 的表

- `datasource` 可以是 `databricks` 数据源。
- `tables` 为空时：Databricks 数据源上，运行绑定了项目就探查项目的引用表（`references`，表名按项目里写的原样，超过 20 张时 `input_error`；项目没有登记引用表时 `input_error`“项目「…」没有登记引用表，Databricks 数据源请写要探查的表”），没绑定时 `input_error`“Databricks 数据源请写要探查的表”；其他数据源照旧探查全部表。
- 读取筛选：探查的表是项目的引用表（表名不分大小写，`tables` 里写明的表也算）、并且带 `filter` 时，`table_stats` 和 `sample_rows` 带上 `where`，样本和统计都只算筛选后的行。另外用连接器的 `aggregate`（`count(*)`，不带 `where`）数一遍全表（试点后新增），让模型知道筛选去掉了多少行。只在 Databricks 数据源上用，数据库数据源上的表不受影响。
- 调 Databricks 数据源的工具时超时用 `AEP_DATABRICKS_TOOL_SECONDS`（[datasources.md](datasources.md)），`tool`、`agent` 节点也一样。
- 已有的表（验收后新增）：运行绑定了项目、数据源是 Databricks 时，另外列出项目在这次运行的环境里已经有的表，见下面“表结构变化”。
- 产出的 `tables[]` 每张表多三个键：`filter`（用了的读取筛选，没有为 null）、`unfiltered_row_count`（有筛选时全表的行数，没有筛选为 null；`estimated_row_count` 是筛选后的行数）和 `comment`（表注释，没有为 null）；列多 `comment`（没有为 null）。别的数据源同样有这几个键，值为 null（连接器给了注释时照用）；代码应用 `etl` 的探查结果也一样。`description` 里写上表和列的注释、筛选（“只读 Type = 'Vol.' 的行（项目登记的读取筛选，统计和样本也只算这些行；全表 3 行，筛掉了 1 行）”，策略是“只发结构”时不写行数）和引用表的说明，并按数据外发策略生成（[projects.md](projects.md)“四个级别”）。

### databricks_ingest：Databricks 的表不复制

`datasource` 是 `databricks` 数据源时，节点不读行、不写 Databricks：表本来就在 Databricks 上，生成的代码直接读。产出照旧，`catalog` 为 null，`tables[]` 是 `{"source": "prev.sales.forecast", "target": "prev.sales.forecast", "rows": null, "columns": 列数, "copied": false}`（`target` 是 SQL 的写法，同抽取的 `target`，需要时加反引号，如 `` `icsd-ace-dev`.sales.orders ``；`columns` 由 `describe_table` 数出；别的数据源 `copied` 为 true），`summary` 写“这 3 张表本来就在 Databricks 上，直接读，不复制”，对话里另列出各表。`max_rows` 不检查，`reuse` 不起作用；不读行，也不需要平台配置 Databricks 工作区（`AEP_DATABRICKS_*`）。这样同一个模板既能接数据库，也能接 Databricks。

### 生成的代码怎么读源表和参数

PySpark 管道文件开头由平台写好这几样（模型写的转换函数不能重新定义它们，检查同现有的禁用名单）：

```python
SOURCE_CATALOG = spark.conf.get("source_catalog")
SOURCE_FILTERS = {"prev.sales.forecast": "Type = 'Vol.'"}   # 项目登记的读取筛选，平台写入；没有时为 {}
PARAMS = json.loads(spark.conf.get("aep_params", "{}"))
WARNINGS = []   # 代码用 warn() 记下的告警


def source(name: str):
    """A source table: schema.table through the source catalog, or catalog.schema.table as is."""
    ...   # 两段时前面加 SOURCE_CATALOG，三段时原样；名字在 SOURCE_FILTERS 里时 .where(筛选)


def param(name: str):
    """A run parameter of the project: str, int, float, bool or None."""
    return PARAMS.get(name)


def warn(message: str) -> None:
    """A warning about the data for the run report; the run goes on."""
    WARNINGS.append(str(message))
    print(f"WARNING: {message}")
```

- 筛选写进代码里，代码评审时看得见；改了项目的筛选，要重新生成代码才生效。
- 生成的代码在集群上读源表是运行管道本身（写的一侧），用平台部署和运行 Job 的身份，和以前一样；试算后读结果表的预览、算答案也照旧在集群上做，读的是这次刚写的表。探查和数据检查读表一律经只读连接器。
- `param()` 的值按参数类型：`number` 是 int 或 float，`boolean` 是 bool，其余（含 `date`、`month`）是文字，没给值为 None。
- `etl_code_check` 新加的检查（问题都让模型重写）：`param()` 的参数必须是字符串字面值，并且是项目定义了的参数（没绑定项目时“没有绑定项目，不能用 param()”）；`source()` 的三段式名字同样要在方案的来源里。
- 函数体能 import 的（试点后新增，原来一律“不要写 import”）：标准库 `re`、`math`、`datetime`、`decimal`、`calendar`（`import re`、`import datetime as dt`、`from decimal import Decimal` 都行），以及 `pyspark.sql` 的 `types`、`functions`、`window`（`from pyspark.sql import types as T`、`from pyspark.sql.functions import col`；`import pyspark.sql.types` 要带 `as`）。别的模块、相对导入、`import *` 检查不过：“不能 import os，能 import 的只有 re、math、datetime、decimal、calendar 和 pyspark.sql 的 types、functions、window”。import 出来的名字不能是平台提供的名字，只有 `from pyspark.sql import functions as F`、`from pyspark.sql import Window` 这种和平台同一个东西的除外。
- 没有定义的名字：函数体读了一个名字，而它既不是函数体里赋值、import、定义或参数里出现过的，也不是 Python 内置名字或平台提供的名字时，检查不过：“ibf_table2: 用了没有定义的名字 re（能 import 的只有 …；告警用 warn()）”。只查有没有，不查先后。以前这种代码要到 dev 运行时才报 NameError，白跑一次集群。
- `warn("说明")`：数据有问题但不该停下时用（比如某个月没有数据、分摊比例加起来不是 1），每条进 dev 运行的 `warnings`，写成“代码告警：说明”，最多 20 条，每条最多 500 字；运行照常通过（`cluster`、`job` 模式；`pipeline` 模式只打印在管道日志里）。该停下的问题照旧 `raise ValueError("说明")`，dev 运行失败并带回这句话。`WARNINGS` 由平台读取，代码不能直接用（“不要直接用 WARNINGS，告警用 warn()”）。
- `warn_rows(df, "说明", limit=10)`（验收后新增）：要在告警里列出有问题的行（比如没分摊上的组合）时用。代码不许 `collect`，列行的事由平台的这个函数做：`df` 为空时什么都不记；否则数一遍行数，取前 `limit` 行（1–100），记一条 `warn("说明：共 n 行：列=值, 列=值；……（只列出前 k 行）")`，同样受上面每条 500 字的限制。代码不能重新定义它。代码用了禁止的写法时，问题里写上能用什么代替：`.collect`、`.toPandas` 写“要在告警里列出几行用 warn_rows(df, "说明")，数据留在集群上算”，`.sql` 写“用 DataFrame 写，读源表用 source()”，写表的 `.write`、`.saveAsTable`、`.insertInto` 写“不要写表：return 结果 DataFrame，平台按方案的写表方式写”。
- SQL 方案：源表照旧换成 `${source_catalog}` 加两段名，三段名原样；条件里的 `${param.<key>}` 换成值的 SQL 字面值（同下面写表方式的规则）。读带筛选的引用表时检查不过：“表 … 有读取筛选，SQL 代码暂不支持，请改用 PySpark”。

### 写到哪里，只写本项目的表

运行绑定了项目时（没绑定时一切照旧）：

- **运行方式**：`job` 和 `cluster`（`AEP_DATABRICKS_DEPLOY`）。`pipeline` 方式由 Lakeflow 自己管表，查不了归属，也用不了下面的写表方式，绑定项目时 `databricks_dev_run` 以 `input_error` 失败“绑定项目的运行请用 job 或 cluster 方式（AEP_DATABRICKS_DEPLOY）”。
- **位置**：`job` 方式把结果表写到这次运行的环境：`<catalog>.<schema>.<目标表>`；`cluster` 方式照旧不落表，只是读源表和算答案也用环境的集群。集群用环境的 `cluster_id`，没写时用 `AEP_DATABRICKS_CLUSTER_ID`。`databricks_dev_run` 的 `run.catalog`、`run.schema` 就是环境的 catalog 和 schema。bundle 的 dev 目标里 `catalog`、`schema`、`cluster_id` 的默认值取这个环境；prod 目标不变，按项目环境生成多个目标放在第三期。
- **前缀**：方案的目标表名要以项目的 `table_prefix` 开头，不是时 `etl_plan_check` 让模型重写（第 3 版，见下）；`databricks_dev_run` 运行前再查一遍，不合格时 `input_error`。
- **归属**：集群程序写每张表之前查它是否存在：
  - 不存在：建表。
  - 存在并且表属性 `aep.project` 等于项目 id，或者表名登记在项目的 `adopted` 里：照常写。
  - 其他：不写，记一条写入错误“表 … 已经存在，不属于项目「…」（没有项目标记，也没有登记在“已有的表”里），没有写”（标记是别的项目时括号里写“项目标记是另一个项目 …”），这次运行按失败处理（同现有的 `write_errors`，不让模型重写代码）。集群程序写表前也再查一遍前缀，不合格时写入错误“表 … 不以项目的表名前缀 … 开头，没有写”。
  - 写完后设表属性 `aep.project`（项目 id）和 `aep.run_id`（部署成 Job 后由 Job 自己跑时是 Job 运行的 id）。
- **传给集群程序的设置**：`run_notebook` 的 widget 新增 `project`（项目 id，没绑定为空）、`project_name`（报错里用）、`table_prefix`、`adopted`（JSON 列表）、`params`（参数值的 JSON 对象）和 `run_id`，bundle 变量同名（`run_id` 除外，它的默认值是 `{{job.run_id}}`，平台运行时传自己的运行 id），默认值取这次运行的快照；`cluster` 模式由平台直接放进 spec。没绑定项目时这些为空，不查前缀和归属。每张表的写表方式和参数的类型在生成代码时就定了，平台写进 Job 的 notebook（`run.py` 调 `run_notebook` 时带上）；`etl_code_check` 的 `bundle` 另有 `settings`（`loads` 每张表的写表方式、`param_types`、`source_catalog` 代码是否经源表 catalog 读表），`cluster` 模式用它，只在绑定项目或用了第 3 版写表方式时有。源表全是 Databricks 上的三段式表时，dev 运行不需要 `AEP_DATABRICKS_SOURCE_CATALOG`。

### 写表方式

方案第 3 版里目标表的 `load` 改成：

```jsonc
{"mode": "overwrite"}                                                    // 整表覆盖（默认，同以前的 full）
{"mode": "replace_where", "where": "update_month = ${param.update_month}"}  // 只替换满足条件的行（一个月、一个版本）
{"mode": "append"}                                                       // 追加
{"mode": "merge", "keys": ["order_id"]}                                  // 按键合并：有的更新，没有的插入；keys 不写时用 primary_key
```

- `where` 是 Spark SQL 布尔表达式，形状检查同引用表的 `filter`；里面的 `${param.<key>}` 在运行时换成参数值的 SQL 字面值：文字和 `month` 加单引号并转义，`date` 写成 `DATE'2026-10-01'`，数字原样，布尔写 `true`、`false`，空值写 `NULL`。参数不存在时 `etl_plan_check` 让模型重写。
- 已经加了单引号的参数（`version_id = '${param.version_id}'`，引号正好包住整个参数）也认：换成值的文字字面值，不再套一层引号（以前会变成 `''FY26_06_M''`，Spark 报语法错）。所以 `'${param.x}'` 和 `${param.x}` 对文字参数是一样的；对数字和日期，加了引号的是文字（`'3'`、`'2026-10-01'`），不加的保持类型；空值照样是 `NULL`。只有包住整个参数的引号才算，`'FY_${param.x}'` 这种拼在更长字符串里的不支持。这条规则对用到 `${param.*}` 的所有地方都一样（替换条件、数据检查、SQL 代码）。
- 集群程序怎么写（Delta）：
  - `overwrite`：同现在（`overwrite`，`overwriteSchema`）。
  - `replace_where`：`mode("overwrite")` 加 `replaceWhere`；结果里有不满足条件的行时 Delta 会拒绝，记写入错误“有行不满足替换条件 …”。
  - `append`：`mode("append")`。
  - `merge`：先查结果在 `keys` 上不重复，重复时记写入错误“合并键 order_id 有 3 组重复”；再 `MERGE INTO … WHEN MATCHED THEN UPDATE SET * WHEN NOT MATCHED THEN INSERT *`。源里没有的行不删。
  - 后三种在表不存在时直接建表。表已存在时：绑定项目的运行只按确认的方案加列（见下面“表结构变化”，验收后改的；以前结果多了新列就自动加在表的最后，人看不到也没确认过）；没绑定项目的运行照旧自动加列（`mergeSchema`，`merge` 先 `ALTER TABLE … ADD COLUMNS`）。列的类型变了时写入失败，报错原样带回。
- 写完（打项目标记之前）读一次 `DESCRIBE HISTORY … LIMIT 1`，`databricks_dev_run` 的 `tables[]` 新增 `write`：`{"mode": "replace_where", "version": 12, "operation": "WRITE", "rows_written": 1200, "rows_updated": null, "rows_deleted": 340}`（取 Delta 的操作指标，没有的为 null；`merge` 的 `rows_written` 是插入的行数；按条件替换会重写旧行所在的文件，同一文件里不在条件内的行原样写回，Delta 也算进输出，平台从 `rows_written` 里减掉，另记 `rows_copied`，只在不为 0 时有，写表说明写“另把同一文件里 n 行其他数据原样写回（Delta 重写文件）”，验收后新增）；这次给表加了列时另有 `columns_added`（见“表结构变化”）。没写的表（`cluster` 模式不落表、写入失败）没有这一项。运行报告的结果表多一列“写表”，如“按条件替换，版本 4，写入 4 行，加列 platform_original（string，放在 platform 后面）”。
- 方案第 1、2 版的 `full` 当作 `overwrite`，`incremental` 照旧只写进方案说明、按整表覆盖执行，结果不变。

### 表结构变化（验收后新增）

jadeflow 验收时发现：结果表已经存在、方案多了一列时，集群程序用 `mergeSchema` 把新列静默加在表的最后，人在确认方案和审核结果时都看不到。现在已有的表的结构只按确认过的方案变：方案写明加哪些列、加在哪一列后面，确认方案就是同意加列；集群程序只加这些列，加在方案写的位置。三处配合：

- **探查（`etl_profile`）**：运行绑定了项目（快照里有环境的 catalog 和 schema）、数据源是 Databricks 时，经只读连接器 `list_tables` 列出环境 `<catalog>.<schema>` 里以项目前缀开头的表（按表名排序，最多 20 张），每张 `describe_table` 读列；只读结构，不读行、不做统计。新产出：
  - `existing`：`[{"name": "t2p_table2_lane", "table": "icsd-ace-dev.aep_t2_pilot.t2p_table2_lane", "columns": [{"name": "version_id", "data_type": "string", "nullable": false, "comment": null}, …]}]`；环境里还没有本项目的表时为 `[]`；没列（没绑定项目、数据源不是 Databricks）或列表失败时为 `null`，失败原因写在 `summary` 里（“没能列出项目在环境里已有的表：…”），节点照样通过。
  - `existing_description`：给模型看的已有的表（列名、类型、非空、列注释，不受数据外发策略限制）和写到这些表的规则：同名就是写到这张表；按条件替换、追加、按键合并要写出表里已有的全部列，要加的列写在 `columns` 里想放的位置；整表覆盖删掉现有的全部行、按方案重建表结构。没有表时写“环境「开发」的 `…`里还没有本项目的表，目标表都会新建。”，没列时写“（平台没有列出…里已有的表；写表时会再核对表结构，不会加方案外的列）”；没绑定项目时为空。
  - `summary` 末尾加“；项目在环境里已有的表：t2p_table2_lane”（没有时“没有”）。
- **校验方案（`etl_plan_check`，只在 `format: 3`）**：新设置 `existing`（可选，整段引用，如 `{{profile.existing}}`；`format` 不是 3 时保存报错“只有方案版本 format 为 3 时才能写已有的表”，不是整段引用时“已有的表必须恰好是一个变量，如 {{profile.existing}}”）。运行时引用的值是 null 或没设 `existing` 时不做下面这些（平台不知道哪些表已经存在）；值不是表的列表时节点以 `output_format` 失败“已有的表要引用探查数据节点的 existing”。目标表名和某张已有的表同名（不分大小写）时：
  - 写表方式是 `replace_where`、`append`、`merge`：表里的列（不分大小写）都要在方案的 `columns` 里，少了让模型重写：“t2p_table2_lane 已经存在，表里的列 loaded_at_utc 不在方案的 columns 里：按条件替换会保留表里其他的行，要写出表里已有的全部列，新加的列写在想放的位置；要去掉列或重建表，请用 overwrite”。方案里有、表里没有的列由平台按方案的列顺序写进 `load.add_columns`：`[{"name": "platform_original", "after": "platform"}]`（`after` 是方案里它前面的那一列，排在第一时为 null；连着加几列时后一列跟在前一列后面）。
  - 列的类型（第二轮验收 I-14 后新增）：第 3 版方案的列多一个可选的 `type`（Spark SQL 类型，如 `STRING`、`BIGINT`、`DECIMAL(18,2)`、`DATE`、`ARRAY<STRING>`）。平台按 Spark 的写法统一（`dataType.simpleString()`：小写、去空格，`INTEGER` 写成 `int`、`LONG` 写成 `bigint`、`VARCHAR(10)` 写成 `string`、`DECIMAL` 写成 `decimal(10,0)`），不是类型时让模型重写：“t2p_table2_lane 的列 x 的 type number 不是 Spark SQL 类型（如 STRING、INT、BIGINT、DOUBLE、DECIMAL(18,2)、DATE、TIMESTAMP、ARRAY<STRING>）”。没写 `type` 的列 dump 里没有这个键，所以以前的方案读出来不变。给已有的表新加的列必须写类型，没写时让模型重写：“t2p_table2_lane 已经存在，要新加列 platform_original：请在 columns 里写它的 type（…），确认方案时人要看到加的是什么类型的列”。表里已有的列可以不写；写了就要和表里的一样（统一写法后比），不一样时“…的列 x 在表里是 STRING，方案写的是 BIGINT：已有的列不改类型，type 照表里的写或者不写”。`add_columns` 每项带上 `type`：`[{"name": "platform_original", "after": "platform", "type": "string"}]`。
  - `overwrite`：不加列，表按方案重建。
  - `load.add_columns` 只由平台写：模型写了的先去掉，不提示。第 1、2 版方案和不同名的表为 `[]`。`Load.stored` 在 `add_columns` 不为空时带上它，随 `settings.loads` 进 bundle，Job 的 notebook 传给集群程序。
  - `message` 里每张目标表在写表方式下面多一行“表结构”：“表还不存在，按方案新建”“表已经存在（22 列），结构不变”“表已经存在（22 列），确认方案后加 1 列：`platform_original` STRING，可为空（放在 `platform` 后面）”（Delta 给已有的表加的列都可以为空）“表已经存在（22 列），整表覆盖会删掉现有的全部行，按方案重建表结构”；不知道已有的表时没有这一行。确认方案页的技术方案在主键下面列出“加列”（带类型和“可为空”），列映射里写了类型的列在列名后面显示类型，新加的列标“新加”；接口看 `pending.details.data.plan.targets[].load.add_columns`。
- **集群程序写表**：绑定项目、表已经存在、写表方式是 `replace_where`、`append`、`merge` 时，写之前先比对结果和表的列（不分大小写）：
  - 结果少了表里的列：不写，写入错误“t2p_table2_lane: 结果里没有表里已有的列 loaded_at_utc，没有写”。
  - 结果里的列和表里同名的列不是一类值（数字、文本、日期、时间、布尔、二进制，数组等复杂类型按完整类型比；数字的宽度不同、时间带不带时区不算）：不写，“…: 列的类型和表里的不一致（qty 在表里是 STRING，结果里是 INT），没有写”。数字宽度不同这类交给 Delta，失败时报错原样带回。
  - 结果多了 `add_columns` 里没有的列：不写，“…: 结果比表多了列 x，确认的方案里没有要加这些列，没有写”。
  - `add_columns` 里带 `type` 的列，结果里的类型（统一写法后）要和确认的一样，不一样时不加列也不写：“…: 确认的方案里新加的列 qty 是 BIGINT，结果里是 INT，没有加列，也没有写：代码要让这一列是 BIGINT（需要时用 cast）”。以前确认、没有 `type` 的方案照旧用结果里的类型。写代码的提示也说了：方案里写了 type 的列，结果里就要是这个类型。
  - `add_columns` 里的列（结果里有、表里还没有的）按方案的顺序逐列 `ALTER TABLE … ADD COLUMNS (<列> <结果里的类型> AFTER <列>)`（`after` 为 null 时 `FIRST`）；`after` 那一列表里没有时不写，“…: 方案要把列 x 加在 y 后面，表里没有列 y，没有写”。表里已经有的（以前的运行加过）跳过，所以同一个方案重跑不会再加。替换条件、合并键这些写前检查先做，都通过了才加列。
  - 加完列再写，不再用 `mergeSchema`。加了列之后写入失败时，写入错误后面加“（列 x 已经加到表里，数据没有写）”。
  - 结果的 `write` 多 `columns_added`：`[{"name": "platform_original", "type": "string", "after": "platform"}]`，没加列时没有这一项。
  - `overwrite` 照旧 `overwriteSchema`；没绑定项目的运行照旧 `mergeSchema`。prod 等别的环境写表时同样按 `add_columns` 比对那个环境里的表。

### data_check 数据检查（新节点）

运行后核对数据：总数对不对、和源表对不对得上、键是否重复、和老系统的结果是否一致。和探查一样经 Databricks 只读连接器读表（它的只读身份和 catalog 白名单照样管用），不用平台写表的身份。

```jsonc
{"datasource": "{{start.datasource}}",   // 必填：Databricks 数据源（datasource_id 或整段引用），规则同 etl_profile
 "checks": [Check],                      // 写在节点里的检查，0–50 条
 "include": ["{{check_plan.checks}}"],   // 0–5 个整段引用，值是 Check 的列表（比如方案里的检查），和 checks 合在一起
 "golden": {"expected": "{{start.golden}}",       // 可选：和老系统对比，见下
            "table": "{{check_plan.result_table}}",
            "keys": "{{check_plan.result_keys}}",
            "tolerance": 0.001,                    // 可选，数字列的容差，同 golden 检查
            "severity": "warn"},                   // 可选，默认 error
 "only_if": "{{dev_run.ok}}"}            // 可选，恰好一个引用：值不是 true 时不执行，见下
```

```jsonc
// Check 公共字段
{"id": "total_qty",                      // 规则同节点 id，合在一起后不能重复；golden 留给节点自动加的对比，不能用（都是 input_error）
 "label": "总数量和预测表一致",            // 1–100 字符
 "kind": "aggregate" | "reconcile" | "unique" | "golden" | "rows",
 "severity": "error" | "warn" | "info",  // 默认 error
 "known": [{"match": {"sku": "B2"}, "note": "B2 按新口径，和老系统不同"}]}  // 可选：已知差异，见下（golden 还要写 columns）

// aggregate：一张表上的一个聚合值和期望比
{"table": "ibf_table2", "expr": "sum(qty)", "where": "update_month = ${param.update_month}",
 "op": "eq" | "ne" | "gt" | "ge" | "lt" | "le" | "between", "value": 0, "max": null, "tolerance": 0,
 "group_by": ["month"]}                  // 可选：按组算，每组都要满足

// reconcile：两张表的聚合值对账，可以按组
{"left": {"table": "ibf_table2", "expr": "sum(qty)", "where": "…"},
 "right": {"table": "prev.sales.forecast", "expr": "sum(Volume)", "where": "…"},
 "group_by": ["month"],                  // 可选，两边要有同名列；按组比时列出对不上的组
 "tolerance": 0.005, "relative": true}   // relative 为 true 时按相对误差

// unique：键不重复
{"table": "ibf_table2", "columns": ["sku", "month"], "where": "…"}

// golden：和老系统的结果逐行对比
{"table": "ibf_table2", "expected": "old.jade.table2", "keys": ["sku", "month"],
 "columns": ["qty", "amount"],           // 可选，不写时比两边同名的非键列
 "where": "…",                           // 可选，只筛 table
 "expected_where": "Type = 'Vol.'",      // 可选，只筛 expected
 "tolerance": 0.0001}

// rows：满足条件的行必须没有
{"table": "ibf_table2", "where": "qty < 0",
 "lookup": {"table": "prev.sales.allocation",          // 可选：只看在另一张表里找不到（或找得到）对应行的行
            "on": {"lane": "lane", "efficiency_type": "eff_type"},   // 本表的列: 另一张表的列，1–10 对
            "where": "array_contains(split(months, ','), '07')",   // 可选，只筛另一张表
            "found": false}}                                         // false 只看找不到的行，true 只看找得到的行
```

- **字段**：每种检查只认上面列出的字段，多写的字段算不合格（“rows 检查没有 sql 这一项”）。`aggregate` 的 `op`、`value` 必填：`eq`、`ne` 的 `value` 可以是数字、文字或 true/false，`gt`、`ge`、`lt`、`le` 是数字或文字（文字按字符串比较，适合 ISO 日期），`between` 要数字 `value` 和 `max`（`value` 不大于 `max`），其他 `op` 不写 `max`。`tolerance`（不小于 0，默认 0）用在数字的 `eq`、`ne`、`between` 上：`eq` 是差的绝对值不超过它，`between` 两头各放宽它。聚合值是空值时一律不满足；要和数字比而聚合值不是数字（比如 `NaN`）时这条检查记为 `error`。分组列、`unique` 的 `columns`、`golden` 的 `keys` 最多 10、20、20 列，不能重复；分组列不能叫 `value`，`unique` 的列不能叫 `count`（和结果里的字段重名）。`checks` 和 `include` 合在一起最多 100 条。
- **表名**：不带点的名字是本次运行的结果表（绑定项目时 `<环境 catalog>.<环境 schema>.<名字>`，否则 `<AEP_DATABRICKS_CATALOG>.<AEP_DATABRICKS_SCHEMA>.<名字>`）；两段名是抽取到 `AEP_DATABRICKS_SOURCE_CATALOG` 下的源表；三段名原样。读项目引用表时带上它的读取筛选，和这张表那一边自己的条件用 AND 连起来，只用在这一边：`reconcile` 的 `left`、`right` 各用各的 `where`，`golden` 的 `table` 用 `where`、`expected` 用 `expected_where`（连接器的 `compare` 两边条件分开传）。表名都由平台换成三段式再交给连接器，所以连接器的 `AEP_DATABRICKS_READ_CATALOGS` 要包括项目环境的 catalog 和老系统结果表所在的 catalog，只读身份要能读它们；读不到时这条检查记为 `error`，`message` 是连接器的报错。`cluster` 方式不落表，读结果表的检查记为 `error`（“cluster 方式不落表，读不到结果表”）。
- **条件和表达式**：`expr` 是一个 Spark SQL 聚合表达式；`where`、`expected_where`、`expr` 的形状检查同 `filter`，连接器再查一遍。都可以用 `${param.<key>}`，平台按写表方式的规则换成字面值后再交给连接器。没有执行任意 SQL 的检查。
- **查另一张表 `lookup`**（第二轮验收后新增，为了 R4“分摊表里不适用的月份必须为 0”）：`rows` 检查可以只看在另一张表里找不到（`found: false`）或找得到（`found: true`）对应行的行，比如“这个月有重量、但分摊表里这条航线这个月不适用的行”。以前只能在条件里写子查询，而条件不收 `SELECT`。
  - `on` 是 `{本表的列: 另一张表的列}`，1–10 对，列名规则同分组列，本表的列不能重复；按空值安全的相等对上。`where` 只筛另一张表，规则同检查的 `where`（可以用 `${param.<key>}`）；另一张表是项目引用表时同样带上它的读取筛选，和 `lookup.where` 用 AND 连起来。`found` 必填，是 true 或 false。只有 `rows` 能写，别的检查写了算多写的字段（“unique 检查没有 lookup 这一项”）。
  - 表名规则同 `table`；方案里的检查只能查方案的目标表和源表，项目验收检查里不带点的表名要以表名前缀开头，这些规则对 `lookup.table` 同样适用，`lookup.where` 里的运行参数也要是项目定义的。
  - 连接器的 `aggregate`（数行）和 `sample_rows`（取例子）都带上 `lookup`，子查询由连接器拼（[datasources.md](datasources.md)）；已知差异照常加在本表的条件上。
  - `message` 和方案里的说明写成“有 2 行满足 `NOT (cw_07 <=> 0)`、并且在 `…allocation` 里找不到 lane、efficiency_type = eff_type 对应（满足 `…`）”“没有满足 …、并且在 … 里找不到 … 对应的行”；两边列名相同时只写一次。
  - 条件里写了子查询时，报错除了指向 `reconcile` 和 `rows`，也说可以用 `lookup`。
- **已知差异 `known`**（第二轮验收 I-4 后新增）：项目里有些差异是预期中的（假数据故意放的、口径变了的），以前只能把整条检查降成警告，之后新出的差异混在里面看不出来。现在检查可以列出已知差异，命中的单独报出来、不算不通过，清单以外的照常按 `severity` 不通过，所以这条检查可以设成 `error`。
  - 每项 `{"match": {列: 值}, "note": "说明"}`，最多 20 项；`note` 可选，200 字符以内，报出时用它，没有时写成“sku = B2、month = 2026-11”。值是文字、数字、true、false 或 null（null 只配 null）；比较时 2026 和 "2026" 算同一个值。
  - `match` 的列：按组的 `aggregate`、`reconcile` 是 `group_by` 里的列，`unique` 是 `columns` 里的列，`golden` 是 `keys` 里的列，`rows` 是表里任意的列。不分组的 `aggregate`、`reconcile` 没有可以区分的行，写了不合格（“只有按组比的聚合值和对账、键不重复、和老系统对比、不该有的行能写 known（已知差异）”）。
  - 怎么算：分组的 `aggregate`、`reconcile` 拿全了组，在平台这边把对不上的组分成已知和新的。`unique`、`golden`、`rows` 在连接器上分两次查：一次条件加上 `NOT (已知差异)`，判断通过没有、取例子；一次条件加上已知差异本身，数出命中多少、看是哪几项。已知差异写成 `` (`sku` <=> 'B2' AND `month` <=> '2026-11') OR (…) ``，和原来的条件用 AND 连起来，`golden` 两边都加（按键排除）。加上后超过 2000 个字符时这条检查记为 `error`（“加上已知差异后条件超过 2000 个字符，连接器不收；请少写几项已知差异”）。
  - **精确到格和值**（第三轮复测 I-16 后新增）：以前 `golden` 按键整行排除，`match` 只写了键，这一行别的列错了也被算成已知差异（复测时注入的 allocation 错误就这样漏掉了）。现在已知差异要说清楚差在哪：
    - `golden` 的一项可以写 `columns`：可能不一致的列名列表 `["platform_original"]`，或者连两边的值一起写 `{"platform_original": {"table": "PF_A1a;PF_A1b", "expected": "PF_A1b;PF_A1a"}}`（`table` 是结果表的值，`expected` 是老系统的值，可以只写一边）。最多 20 列，不能是键，检查写了 `columns` 时要在里面。命中的行两边都有、不一致的列都在 `columns` 里、写了的值都对上，才算已知差异；同一行还有别的列不一致，或者值不是写的那个，这一行照常算不一致。
    - `golden` 的一项没写 `columns` 时只管只在一边有的行（少的行、多的行）；两边都有、值不一样的行不算已知差异。
    - 分组 `aggregate` 的一项可以写 `value`（这组的值），`reconcile` 的可以写 `left`、`right`（两边的值，可以只写一边，`null` 表示这一边没有这组）。写了就要对上，数字按检查的容差比。
    - 比较值时数字和写成文字的数字算同一个（decimal 列从连接器回来常是文字，"280.0000" 和 280 相同）。
    - `golden` 的已知差异那次 `compare` 的 `limit` 用 200，平台逐行看；命中的行比这多时这条检查记为 `error`（“已知差异匹配到 250 行，超过一次能逐行核对的 200 行；请把 match 写得更具体”）。命中但不算已知差异的行加进少、多、不一致的数，排在例子最前面。
    - 结果的 `known` 多一项 `beyond`：命中了、但差的比清单写的多的已知差异，`message` 末尾写“；已知差异「…」的那行和清单写的不一样或还有别的不一致，照常算不通过”。`absent` 不再列它们。
    - 没有备注时，说明写成“sku = B2 的 qty”。
  - 结果多一项 `known`：`{"count": 1, "seen": ["B2 按新口径，和老系统不同"], "absent": ["Z9 已下市"], "beyond": []}`。`count` 是命中的行数或组数，`seen` 是看到的已知差异，`absent` 是这次没出现的（命中的行多于 20 行例子、看不全时为 `[]`）。没写 `known` 的检查结果里没有这一项。`message` 末尾加“；另有 1 行是已知差异（…）”“；已知差异「…」这次没有出现”；只有已知差异时 `status` 是 `passed`，说明写“和老系统的结果除已知差异外逐行一致（…）”“按 month 分 2 组，除已知差异外每组都满足 ≥ 5”“已知差异以外没有满足 qty < 0 的行”这样。`summary` 的通过数后面写“（其中 3 项只有已知差异）”。方案里的检查说明（`describe`）末尾加“；已知差异 1 处（…）不算不通过”。
- **经连接器的哪个工具**：`aggregate`、`reconcile` 用 `aggregate`（`reconcile` 两边各调一次，平台按组对齐、比较）；`unique` 用 `duplicates`；`golden` 用 `compare`；`rows` 用 `aggregate`（`count(*)`）数行、`sample_rows` 取例子。比较、容差和结论都在平台这边算。分组的 `aggregate` 和 `reconcile` 要拿全所有组才下结论：调 `aggregate` 时 `limit` 用 10000，任何一边返回的 `groups` 比 `rows` 多（没拿全）时这条检查记为 `error`（“分组有 12,345 组，超过一次能核对的 10,000 组；请减少分组列或加条件”），不按拿到的那部分判断。工具见 [datasources.md](datasources.md)“Databricks 数据源”。
- **节点的 `golden`**：各项都可以是整段引用。`expected`（老系统结果表的三段式名）取值后不是空的，节点就加一条检查 `{"id": "golden", "label": "和老系统的结果一致", "kind": "golden", "table": table, "expected": expected, "keys": keys}`，比两边同名的非键列；写了 `tolerance`、`severity` 时这条检查也带上（试点后新增：老系统的数常有末位舍入差异，差异也可能只想报出来不拦住），取值不合格时这条检查记为 `error`（“tolerance 要是不小于 0 的数”）；`expected` 为空（比如用户没填）时不加。`keys` 为空时这条检查记为 `error`（“结果表没有主键，不能逐行对比；请在方案里给结果表写 primary_key”）。id `golden` 只留给这条自动加的检查：`checks` 和 `include` 里有检查用这个 id 时节点 `input_error`（“检查 id golden 是留给和老系统对比的，请换一个”），所以填了老系统结果表就一定有这条对比，不会被别的检查顶掉。别的 id 的 golden 检查可以照常写在 `checks` 里；没写 `expected` 时用节点 `golden.expected`，也没有时记为 `skipped`（“没有指定老系统的结果表”），不算失败。
- **golden 先查键是否唯一**：`compare` 先数两边按 `keys` 重复的组数（见 datasources.md）。结果表有重复时这条检查 `failed`（“结果表按 sku、month 有 3 组重复的行，不能逐行对比”，`rows` 是重复的键）；老系统的表有重复时记为 `error`（“老系统的结果表按 sku、month 有重复，不能逐行对比；请换一组键”）。都不重复才比 missing、extra、different。
- **结果** `results[]`：`{"id", "label", "kind", "severity", "status": "passed" | "failed" | "error" | "skipped", "actual", "expected", "message", "rows"}`。`actual`、`expected` 是比较的两个值：
  - `aggregate`：聚合值和 `value`（`between` 时是 `[value, max]`），`message` 如“实际 1,234，要求 ≥ 0，满足”。
  - 按组的 `aggregate`、`reconcile`：`actual` 是不满足（对不上）的组数，`expected` 是 0，`message` 如“按 month 分 12 组，2 组对不上”；`rows` 是这些组：分组列加 `value`（`aggregate`），或加 `left`、`right`（`reconcile`，一边没有这一组时那边为 null）。
  - 不分组的 `reconcile`：两边的值，`message` 如“左边 1,385,355.07，右边 1,385,355.07，一致”“左边 13，右边 16，相差 3，超过容差 2”“左边 13，右边 16，相差 18.75%，在容差 20% 内”。
  - `unique`：重复的组数和 0；`rows`：重复的组数和行数（`rows` 检查）和 0。
  - `golden`：`actual` 是 `{"missing": 3, "extra": 0, "different": 12, "duplicates": {"table": 0, "expected": 0}}`（有重复键时前三项为 null），`expected` 是老系统结果表的三段式名。
  
  `message` 一句话；`rows` 最多 20 行例子（对不上的组、重复的键、不一致的行、查出的行），通过的检查和没有例子时为 `[]`。执行出错的检查 `status` 为 `error`，`message` 是报错第一行（连接器自己的报错，去掉 MCP 加在前面的工具名）。
- **`only_if`**：到这一步时取值，不是 true 就不调连接器：`ok` 为 false，`results` 为 `[]`，`summary` 为“前一步没有通过，没有做数据检查”，`report` 同 `summary`，走 `failed`。这样试算失败的路径也能经过这个节点，后面的节点可以放心引用它的产出（保存检查要求引用的节点一定跑过）。
- **节点什么时候失败**：`datasource` 不是 Databricks 数据源时 `input_error`（“数据检查要用 Databricks 数据源”）；连不上连接器时 `tool_error`（同 `tool` 节点，超时为 `timeout`）；单条检查出错只记在这条上。合在一起的检查不合格、id 重复或用了 `golden` 时 `input_error`，说是哪条（“检查 dup（columns）：列要写 1–20 个列名”）；`include` 的引用取值为 null 时当作空列表，是别的值时 `input_error`（“include 第 1 项（{{check_plan.checks}}）的值不是检查的列表”）。
- **保存时**：`checks` 每条按上面的规则检查，报错的 `loc` 指到字段（`[..., "checks", 2, "where"]`）；字段的值里有引用（`{{…}}`）时这个字段到运行时取值后再查。`include` 每项、`only_if` 必须恰好是一个引用；`golden` 只有 `expected`、`table`、`keys`、`tolerance`、`severity` 五项，`keys` 是列名列表或一个引用，`tolerance` 是不小于 0 的数或一个引用，`severity` 是 `error`、`warn`、`info` 或一个引用。写死的数据源不是 Databricks 类型时报“数据检查要用 Databricks 数据源”，是的话要有 `aggregate`、`duplicates`、`compare`、`sample_rows` 四个工具（连接器连得上时查）。

产出：

| 字段 | 内容 |
| --- | --- |
| `ok` | 没有 `severity` 为 `error` 的检查是 `failed` 或 `error` |
| `results` | 见上，按检查的顺序 |
| `summary` | 如“8 项检查：7 项通过（其中 1 项只有已知差异），1 项不通过（警告）”；不通过和出错的写出几项是错误、警告、提示（都是错误时不写）；没有检查时“没有数据检查” |
| `report` | Markdown：每条检查一行（结果、名称、一句话），不通过的附例子表格 |

分支：`passed`（`ok`）、`failed`（编辑页显示“通过”“不通过”）。不支持单节点试跑：`POST /apps/{app}/nodes/{node}/test` 返回 422“数据检查节点要读这次运行的结果表和项目，不能单独试跑”。`results`、`report` 算数据值，受数据外发策略约束。

编辑页：数据源只列 Databricks 数据源（和开始节点的数据源输入）；检查写成 JSON（不合法时提示，不覆盖已保存的内容），下面列出每条的名称和种类；“引用的检查”最多 5 个引用；“和老系统对比”五项（主键用逗号分隔，或一个引用；容差填数字；“不一致时”选算错误、只警告、只提示），清空时不写 `golden`；“只在这个值为 true 时检查”清空时不写 `only_if`。

### 方案第 3 版（`etl_plan_check` 的 `format: 3`）

在第 2 版上加这些，第 2 版的规则、整理和产出全部照旧（`aep.etl.plan.validate_plan_v3`，解析出的方案是 `EtlPlanV3`，比第 2 版多 `checks`，代码检查和试算照样当第 1 版方案读）。不合格的地方都让模型重写，问题的写法如下：

- **写表方式**：目标表的 `load` 按上面的写表方式；没写 `load` 或 `load.mode` 时是 `overwrite`。`load` 里用不上的 `where`、`keys`、`key` 写成 null 或空白时当作没写（验收后新增：格式说明让模型写 null，以前 `keys` 写 null 反而被退回）。
  - `mode` 只能是 `overwrite`、`replace_where`、`append`、`merge`（第 1、2 版的 `full`、`incremental` 也不行）：“city_daily 的 load.mode 只能是 overwrite、replace_where、append、merge”。
  - `replace_where` 要有 `where`（“… 按条件替换（replace_where），要给出替换条件 load.where”），形状检查同读取筛选（“… 的 load.where：替换条件不能有分号”），`${param.*}` 要写对、是项目定义了的参数。
  - `merge` 要有 `keys` 或 `primary_key`，`keys` 是这张表的列、不重复。
  - `where` 只用在 `replace_where`、`keys` 只用在 `merge`；第 1、2 版增量的 `key` 这一版不用，写了也让模型重写。
- **项目**：运行绑定了项目时，目标表名要以 `table_prefix` 开头（“目标表名 'x' 要以项目「…」的表名前缀 ibf_ 开头”），不能和项目引用表的表名（三段式名的最后一段，不分大小写）一样（“目标表名 'forecast' 和项目引用的表 prev.sales.Forecast 同名，请换一个”）。方案的 `language` 是 `sql`、而某张目标表的来源是带读取筛选的引用表时，平台把 `language` 改成 `pyspark`，`cleaned` 里写“prev.sales.forecast 有项目的读取筛选，SQL 代码暂不支持，代码语言改成了 PySpark”（验收后改的：以前让模型重写；再以前要到代码检查才发现，那时方案已经确认过了）。
- **运行参数**：`load.where` 和检查里的 `where`、`expr` 用到的 `${param.<key>}` 要是项目定义了的参数（“… 的运行参数 ${param.month} 不是项目「…」定义的参数”）；没绑定项目时不能用（“… 用了运行参数 ${param.update_month}，这次运行没有绑定项目，不能用运行参数”），也不查前缀。
- **`checks`**：模型建议的数据检查，格式同 `data_check` 的 Check，按 `aep.etl.checks.problems` 检查（问题写成 `checks[2].where：…`）。检查之前先去掉模型写空了的部分（验收后新增，不记 `cleaned`）：值为 null 的字段（模型常把各种检查的字段都写出来，用不上的写 null）、空白的 `where` 和 `expected_where`、空列表的 `group_by`，`reconcile` 两边里为 null 的字段和空白的 `where`。别的照原样检查。另外：
  - 最多 10 条；种类只能是 `aggregate`、`reconcile`、`unique`、`rows`，和老系统的逐行对比由 `data_check` 的 `golden` 加；
  - id 不能用 `golden`、不能重复，也不能和运行绑定的项目的验收检查同 id（“checks[1].id：c4_keys 是项目的验收检查，数据检查时会自动跑，方案里不用再写；要另加检查请换一个 id”），因为数据检查节点同时引用两边，id 重复时节点 `input_error`；
  - 表名（含 `reconcile` 两边的）只能是方案的目标表名和各目标表的 `sources`，不分大小写（“checks[4].table：sales.refunds 不是方案的目标表或来源，检查只能读方案写的表和读的源表”）。不带点的目标表名到 `data_check` 里就是本次运行的结果表，源表名照探查结果写。
- **产出**：`result_table` 是结果表的表名（同第 2 版“结果表”：`answer.target`，没有答案设计时最后一张目标表），`result_keys` 是它的 `primary_key`（没写时为 `[]`），给 `data_check` 的 `golden` 用；`checks` 是整理后的检查（没写 `severity` 的补上 `warn`：模型建议的检查默认只提醒，要拦住结果得在方案里写 `error`，给人看的方案里写明“不通过时结果不能直接通过”；验收后从 `error` 改成 `warn`），同 `plan.checks`，可以整段给 `data_check` 的 `include`。没通过校验时三项为 `""`、`[]`、`[]`；第 1、2 版也是。
- **提示词**：模型按 `aep.etl.plan.OUTPUT_FORMAT_V3` 回答（新增，前两个不动）。
- **`message`**：技术方案里每张表的“加载方式”一行换成“写表方式”（整表覆盖、只替换满足 `…` 的行、追加、按 `…` 合并），最后加“数据检查”一节，每条一行：名称（不是 `error` 的标出“警告”“提示”）、种类和一句话说明，如“**每天每个城市一行**（警告）：键不重复，`city_daily` 按 `stat_day`、`city` 没有重复的行”；没有检查时写“方案没有建议数据检查。”

### 应用模板 `etl-lakehouse` 第 3 版（应用层）

模板第 2 版 `etl-lakehouse` 留着不动（不绑定项目的原型方式）。第 3 版是另一个模板：id `etl-lakehouse-project`，标题“数据 ETL（湖仓，绑定项目）”，说明“在数据项目里做 ETL：探查项目的引用表，按口径和开发规范出方案并写明写表方式，确认后写代码，在项目的开发环境试算，核对数据（可以和老系统逐行对比），审核通过后交付。”，`needs` 同第 2 版。模板没有版本号，用户从模板建的应用各自有版本，所以新写法用新 id，不改第 2 版。

给绑定项目的数据团队用，节点和第 2 版一样，只有这些不同：

- 开始节点 `project: "required"`；输入：数据源 `datasource`（必填；Databricks 数据源，数据检查也经它读表）、需求 `request`（必填）、表范围 `tables`（高级选项，空着时探查项目的引用表）、老系统结果表 `golden`（高级选项，三段式名，可选）。不再有 bundle 名 `name`，bundle 用第一张目标表的名字。
- 确认方案：说明和提示里加上写表方式和建议的数据检查，确认后“写代码，在项目的开发环境试算”（Databricks 上的表不复制）。
- 出方案：Skill `etl-plan-project`（版本 1），`system` 为 `OUTPUT_FORMAT_V3`，提示词在需求后面加“项目说明（环境、表名前缀、引用表、运行参数）：{{project.brief}}”，在源表后面加“本项目在环境里已有的表（不是源表）：{{profile.existing_description}}”（验收后新增）。
- 校验方案：`format: 3`，`existing: "{{profile.existing}}"`（验收后新增），不设 `language`（默认 PySpark，模型可以在方案里写 `sql`）。
- 写代码：Skill `etl-codegen-project`（版本 1），提示词开头加“项目说明（表名前缀、引用表、运行参数）：{{project.brief}}”。
- 开发环境试算之后加节点 `checks`（`data_check`，标题“数据检查”，`datasource: "{{start.datasource}}"`，`include: ["{{project.checks}}", "{{check_plan.checks}}"]`（项目的验收检查在前，试点后加上），`golden: {"expected": "{{start.golden}}", "table": "{{check_plan.result_table}}", "keys": "{{check_plan.result_keys}}"}`，`only_if: "{{dev_run.ok}}"`，`max_visits: 9`，出错重试 2 次）。用户填了老系统结果表时多一条和老系统的逐行对比，没填时没有。试算的 `passed`、`failed` 都连到它，它的两个分支都到“审核试算结果”；试算没通过时它不调连接器，`summary` 为“前一步没有通过，没有做数据检查”。
- 审核试算结果：`data` 加 `checks: "{{checks.results}}"`、`checks_summary: "{{checks.summary}}"`，`show` 在试算报告后接检查报告；“通过”的 `requires` 是 `["{{dev_run.ok}}", "{{checks.ok}}"]`，试算和检查都通过才显示。意见可以挂在某条检查上（`check:<检查 id>`，`feedback` 里叫“数据检查「名称」”）。
- 交付和终止的结果都加 `checks`（检查报告）。

两个 Skill 是新的内置 Skill，不是 `etl-plan-brief`、`etl-codegen` 的版本 2：内置 Skill 只在名字不存在时登记（见 apps-and-skills.md），公司可能已经发布了自己的版本 2，平台不能占用它的版本号。`etl-plan-project` 在 `etl-plan-brief` 的写法上加项目（前缀、三段式源表、读取筛选自动加、`${param.*}`）、写表方式怎么选和数据检查怎么建议（2–6 条，不写 golden）；`etl-codegen-project` 写 `source()` 读三段式表、`param("key")` 取参数（参数名写字面值）、不写表、`replace_where` 只算要替换的那部分行、`merge` 的键不重复。

人工节点的数据最多 16 个键（原来 12）：审核试算结果多了两项，已经是 14 个。

“通过”的 `requires` 用到人工节点的一种新写法：除了一个整段引用，还可以是 1–5 个整段引用的列表，全部为 true 时才能选。这是对人工节点的通用扩展，同步写进 flow-apps.md。

上线顺序（每步一个 PR，前面的不依赖后面的）：数据外发策略 → 项目和运行绑定（含 `{{param.*}}`、`{{project.*}}` 和项目页面）→ Databricks 只读连接器和探查、抽取 → 读源表和参数、写到项目环境、写表方式 → `data_check` → 方案第 3 版 → 模板第 3 版和两个新 Skill。之后用 Table 2 Forecast 的模拟数据试点。

## 第五批：上线到 prod（`databricks_release`）

以前只有代码应用 `etl` 能把审核过的代码上线到 prod（`DevTarget.release`，见 `aep.databricks`）。第五批把它做成平台的节点类型，任何编排应用都能在审批后上线；两个 ETL 模板在“审核试算结果”后面加上“上线审批”和“上线到 prod”。按时刷新由 Databricks 上的 Job 自己按 Quartz cron 跑，平台不参与；平台按时发起整次运行另见 [schedules.md](schedules.md)。

### databricks_release 上线到 prod（新节点）

`category: "data"`、`error_handling: true`，流程视图的 `kind` 为 `tool`。会写 Databricks，不能单独试跑（同 `databricks_dev_run`）。

```jsonc
{"bundle": "{{check_code.bundle}}",        // 整段引用，必填；同 databricks_dev_run
 "schedule": "{{release_review.values}}",  // 可选：Quartz cron，见下；空时用 AEP_DATABRICKS_PROD_SCHEDULE
 "environment": "prod"}                    // 可选：绑定项目的运行上线到项目的哪个环境；默认 "prod"
```

- `bundle`：同 `databricks_dev_run`，必须恰好是一个引用，取到的不是检查并打包节点的 bundle 时 `input_error`。
- `schedule`：Quartz cron 文字（Databricks Job 的写法：秒 分 时 日 月 周 [年]，如每天 2 点 `0 0 2 * * ?`），可以含引用；恰好是一个引用、取到对象时取它的 `schedule` 键（给确认模式人工节点的 `values` 用：题的 key 叫 `schedule`）。取到空值时用平台的 `AEP_DATABRICKS_PROD_SCHEDULE`。写成不含引用的文字时保存检查写法：6 或 7 段，每段只有数字、字母和 `* ? / , - # L W`，不合格报错“定时要写成 Quartz cron，如每天 2 点：0 0 2 * * ?”；运行时取到的值不合格时 `input_error`，文字同上。时区用 `AEP_DATABRICKS_PROD_TIMEZONE`。
- `environment`：写法同项目环境的 key，最长 32 个字符；不绑定项目的运行不看它。

执行：

- 不绑定项目：同代码应用 `etl` 的上线，`DevTarget.release(bundle, schedule)`，写到 `AEP_DATABRICKS_PROD_CATALOG`.`AEP_DATABRICKS_PROD_SCHEMA`，集群用 `AEP_DATABRICKS_PROD_CLUSTER_ID`（空时用 `AEP_DATABRICKS_CLUSTER_ID`）。
- 绑定项目：上线到项目里 key 为 `environment` 的环境，写到它的 catalog 和 schema，集群用它的 `cluster_id`（空时同上）；不看 `AEP_DATABRICKS_PROD_CATALOG`、`_SCHEMA`。Job 的项目变量（`project`、`table_prefix`、`adopted`、`params`）取这次运行的快照，所以 prod 上每次刷新都用这次运行的参数值；`run_id` 是 Job 运行自己的 id。目标表名和试算一样要以项目的表名前缀开头。下面这些情况不部署，走 `failed` 分支，`errors` 写明原因（让人改了项目再试，或者先不上线；不让整次运行失败）：
  - 运行方式是 `pipeline`：“绑定项目的运行请用 job 方式上线（AEP_DATABRICKS_DEPLOY）”。
  - 项目没有这个环境：“项目「…」没有环境 prod”。
  - 这个环境就是这次运行试算用的环境：“上线的环境 dev 就是这次试算的环境，请选别的环境”。
  - 目标表不以前缀开头：同 `databricks_dev_run`。
  - 以前的运行快照里没有项目的全部环境：“这次运行绑定项目时还没有记下项目的全部环境，请重新发起运行”。为此，运行快照（projects.md）新增 `environments`：项目当时的全部环境，原样照抄；`environment` 照旧是选中的那个。
- 绑定项目的 Job 名是“`<bundle 名>`（`<项目 id>`/`<环境 key>`）”，标签另加 `aep_project`、`aep_environment`，文件放在 `.bundle/<bundle 名>/<环境 key>/releases/…`：同一个项目的不同环境、不同项目的同名 bundle 各是各的 Job。不绑定项目时 Job 名、标签和文件位置都同代码应用 `etl`。
- 同一个 Job 再上线是更新它（换文件、换定时），不新建；上线失败时正在跑的 Job 不变（同 `etl`）。
- 没配置 Databricks 工作区时以 `databricks_error` 失败。工作区配了但上不了线（`cluster` 运行方式；不绑定项目时没配 prod 目标）不算出错，走 `failed` 分支，`errors` 写明原因，让人决定先不上线还是找管理员。部署时 Databricks 接口报错也走 `failed`。

产出：

| 字段 | 内容 |
| --- | --- |
| `status` | `deployed` 或 `failed` |
| `ok` | `status` 是不是 `deployed` |
| `release` | `Release.as_dict()`：`mode`、`catalog`、`schema`、`schedule`、`timezone`、`steps`、`errors`、`job_id`、`pipeline_id`、`cluster_id`、`url`；另加 `environment`（绑定项目时是环境 key，否则 `"prod"`）、`job_name` |
| `url` | Databricks 上这个 Job（或管道）的链接，没有时为 null |
| `job_id` | Job 的 id，没有时为 null |
| `schedule` | 用的 Quartz cron |
| `schedule_text` | 定时的中文说法：常见写法（每天、每小时、每周几、每月几号的几点几分）写成“每天 02:00（Asia/Shanghai）”这样，其他写成“按 Quartz cron 0 15 10 ? * 6L（Asia/Shanghai）” |
| `errors` | 失败时的原因，一条一行；成功时为 `""` |
| `summary` | 一句话，如“已上线到 prod：Job 100，每天 02:00（Asia/Shanghai）刷新 `icsd-ace-prod`.sales_mart” |
| `report` | Markdown 报告：成功或失败、环境、写到哪个 catalog 和 schema、集群、定时、Job 链接、每一步的结果、失败原因 |

分支：`deployed`（已上线）、`failed`（失败）。运行过程里的那句话是 `summary`。

### 两个 ETL 模板加上线

`etl-lakehouse` 和 `etl-lakehouse-project` 都在“审核试算结果”和“交付”之间加两个节点。已经从模板建好的应用不变（包括 jadeflow 试点的 T2 应用），新建的应用才有。

```jsonc
{"id": "release_review", "type": "human", "title": "上线审批", "mode": "approval",
 "prompt": "试算结果已审核通过。上线后，prod 环境按选的时间自动刷新结果表；先不上线就只交付开发环境的结果。",
 "show": "{{check_code.summary}}\n\n{{release.report}}",
 "fields": [{"key": "schedule", "label": "上线后什么时候自动刷新？", "type": "select",
             "choices": [{"value": "0 0 2 * * ?", "label": "每天 02:00"},
                         {"value": "0 0 * * * ?", "label": "每小时整点"},
                         {"value": "0 0 2 ? * MON", "label": "每周一 02:00"},
                         {"value": "0 0 2 1 * ?", "label": "每月 1 日 02:00"}],
             "suggested": "0 0 2 * * ?"}],
 "options": [{"key": "release", "label": "上线到 prod"}, {"key": "skip", "label": "先不上线，交付"}],
 "max_visits": 3}

{"id": "release", "type": "databricks_release", "title": "上线到 prod",
 "bundle": "{{check_code.bundle}}", "schedule": "{{release_review.values}}", "environment": "prod"}
```

- 第一次到上线审批时 `release.report` 还是空的，`show` 只有检查并打包的一句话。
- 连线：“审核试算结果”的 `approve` 改到 `release_review`；`release_review` 的 `release` 到 `release`，`skip` 到 `end`；`release` 的 `deployed` 到 `end`，`failed` 回到 `release_review`（`show` 里能看到失败原因，再试或先不上线）。
- 上线审批是确认模式的人工节点，要审批者处理（auth.md）。
- 交付的结果加 `release`（`{{release.summary}}`，没上线时为空）和 `release_url`（`{{release.url}}`）；交付节点的说明改成“……选了上线时代码已部署到 prod，按选的时间自动刷新；没选时数据还在开发环境。”
- 应用说明书、画布和运行页的流程视图照常显示这两个节点。
- 两个模板的说明末尾“审核通过后交付。”改成“审核通过后可以上线到 prod 定时刷新。”。

### 前端

- 编辑页：节点库“数据工程”里加“上线到 prod”，表单有 bundle（变量选择）、定时（文字，可插入变量）、上线环境（文字，默认 prod，说明“只对绑定项目的运行有用”）。保存检查同后端。
- 运行页：节点详情里上线成功时显示 Job 链接和定时的中文说法；失败时显示原因。
- mock：`databricks_release` 按设置模拟成功（Job 100，写到 `demo_prod`.retail_mart）；bundle 名里含 `fail` 时模拟失败（没配 prod 目标）。mock 里的演示应用 `retail-etl-v2` 是以前录好的流程，不加这两个节点。

## 测试

- 后端：5 种节点各自的单元测试（假连接器、假 `DevTarget`、`FakeGateway`）；模板 `etl-lakehouse` 用假模型从头跑到 `end`（含一次方案驳回、一次代码检查不过、一次可修的 dev 报错）；循环规则；说明书接口（编排应用和 `etl`）；`read_rows` 分页和权限（三个连接器）；`aep.demo` 用假 API 测幂等。
- 第二批另加：
  - 方案第 2 版：英文前缀去掉、业务文字里的代码（含不该误判的例子，如全角括号、`GMV(含税)`；也含汉字紧跟 `sum(` 这种要查出来的例子）、题改成规则、答过的题改成规则（包括前几轮答过的；重写后的方案把 `q1` 用在另一道题上时不算答过）、整理后的题都能通过人工节点的检查、可加性判断（`sum`、`count`、`count(DISTINCT)`、比值、剥壳）；代码应用 `etl` 的方案和结果不变（test_etl）。
  - 人工节点：问题必答、`changed_branch`、`requires` 隐藏的选项提交被拒、意见挂在条目上和找不到条目时的 422、`seen` 不一致时 409、`feedback` 的写法和确认时清空、没有扩展设置的节点（包括只有 `fields` 的填写模式节点）`pending` 只多一个 `changed_branch: null`、那句话不变。
  - 模板第 2 版用假模型和假 Databricks 跑通：两次确认后交付；“按我的意见修改方案”；“改代码”后重新从第 1 次数；“改方案”后再确认、表没变时不再抽取（`reuse` 不写时照旧重新抽取）；“终止”走到 `stopped`；试算失败时没有“通过”。
  - 答案：两份集群辅助函数一字不差；答案设计作为数据传过去、列名复核；`aep.etl.answer` 的合计、占比条件（看 `mins`）、`format`、没有行时的结论、结论模板和引号转义；`files.zip` 读 `result.files`、`result.code.files` 和等待中的 bundle，形状不对的跳过。
- 第三批另加：
  - 表名：三段式拆分（带 `-` 的 catalog）、`catalog.schema.*` 展开；Databricks 数据源上 `tables` 为空时用项目引用表、没项目时 `input_error`；引用表的筛选传到 `table_stats`、`sample_rows`，全表行数不带筛选（`unfiltered_row_count`，`description` 里的“筛掉了 N 行”，“只发结构”时不写）；抽取 Databricks 的表不读不写、`copied` 为 false。
  - 代码：`SOURCE_FILTERS` 和 `param()` 写进管道文件；`param()` 参数不是字面值、不是项目参数时检查不过；SQL 读带筛选的表检查不过；`${param.*}` 的字面值（文字转义、日期、数字、布尔、空值）。
  - 代码（试点后新增）：能 import 的标准库和 pyspark 模块通过，别的模块、相对导入、`import *`、import 成平台的名字检查不过；没有定义的名字检查不过；`warn()` 记下告警，集群带回的告警进 dev 运行的 `warnings`；加了单引号的 `'${param.x}'` 不再套引号（文字、数字、日期、空值）。
  - 写表：绑定项目时结果写到环境的 catalog 和 schema、用环境的集群；前缀不对时 `input_error`；归属的三种情况（不存在、有项目标记或登记在 `adopted`、别的项目或没有标记）；写完的表属性；四种写表方式各自的 Spark 调用、`merge` 键重复、`tables[].write`（假的 `DESCRIBE HISTORY`）；第 1、2 版方案的 `full`、`incremental` 结果不变。
  - `data_check`：`only_if` 不成立时不调连接器、走 `failed`；五种检查各自调的连接器工具和参数（表名换成三段式、筛选和 `where` 用 AND 连起来、`${param.*}` 换成字面值）、通过和不通过、`tolerance` 和 `relative`、`group_by` 对不上的组、golden 的 missing/extra/different、节点 `golden` 加检查（`expected` 为空时不加、`keys` 为空时 error、已有 id golden 时不加）、键重复时 failed 或 error、`rows` 查出行、不是 Databricks 数据源时 `input_error`、单条出错不影响其他条、`skipped`、`severity` 和 `ok`、表名解析和筛选、id 重复时 `input_error`。
  - 方案第 3 版：写表方式的检查、前缀、`checks` 的检查和产出、没绑定项目时用 `${param.*}` 让模型重写；第 2 版的产出不变；检查 id 和项目验收检查重复、SQL 方案读带筛选的引用表都让模型重写（试点后新增）。
  - 表结构变化（验收后新增）：探查列出环境里前缀开头的表（只读结构）、没有时为 `[]`、列表失败和不是 Databricks 时为 null；方案里加的列和位置（第一列、连着加几列）、模型写的 `add_columns` 被去掉、少了已有的列让模型重写、`overwrite` 不加列、`message` 的“表结构”一行、`existing` 的保存检查；集群程序只加确认的列（位置、大小写、已经有的跳过）、少列和多列不写、`after` 不在表里不写、加列后写入失败的提示、不再 `mergeSchema`，没绑定项目时照旧；`write_text` 写出加了的列。列的类型（I-14）：类型的统一写法和不是类型的写法、新加的列没写类型和已有的列改类型让模型重写、`add_columns` 带类型、“表结构”一行带类型；集群程序里新加的列类型和确认的不一样不加列不写、已有的列不是一类值不写、数字宽度不同照写。
  - `data_check` 的节点 `golden` 带 `tolerance`、`severity`（试点后新增）：传给连接器的容差、结果的级别、只警告时节点照样通过、保存时的取值检查。
  - 已知差异（I-4）：`golden` 两次 `compare` 的条件、只有已知差异时通过、没出现的已知差异、`summary` 的“只有已知差异”；`rows` 三次调用的条件和例子只取新的行；分组 `aggregate`、`reconcile`、`unique` 的已知组；`known` 的保存检查。
  - 已知差异精确到格（I-16）：`golden` 同一行别的列不一致时照常不通过、`beyond` 和说明；`columns` 写了值时两边的值都要对上（数字和文字数字相同）；没写 `columns` 时只管少的行和多的行；分组的 `value`、`left`、`right`；这些字段的保存检查。
  - 查另一张表（R4）：`rows` 带 `lookup` 时 `aggregate`、`sample_rows` 的参数（另一张表的读取筛选和 `lookup.where` 用 AND 连起来、运行参数换成字面值）、`found` 两种、和已知差异一起用、说明的写法、保存检查（各字段、只有 `rows` 能写、`tables()` 和 `texts()` 包括它）；连接器拼的 `EXISTS` 语句和参数检查。
  - 模板第 3 版用假模型、假 Databricks 和假的 Databricks 连接器跑通：绑定项目、探查引用表（带读取筛选）、在项目环境试算、检查读环境里的结果表并和老系统对比后交付；检查不通过时不能选“通过”，挂在检查上的意见带给模型；试算失败时不做检查。
- 不连真实 Databricks 和真实模型；`DevTarget.ingest`、`preview` 用假 HTTP 测请求内容。
- 前端：类型检查通过；说明书面板和新节点表单有 Playwright 用例（沿用现有的 mock 方式）。第二批另加：确认方案页（每题必答、改了答案后主按钮变化、意见挂在条目上）、审核试算结果页（试算失败时没有“通过”）、结果页（答案卡、合计显示“—”、终止的运行、以前的运行的回退、用户自己建的应用输出叫 `code` 或 `brief` 时照以前显示）；mock 里另加第 2 版的运行（等确认方案、等审核、已交付、已终止），第 1 版的演示运行留作“以前的运行”的样例。
- 第五批另加：
  - `databricks_release`：不绑定项目时上线到平台的 prod 目标、定时取设置或人工节点的 `values`、空时用平台默认；Quartz cron 的保存检查和运行时检查；`schedule_text` 的几种写法；没配工作区时 `databricks_error`、上不了线和接口报错走 `failed`；绑定项目时上线到项目环境（catalog、schema、集群、项目变量、Job 名和标签），pipeline 方式、没有这个环境、就是试算环境、前缀不对、旧快照各自走 `failed` 并写明原因；同一个 Job 再上线是更新。
  - 运行快照多了 `environments`。
  - 两个模板用假模型和假 Databricks 跑通：审核通过后上线审批选“上线到 prod”交付、选“先不上线”交付、上线失败回到上线审批。
  - 前端：上线节点的表单和保存检查、运行页的 Job 链接（Playwright，mock）。
