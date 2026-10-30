# 编排应用接口（通用应用编排）

后端（Claude）和前端（Codex）按这份约定并行开发，设计说明见[通用应用编排设计](https://claude.ai/artifact/WikAUpczduF4g2KHFrFBz8)。本文只写在 [apps-and-skills.md](apps-and-skills.md) 之上新增和改变的部分；没提到的接口和字段保持不变。字段名一律 snake_case，错误格式同前：`{"detail": "中文说明"}`，校验错误为 422，`detail` 是 `[{"loc": [...], "msg": "..."}]`。

## 概念

- **代码应用**（`kind: "code"`）：流程结构写在代码里，页面上只能调节点设置。目前只有 `etl`。
- **编排应用**（`kind: "flow"`）：流程结构是应用配置里的一份 `graph`，平台把它编译成 LangGraph 图来运行。可以在页面上新建、复制、导入、归档。`hello` 改成编排应用，应用标识、输入和结果不变。
- 编排应用的结构和设置都在同一份应用配置里，所以照旧走草稿 → （有评测集时）评测 → 发布 → 回滚。一次运行从头到尾只用一个版本的配置。

## 编排应用的配置

代码应用的 `AppConfig` 仍是 `{"nodes": {...}}`。编排应用的 `AppConfig` 是：

```jsonc
{
  "schema_version": 1,           // 配置格式版本；目前只有 1
  "env": {
    "vars": [EnvVar],            // 常量，跟着版本走
    "secrets": ["crm_token"]     // 用到的密钥名；值另存（见“环境变量和密钥”），不进版本
  },
  "graph": {
    "nodes": [Node],
    "edges": [Edge]
  }
}

// EnvVar
{"key": "refund_limit", "type": "string" | "number" | "boolean", "value": 500, "description": "退款审批金额阈值"}

// Edge：普通节点一条不带 branch 的出边；有分支的节点每个分支一条
{"from": "route", "to": "draft", "branch": "after_sales"}
```

### 节点的公共字段

```jsonc
{
  "id": "draft",              // ^[a-z][a-z0-9_]{0,31}$，应用内唯一；env、secret 保留
  "type": "llm",              // 见下面的节点类型
  "title": "起草售后回复",     // 1–50 字符
  "max_visits": 3,            // 一次运行里最多跑几次，1–10，不写为 3；超过时运行失败并写明原因，不能重试（见下）
  "on_error": OnError,        // 只有 llm、classifier、tool、knowledge、agent、code 可以设；不写等于 {"then": "stop"}
  "ui": {"x": 120, "y": 40},  // 留给以后的画布，平台原样保存，不参与运行
  "note": "讲解说明"           // 可选，最多 500 字符，只给人看，见 data-nodes.md
}

// OnError
{
  "retry": {"max": 2, "interval_seconds": 1},   // max 0–3，interval_seconds 0–60；不写为不重试
  "then": "stop" | "default" | "branch",         // 重试后仍失败：停止运行 / 用默认值继续 / 走 error 分支
  "default": {"text": "暂时无法回答"}            // then 为 default 时必填，键是这个节点的产出字段
}
```

节点出错并且 `then` 是 `default` 或 `branch` 时，这个节点除了产出（`default` 时是默认值，`branch` 时为空），还有 `error_type` 和 `error_message` 两个变量，后面的节点可以引用。`error_type` 取值：`timeout`、`model_error`（网关或模型报错）、`output_format`（JSON 输出不合格式）、`tool_error`、`knowledge_error`（知识库不存在或检索失败）、`code_error`、`sandbox_unavailable`、`step_limit`（Agent 达到步数上限）、`databricks_error`、`input_error`（运行输入或节点设置有问题，比如表名写错、超过行数上限、没选数据源；再试结果一样，所以不按 `retry` 重试，运行页也不提供重试）。

整个运行失败时，运行详情的 `failed.error_type` 除了上面这些，还可能是 `max_visits`（某个节点跑满了 `max_visits`）或 `max_steps`（总步数到了上限，见“保存草稿时的检查”最后一句）。这两种和 `input_error` 一样不能重试：再从同一步接着跑，马上又会到上限。运行页只给“复制并重新发起”，并用业务语言说明原因，比如“在「确认方案」这一步，流程在同一步来回的次数到了上限”：只用 `failed.title`，上限是几次不在接口里。见 `backend/src/aep/api/runs.py`。

### 变量引用

- 写法 `{{节点标识.字段}}`，例如 `{{start.question}}`、`{{draft.text}}`；`{{env.refund_limit}}` 引用常量；`{{secret.crm_token}}` 引用密钥。
- 前端可以用这个正则找出引用：`\{\{\s*([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)\s*\}\}`。
- 文本里的引用按文本插入（对象和数组插入为 JSON）。还没有值的变量插入为空字符串。
- 工具参数和代码节点输入里，如果整个值恰好是一个引用（如 `"{{lookup.result}}"`），传原始值而不是文本。
- `{{secret.*}}` 只能出现在工具节点的 `args` 里，写在别处保存时报错。密钥的值不会出现在运行记录、流程视图、追踪和日志里。
- 只能引用一定已经跑过的节点。例外：循环里可以引用本轮循环后面的节点（如起草节点引用 `{{review.comment}}`），第一次跑时为空。一个节点重复跑时，变量取最近一次的值。

### 节点类型

每种类型的设置字段如下（公共字段不再重复）。“产出”是这个节点写进变量池的字段，“分支”是它的出边要用的 `branch` 名。

**knowledge 知识检索**的设置、产出和引用编号规则见 [knowledge.md](knowledge.md#编排应用知识检索节点)。

**数据工程节点** `etl_profile`、`etl_plan_check`、`databricks_ingest`、`etl_code_check`、`databricks_dev_run` 见 [data-nodes.md](data-nodes.md)。

**start 开始**：恰好一个。

```jsonc
{"inputs": [{"key": "question", "label": "客户问题", "type": "text" | "textarea" | "number" | "boolean" | "select" | "datasource",
             "required": true, "default": null, "choices": [{"value": "a", "label": "A"}],   // choices 只用于 select
             "max_length": 2000}]}   // 1–20 个；max_length 只用于 text、textarea
```

产出：每个输入字段。`datasource` 类型的值是 `datasource_id`。无分支。

输入还可以写两个只管新建运行页怎么显示的属性，都是可选的：

```jsonc
{"key": "request", "label": "需求", "type": "textarea", "required": true,
 "examples": ["按月、按城市统计有效订单的销售额……", "统计每个品类每月的销售额和毛利率……"]}   // 可选，不写为 []
{"key": "tables", "label": "表范围", "type": "text", "advanced": true}                       // 可选，不写为 false
```

- `advanced`：true 时，新建运行页把这个输入收进默认收起的“高级选项”。必须是 true 或 false。必填又没有默认值的输入不能收起来（使用者看不见就没法填），保存时报错“必填且没有默认值的输入不能收进高级选项”。
- `examples`：需求框下面的例子，点一下就用这句替换框里的内容。只有 `text`、`textarea` 能写；0–5 条，每条 1–2000 个字符、不能只有空格，设了 `max_length` 时也不能超过它。
- 两个属性都不改输入的校验、产出和运行标题。`AppDetail.inputs` 和 `RunDetail.inputs` 原样带上它们，以前的配置没有这两个键，按 false 和 `[]` 处理。
- 只有开始节点的输入能写；人工节点填写模式的字段写了，保存时报错“只有开始节点的输入可以设”。
- 报错的 `loc` 指到具体位置，如 `["body", "config", "graph", "nodes", "start", "inputs", 2, "advanced"]`、`[..., "inputs", 1, "examples", 0]`。
- 编辑页的开始节点表单：每个输入可以勾“收进高级选项”（`advanced`）；`text`、`textarea` 输入另有“例子”列表（`examples`），可以增删改。把类型改成别的种类时清掉 `examples`，免得保存时报一个表单上看不见的错。

**llm 调用模型**

```jsonc
{"model": "company",                  // 模型别名，null 为平台默认模型
 "skills": [{"name": "after-sales-policy", "version": 2}],
 "system": "你是售后客服……",           // 可选，最多 20000 字符；拼在 Skill 正文后面
 "prompt": "客户问：{{start.question}}", // 必填，最多 20000 字符
 "output": {"kind": "text"}
        |  {"kind": "json", "fields": [{"key": "amount", "type": "string" | "number" | "boolean" | "array" | "object", "description": "退款金额"}]},
 "quiet": false}                       // 可选，默认 false；true 时文本回答不写进运行过程
```

产出：`text`；JSON 输出时是每个字段（不再有 `text`）。模型没按格式回答时自动重试一次，仍不对就按 `output_format` 出错处理。无分支（设了失败分支时另有 `error`）。

`quiet` 只管运行过程（`messages`），不改产出：设了 `quiet` 的节点照样产出 `text`，后面的节点照常引用，流程视图里照常能看。给回答只是中间结果的步骤用，比如 `etl-lakehouse` 模板的「出方案」「写代码」。JSON 输出本来就不写进运行过程。`quiet` 不是 true 或 false 时保存报错，`loc` 指到 `quiet`。

**classifier 意图分类**

```jsonc
{"model": null, "skills": [],
 "text": "{{start.question}}",          // 要分类的文本
 "instruction": "",                      // 可选，补充分类说明
 "classes": [{"key": "after_sales", "label": "售后", "description": "退换货、维修、投诉"}]}   // 1–10 类，key 规则同节点 id，other 保留
```

产出：`class`（类别 key，没有命中时为 `other`）、`label`。分支：每个类别的 key，另加 `other`。

**condition 条件判断**：按顺序判断，第一条成立的规则决定分支，都不成立走 `else`。不调模型。

```jsonc
{"rules": [{"key": "big_refund", "label": "大额退款",
            "match": "all" | "any",
            "conditions": [{"left": "{{extract.amount}}", "op": "gt", "right": "{{env.refund_limit}}"}]}]}
// 1–10 条规则，每条 1–10 个条件；key 规则同节点 id，else 保留
// op：eq ne contains not_contains gt gte lt lte empty not_empty；empty 和 not_empty 不用 right
// right 可以是字面值或引用；gt/gte/lt/lte 两边都能转成数字才成立
```

产出：`rule`（命中的规则 key 或 `else`）。分支：每条规则的 key，另加 `else`。

**tool 调用工具**

```jsonc
{"datasource": "ds_1a2b",               // 已登记数据源的 datasource_id，也可以是整段引用，如 "{{start.datasource}}"
 "tool": "run_query",                   // 该数据源 GET /datasources/{id}/tools 里的工具名
 "args": {"sql": "select count(*) from orders where id = '{{ask_order.order_id}}'"}}
```

产出：`result`（工具返回的内容；是 JSON 就解析成对象）。这一期只能调已登记的数据源和工具服务的工具，而且只支持只读工具：数据源连接器都是只读的，工具服务由登记的人确认。

`datasource` 写成引用时（通常引用开始节点 `datasource` 类型的输入），保存时只检查引用本身，工具名和参数到运行时才对照那个数据源检查；没选数据源或数据源不存在，节点以 `input_error` 失败；数据源没有这个工具或连接器报错，以 `tool_error` 失败。agent 的 `tools[].datasource` 同理。工具服务（`kind` 为 `mcp`）只能用在 `tool` 和 `agent` 节点，见 [datasources.md](datasources.md)。

**agent 智能体**：有步数上限的“模型 → 工具 → 模型”循环。

```jsonc
{"model": null,
 "skills": [{"name": "sales-glossary", "version": 1}],   // 按需加载：提示词里只放名称和说明，模型要用时再读正文
 "task": "根据数据回答：{{start.question}}",               // 必填，最多 20000 字符
 "tools": [{"datasource": "ds_1a2b", "tools": null}],     // 0–10 个；tools 为 null 表示这个数据源的全部工具，否则列工具名
 "knowledge": null,                                        // 可选，知识库检索工具；和 tools 至少要有一个，见 knowledge.md
 "max_steps": 8}                                           // 1–20，一次工具调用算一步
```

产出：`text`（最终回答）、`steps`（`[{"tool", "datasource", "args", "error"}]`，按调用顺序）、`hits`（知识库检索命中的片段，没设 `knowledge` 时为 `[]`）。知识库怎么作为工具给模型用，见 [knowledge.md](knowledge.md#编排应用智能体调用知识库)。

**human 人工**：暂停运行等人处理，两种模式。

```jsonc
// 确认模式
{"mode": "approval",
 "prompt": "确认这条回复吗？",
 "show": "{{draft.text}}",                 // 可选，给人看的内容，按 Markdown 显示
 "options": [{"key": "approve", "label": "确认"}, {"key": "reject", "label": "驳回"}],   // 2–5 个；key 规则同节点 id
 "comment_required": ["reject"]}

// 填写模式
{"mode": "form",
 "prompt": "请提供订单号",
 "show": "",
 "fields": [{"key": "order_id", "label": "订单号", "type": "text", "required": true}]}   // 1–20 个，字段写法同 start 的 inputs，但没有 datasource 类型
```

产出：确认模式是 `decision`、`comment`、`actor`、`values`、`notes`、`changed`、`feedback`、`record`（后五个见下）；填写模式是每个字段和 `actor`。分支：确认模式每个选项的 key；填写模式无分支。

**人工节点的扩展设置**（第二批新增）。下面的设置都是可选的（值为 null 的和没写一样）；一个都没写的人工节点和以前一样：等待时的 `pending` 只多一个 `changed_branch: null`，`details` 仍然只有 `type`、`node`、`show`；运行过程里的那句话不变；确认模式多了几个产出。

```jsonc
{"mode": "approval",
 "prompt": "请检查方案，回答每道问题",
 "view": "etl_plan_review",                         // 可选：页面用哪种专门的样子显示
 "show": "{{check_plan.message}}",                  // 页面不认识 view 时照旧显示它
 "data": {"brief": "{{check_plan.brief}}", "plan": "{{check_plan.plan}}"},   // 可选：给 view 用的数据
 "fields": "{{check_plan.questions}}",              // 可选：确认时要回答的题，列表或整段引用
 "changed_branch": "revise",                        // 可选：改了建议答案或写了意见时按哪个选项处理
 "options": [{"key": "approve", "label": "确认方案，开始做"},
             {"key": "revise", "label": "按我的意见修改方案"}],
 "comment_required": ["revise"]}

// 选项可以带 requires：值不是 true 时这个选项不显示，提交了也不接受
{"key": "approve", "label": "通过", "requires": "{{dev_run.ok}}"}
```

| 设置 | 模式 | 不写时 | 规则 |
| --- | --- | --- | --- |
| `view` | 两种 | null | 写法同节点 id，最多 32 个字符。页面认识的有 `etl_plan_review`、`etl_dev_review`，见 [data-nodes.md](data-nodes.md) 的“两个人工步骤的页面”。null 或页面不认识时，照旧把 `show` 按 Markdown 显示，`data` 不显示 |
| `data` | 两种 | `{}` | 对象，最多 12 个键，键的写法同节点 id。值可以是字面值或引用：恰好是一个引用时取原始值，其余按文本替换，对象和数组里的引用逐层替换（同工具参数）。只放页面要显示的东西，代码这类大块内容不要放，页面另从 `GET /runs/{id}/flow` 取 |
| `fields` | 确认模式 | 不带题 | 要人回答的题，见下 |
| `changed_branch` | 确认模式 | null | 必须是某个选项的 key，这个选项不能设 `requires`。提交时改了建议答案、写了意见或挂了意见，就按这个选项处理，见“人工节点在工作台上的样子” |
| `options[].requires` | 确认模式 | 总是显示 | `true`、`false` 或恰好是一个引用（如 `"{{dev_run.ok}}"`）。到这一步暂停时取值，恰好是 `true` 才显示；`false`、null、文字、数字都算不显示。至少要有一个选项不设 `requires` |

确认模式的题 `fields`：
- 写成列表时 0–20 题，每题的写法同填写模式的字段（没有 `datasource` 类型），题目（`label`）最多 200 个字符。另可写 `suggested`（建议的答案，必须是这题能填的值）、`why`（为什么问，最多 500 个字符）、`affects`（影响到哪些条目，最多 20 个字符串，每个最多 64 个字符）。`suggested`、`why`、`affects` 的值为 null 时当作没写，保存时和运行时都不算不合格。
- 每题都必答：`required` 不用写，一律当作 true；不能写 `default`，保存时报错“题不能有默认值，每题都要人来答”。
- 也可以整段引用别的节点产出的题目列表，如 `"{{check_plan.questions}}"`。保存时只检查引用本身；到这一步暂停时取值，按上面的规则逐题检查：`default` 去掉、`required` 改成 true，其他不合格的题丢掉，超过 20 题的从第 21 题起丢掉。丢掉的题列在 `pending.details.dropped_fields`，页面上标出来。取到 null 或空文字时这一步不带题；取到别的不是列表的值，也当作不带题，并在 `dropped_fields` 里记一条。
- 写成列表的题在运行时也按整段引用的规则逐题检查，丢掉不合格的，记进 `dropped_fields`：以前确认模式的保存检查从来不看 `fields`，导入的文件可能给已经发布的版本留下从没检查过的题。

`comment_required` 里的选项，写了意见、挂了至少一条意见、或者改了建议答案，三者有一个就算满足（以前只认意见）。

保存时报错的 `loc` 指到具体设置：`[..., "nodes", "review", "view"]`、`[..., "data", "brief"]`、`[..., "fields", 0, "suggested"]`、`[..., "changed_branch"]`、`[..., "options", 2, "requires"]`。填写模式写了 `changed_branch` 或 `requires`，报错“只有确认模式可以设”。编辑页从来不写这些键，用编辑页建的确认模式节点保存结果不变；导入的配置里确认模式节点如果已经有 `fields`，第二批起会当作题（保存时按上面的规则检查，运行时见上一段）。

确认模式的产出：

| 字段 | 内容 |
| --- | --- |
| `decision` | 最后按哪个选项处理；按 `changed_branch` 改过时是它，分支也走它 |
| `comment` | 意见，原样 |
| `actor` | 处理人 |
| `values` | 每题的答案，如 `{"q1": "y2026"}`；不带题时为 `{}` |
| `notes` | 挂在条目上的意见 `[{"item": "metric:m4", "label": "客单价", "text": "保留两位小数"}]`；没有时为 `[]` |
| `changed` | 有没有哪题没按建议答（没写 `suggested` 的题不算） |
| `feedback` | 交给后面步骤（比如重写方案的模型）的一段话，见下 |
| `record` | 这次处理的完整记录，写法见“人工节点在工作台上的样子” |

`feedback` 写成 `【第 n 轮 · 节点标题 · 选项名】`，后面依次接没按建议答的题 `「统计哪段时间？」选「只看 2026 年」（原建议「源库现有的 12 个月」）`、挂的意见 `指标「客单价」：保留两位小数`、意见 `意见：……`，各部分用“；”连接。n 是这个节点在这次运行里第几次等人处理，节点标题是这次运行用的标题，选项名是最后按哪个选项处理的名称。只有最后的选项在 `comment_required` 里、或者就是 `changed_branch` 时才写；其余选项（如“确认”“通过”）的 `feedback` 为 `""`：这一步过了，旧意见就不再往后传给模型。

例子：

```text
【第 1 轮 · 确认方案 · 按我的意见修改方案】「统计哪段时间？」选「只看 2026 年」（原建议「源库现有的 12 个月」）；指标「客单价」：保留两位小数
【第 2 轮 · 审核试算结果 · 改代码】列「ads_sales_city_member_m.avg_order_value」：要保留两位小数；意见：其他没问题
```

填写模式的产出不变，不加 `record` 这些固定名字（字段的 key 是用户起的，可能重名）；`view`、`data` 两种模式都能用。编辑页的节点表单第一阶段只显示这些扩展设置、不提供编辑（写在模板或导入的文件里），保存时原样保留；改某个选项的 key 时，`comment_required` 和 `changed_branch` 跟着改；删掉 `changed_branch` 指的那个选项时，`changed_branch` 也删掉；切换模式时删掉这些键（不写成 null）。

**template 文本拼接**

```jsonc
{"template": "客户 {{start.name}} 的问题已转给人工客服。"}   // 最多 20000 字符
```

产出：`text`。

**code 代码**：在沙箱里运行 Python，详见设计文档。

```jsonc
{"code": "def main(inputs):\n    import pandas as pd\n    df = pd.DataFrame(inputs['rows'])\n    return {'total': float(df['amount'].sum())}",
 "inputs": {"rows": "{{lookup.result}}"},     // main 收到的 dict；值可以是字面值或引用，不能引用密钥
 "outputs": [{"key": "total", "type": "number"}],   // main 必须返回含这些键的 dict；type 同 llm 的 JSON 字段
 "timeout_seconds": 10}                          // 1–30
```

产出：`outputs` 里的每个字段。代码最多 20000 字符；可用 Python 标准库和 pandas、numpy；没有网络、没有凭据。

- 代码在沙箱连接器（`connectors/sandbox`，工具 `run_python`）里运行，平台经 `AEP_SANDBOX_URL` 调用它。
- `code` 原样交给沙箱，里面的 `{{...}}` 不当作变量引用；变量只能经 `inputs` 传入，`inputs` 的键只能用小写字母、数字和下划线。
- 保存时检查语法和顶层的 `def main`。
- 返回值里的 numpy、pandas 值会转成 JSON（DataFrame 按行转成列表），NaN 和无穷大变成 null。
- 出错时的 `error_type`：超时为 `timeout`；代码报错、返回值不是 dict、缺字段或类型不对为 `code_error`（报错信息附代码输出的最后几行）；连不上沙箱为 `sandbox_unavailable`。

**end 结束**：至少一个，没有出边。

```jsonc
{"outputs": [{"key": "reply", "from": ["{{draft.text}}", "{{answer.text}}", "{{handoff.text}}"]}],   // 1–20 个
 "outcome": "delivered"}   // 可选，第二批新增；不写为 null
```

`from` 依次取第一个有值的引用，所以几个分支可以汇到同一个结束节点。运行到结束节点时，`outputs` 组成运行的 `result`，例如 `{"reply": "……"}`。

`outcome` 说明运行停在这里算什么结果，写法同节点 id，不合格时保存报错，`loc` 指到 `outcome`。
- 运行到这个结束节点时，运行记录的 `outcome` 就是它（见 `backend/src/aep/api/runs.py`）。运行的 `status` 仍是 `done`，`result` 仍然只有 `outputs`，不加 `outcome` 键。
- 平台认识两个值：`delivered`（列表和页头显示“已交付”）、`stopped`（“已终止”）。别的值显示这个结束节点的标题。
- 一个应用可以有几个结束节点（本来就允许），各写各的 `outcome`。比如 `etl-lakehouse` 模板第 2 版的结束节点“交付”写 `delivered`，审核时选“终止”走到的结束节点“终止”（id `stopped`）写 `stopped`。
- 没写 `outcome` 的结束节点、以前的配置和以前的运行，`outcome` 都是 null，显示“已完成”。

### 保存草稿时的检查

`PUT /apps/{app}/draft` 校验不过返回 422，`loc` 指到具体位置。节点用 id 而不是下标定位，方便前端把错误显示在对应节点的对应设置下面：

| 问题 | loc 示例 |
| --- | --- |
| 节点设置不合法、引用了不存在的变量、引用了可能没跑过的节点 | `["body", "config", "graph", "nodes", "draft", "prompt"]` |
| 列表里的某一项 | `["body", "config", "graph", "nodes", "start", "inputs", 0, "key"]` |
| 某个分支没连、连了不存在的分支 | `["body", "config", "graph", "nodes", "route", "branches", "product"]` |
| 出边指向不存在的节点 | `["body", "config", "graph", "edges", 3, "to"]` |
| 节点从开始到不了、到不了结束、循环里没有带分支的节点 | `["body", "config", "graph", "nodes", "answer"]` |
| 整张图的问题（没有开始节点、超过 50 个节点） | `["body", "config", "graph"]` |
| 常量 | `["body", "config", "env", "vars", 0, "key"]` |

检查的完整清单：恰好一个开始节点，至少一个结束节点；节点 id 不重复；每个节点的每个分支恰好一条出边、没有多余的出边；从开始能到每个节点，每个节点能到某个结束节点；每个循环至少经过一个带分支的节点（人工、条件、分类和带分支的数据工程节点，`error` 分支不算）；变量引用的节点和字段存在且一定已经跑过（循环例外见上）；模型别名在网关上、Skill 版本已发布、数据源已登记、工具在该数据源上；`{{secret.*}}` 只在工具参数里且已在 `env.secrets` 里声明；最多 50 个节点、200 条边、50 个常量、20 个密钥。

运行时还有总步数上限：一次运行最多执行 200 个节点。

## 节点类型清单

`GET /node-types` 给“添加节点”和设置表单用。字段的完整写法以本文为准，这个接口提供显示用的文字、新节点的默认设置和预设。

```jsonc
[{
  "type": "llm",
  "label": "调用模型",
  "description": "调一次模型，产出一段文字或几个字段",
  "category": "model" | "logic" | "tool" | "data" | "human" | "terminal",
  "error_handling": true,                 // 能否设 on_error
  "outputs": [{"key": "text", "description": "模型的回答"}],   // 固定产出；随设置变化的（JSON 字段、表单字段、代码返回值）不在这里
  "defaults": {"model": null, "skills": [], "prompt": "", "output": {"kind": "text"}, "quiet": false},   // 新建节点时用，不含公共字段
  "presets": [{"key": "extract", "label": "抽取字段", "description": "从文本里抽取结构化字段",
               "settings": {"output": {"kind": "json", "fields": []}, "prompt": "从下面的文本中抽取字段：\n{{start.question}}"}}],
  "max_count": null                        // 一个应用里最多几个，start 为 1
}]
```

人工节点的 `outputs` 列出 `decision`、`comment`、`actor`、`values`、`notes`、`changed`、`feedback`、`record`，只有确认模式才有的几个在说明里注明“（确认模式）”。人工节点、开始节点和结束节点的 `defaults` 不变：新加的人工节点没有 `view`、`data`、题和 `changed_branch`，新加的结束节点没有 `outcome`，和以前一样。

## 应用

### 数据结构的变化

```jsonc
// AppSummary 新增
{"kind": "code" | "flow", "created_at": "……"}   // 代码应用的 created_at 为 null

// AppDetail 新增或改变
{
  "kind": "flow",
  "inputs": [Input] | null,     // 编排应用：开始节点的 inputs，新建运行页据此生成表单；代码应用为 null（etl 保留专用表单）
  "nodes": [...], "edges": [...],   // 编排应用由当前版本的 graph 生成，形状同前；edges 多一个 label（分支名的显示文字，普通边为 null）
  "secrets": [{"name": "crm_token", "set": true, "updated_at": "……"}],   // 当前版本和草稿里声明过的、以及设过值的密钥
  "options": {
    ...原有字段,
    "datasources": [{"datasource_id": "ds_1a2b", "name": "MySQL 示例库", "kind": "mysql"}]
  }
}
```

编排应用的 `current.config` 和 `draft.config` 就是上面的编排配置；`nodes[].settings` 为空数组（编排应用的设置在配置里直接编辑）。

### 新增接口

| 方法 | 路径 | 请求体 | 返回 |
| --- | --- | --- | --- |
| GET | `/node-types` | | `NodeType[]` |
| GET | `/app-templates` | | `[{"id", "title", "summary", "needs": ["datasource"], "config": AppConfig}]` |
| POST | `/apps` | `{"app", "title", "summary"?, "from": From, "actor"?}` | 201 `AppDetail`，配置成为版本 1；`app` 重名 409；配置不合法 422 |
| PATCH | `/apps/{app}` | `{"title"?, "summary"?}` | `AppDetail`；代码应用 409 |
| DELETE | `/apps/{app}` | | 204，归档；代码应用 409 |
| POST | `/apps/{app}/nodes/{node}/test` | `{"config"?, "values": {...}}` | `NodeTest`，见下 |
| GET | `/apps/{app}/versions/{version}/export` | | `text/yaml` 文件，见“导出和导入” |
| POST | `/apps/import` | `{"app", "title"?, "summary"?, "yaml", "actor"?}` | 201 `{"app": AppDetail, "problems": [...]}` |
| POST | `/apps/{app}/draft/import` | `{"yaml", "actor"?}` | `{"app": AppDetail, "problems": [...]}` |
| PUT | `/apps/{app}/secrets/{name}` | `{"value", "actor"?}` | 204 |
| DELETE | `/apps/{app}/secrets/{name}` | | 204；没有设过也是 204 |

- `app` 符合 `^[a-z][a-z0-9-]{1,63}$`，`title` 1–50 字符，`summary` 最多 500 字符。
- 模板有 5 个：`qa-approval`（问答加确认，和 hello 一样）、`intent-routing`（意图分类路由）、`datasource-qa`（数据源问答，Agent 调只读数据源工具，`needs` 含 `datasource`：开始节点有一个 `datasource` 类型的输入，Agent 的工具引用它，每次运行时由使用者选数据源，所以平台上至少要登记一个数据源）、`knowledge-qa`（知识问答，`needs` 含 `knowledge_base`：新建时版本 1 为空白应用，模板配置进入草稿；选好知识库后保存、发布，详见 [knowledge.md](knowledge.md#编排应用知识检索节点)）、`knowledge-agent`（知识库智能体，`needs` 同 `knowledge-qa`，见 [knowledge.md](knowledge.md#编排应用智能体调用知识库)）。
- `From`：`{"blank": true}`（开始 → LLM → 结束，可以直接运行）、`{"template": "intent-routing"}`、`{"app": "hello", "version": 2}`（复制已有编排应用的某个版本，`version` 不写为当前版本；不能复制代码应用）。
- 归档的应用不再出现在 `GET /apps`，`GET /apps/{app}` 返回 404，`POST /runs` 返回 404；它的运行记录照常可以查看。
- `PUT /apps/{app}/draft` 对编排应用同样只管配置；改名称、简介用 `PATCH`。
- 代码应用不支持 `nodes/{node}/test`、导出和导入，返回 409。

### 单节点试跑

`POST /apps/{app}/nodes/{node}/test` 只跑一个节点，不建运行记录，不进评测。

- `config`：要试的配置，一般是编辑页里还没保存的配置；不传时用草稿，没有草稿用当前版本。只校验被试的这个节点，图的其他问题不影响试跑。
- `values`：这个节点引用的变量的测试值，键是 `"节点.字段"`，如 `{"start.question": "我要退货", "review.comment": ""}`。没给的变量按空值。常量取配置里的值，密钥取已设的值。
- 可以试跑的类型：llm、classifier、condition、tool、knowledge、agent、template、code。start、human、end 返回 422。
- 超时：整个请求最多 120 秒。

```jsonc
// NodeTest
{
  "status": "done" | "error",
  "outputs": {"text": "……"},       // 出错时为 null
  "branch": "after_sales",         // 会走的分支，没有分支时为 null
  "error": null,                   // {"type": "model_error", "message": "……"}；节点自己的 on_error 在试跑时不生效（不重试、不用默认值），只报告错误
  "duration_ms": 1830,
  "rendered": {"prompt": "客户问：我要退货"}   // 引用替换后的提示词、参数等，便于检查；密钥显示为 ******
}
```

### 导出和导入

导出文件（UTF-8 YAML）：

```yaml
format: aep-app/1
title: 意图分类路由
summary: ……
exported_from: {app: support-router, version: 3}
config: {...}             # 编排配置，原样；env.secrets 只有名字
requires:                 # 方便导入时核对
  skills: [{name: after-sales-policy, version: 2}]
  datasources: [{datasource_id: ds_1a2b, name: MySQL 示例库, kind: mysql}]
  secrets: [crm_token]
```

导入时 `yaml` 是文件的文本内容（最多 1 MB）。文件格式不对、`format` 不认识、图本身不合法返回 422。图本身合法但引用的东西在这个平台上缺失时，不阻止导入，`problems` 列出缺什么：

```jsonc
[{"kind": "skill", "name": "after-sales-policy", "version": 2, "message": "没有这个 Skill 版本"},
 {"kind": "datasource", "datasource_id": "ds_1a2b", "name": "MySQL 示例库", "message": "没有这个数据源，请在节点里重新选择"},
 {"kind": "model", "name": "claude-bedrock", "message": "模型网关上没有这个别名"},
 {"kind": "secret", "name": "crm_token", "message": "密钥没有值，请在应用设置里填写"}]
```

- `POST /apps/import` 新建应用：没有缺失时，导入的配置就是版本 1；有缺失（密钥除外）时，版本 1 是空白应用，导入的配置放在草稿里，修好之后发布。
- `POST /apps/{app}/draft/import` 把文件导入成已有编排应用的草稿，覆盖现有草稿。
- 带缺失的草稿只能由导入产生；之后用 `PUT /apps/{app}/draft` 保存时照常校验。

### 环境变量和密钥

- 常量写在配置的 `env.vars` 里，随版本发布和回滚。
- 密钥的值按应用保存，不随版本变化，不进导出、运行记录和追踪。值最多 4096 字符，读接口只告诉你是否设过（`AppDetail.secrets`）。
- 运行时用到没设值的密钥，这个工具节点按 `tool_error` 出错，`error_message` 写明缺哪个密钥。

## 运行

- `POST /runs` 用于编排应用时，`input` 按开始节点的 `inputs` 校验，不合格返回 422，`loc` 为 `["body", "input", "question"]`。`hello` 的输入仍是 `{"text": "……"}`。
- `POST /runs` 新增可选字段 `draft`（布尔）：为 true 时用当前草稿的配置运行（编辑页的“用草稿试运行”），没有草稿 409，不能和 `app_version` 同时给。草稿运行的 `app_version` 为 null，运行记录新增 `draft: true`（其他运行为 false），运行列表可以据此标注“草稿试运行”。代码应用也可以这样试跑草稿。
- 编排应用的 `result` 就是结束节点的 `outputs`。`hello` 的结果仍是 `{"answer": "……"}`。
- 编排应用的 `messages` 是运行过程，按发生的先后排列。每条有：
  - `role`：`user` 是人说的（提出需求、人工处理），`assistant` 是应用的某一步说的；
  - `content`：`assistant` 的按 Markdown 显示，`user` 的按原样显示；
  - `node`、`node_title`：哪一步说的（节点 id 和这次运行用的标题）；
  - `actor`：谁说的。开始节点的消息是提出人（`POST /runs` 的 `requester`），人工节点的消息是处理人（`resume` 的 `actor`）；没给时为 null，`assistant` 的总是 null；
  - `at`：说这句话的时间（ISO）；人工节点是做出处理的时间。

  这四个字段在之前的运行里没有，接口返回 null；`etl` 代码应用的消息也没有。第一阶段只有数据团队一方在用，消息不标身份；业务方和数据团队分开的视角放到第二阶段。
- 哪些节点说什么。表里的“第 k/n 次”是说接下来是第 k 次、最多 n 次：校验节点算的是连续第几次（n 是 `max_attempts`），dev 运行算的是这一轮第几次运行（每次走 `passed` 或 `failed` 后重新数，n 是 `max_runs`）。

| 节点 | role | content |
| --- | --- | --- |
| start | user | 按开始节点里的顺序，第一个填了字的文本输入（text、textarea，只有空格不算），原样显示；都没填就不说 |
| llm，文本输出 | assistant | 模型的回答；`quiet: true` 时不说 |
| llm，JSON 输出 | — | 不说：产出给后面节点用，在流程视图里看 |
| agent | assistant | 最后的回答 |
| human，确认模式 | user | `选了「确认」`；写了意见时 `选了「驳回」，意见：语气再软一点`。`pending` 里有 `fields`（运行时取到至少一题）、挂了意见或按 `changed_branch` 改过时，一律改用“；”连接，开头后面依次接有的部分：`选了「按我的意见修改方案」；「统计哪段时间？」选了「只看 2026 年」（原建议「源库现有的 12 个月」）；意见：……；另挂了 2 条意见`，只列没按建议答的题，没有的部分不说。按 `changed_branch` 改过时开头写成 `点了「确认方案，开始做」，改了答案或写了意见，按「按我的意见修改方案」处理`；只写了意见时如 `点了「确认」，改了答案或写了意见，按「驳回」处理；意见：……` |
| human，填写模式 | user | 每个字段一句，用“；”连接：`订单号填了「A1001」`；下拉和是否 `统计时段选了「只看 2026 年」`（下拉写选项的名称，是否写“是”或“否”）；没填 `件数没填` |
| etl_profile | assistant | `已探查 示例库 的 2 张表：sales.customers、sales.orders` |
| etl_plan_check 通过 | assistant | `方案整理好了：1 张目标表、2 条假设待确认`（没有假设时只说目标表）；`format` 为 2 时 `方案整理好了：2 张目标表、4 个指标、2 个问题待回答`（没有问题时 `……、4 个指标，没有要回答的问题`）；完整方案在产出 `message` 里 |
| etl_plan_check 不过 | assistant | `方案有 2 处没通过校验，让模型重写（第 2/3 次）`；回答不是 JSON 时 `模型没按要求的 JSON 格式回答，让模型重写（第 2/3 次）`；连续不过到上限时不说，运行失败，`error` 写明问题 |
| databricks_ingest | assistant | `已把 2 张表共 6 行抽取到 Databricks：…`，每条注意事项另起一行 `注意：…`；`reuse` 为 true、表和这次运行上次抽取的一样或是其中一部分时 `要的 2 张表这次运行已经抽取过，沿用（共 6 行）` |
| etl_code_check 通过 | assistant | `代码检查通过，打包成 bundle order_facts（5 个文件）` |
| etl_code_check 不过 | assistant | `代码有 1 处没通过检查，让模型重写（第 2/3 次）`；不是 JSON 时（包括没有 `targets` 对象）同上；到上限时同上 |
| databricks_dev_run | assistant | 通过：`dev 运行通过：写出 1 张表共 96 行，质量规则丢弃 1 行`（cluster 模式说“得到”；有表行数不明时不说行数；没丢弃不说丢弃；没有结果表时只说 `dev 运行通过`）。带着报错重写：`dev 运行失败（1 条报错），让模型改代码（第 2/3 次运行）`。不再重试：`dev 运行失败（1 条报错），不再重试`。没有报错时是 `dev 运行失败，……`。“第 2/3 次运行”数的是这一轮。完整报告在产出 `report` 里，答案的数字不写进这句话 |
| 其他节点（classifier、condition、template、tool、code、knowledge、end），以及出错后走默认值或失败分支 | — | 不说 |

- 运行失败时 `error` 写明节点：`节点「起草售后回复」(draft)：模型没有按格式回答`。
- 运行停在设了 `outcome` 的结束节点时，运行记录的 `outcome` 就是它，其他情况为 null。
- 运行在等人处理时，运行记录的 `step` 就是它等的那个节点，不另加字段。
- 列表和页头的状态文字只改 `done` 一种，按 `outcome` 写：

  | `outcome` | 状态文字 |
  | --- | --- |
  | `delivered` | 已交付 |
  | `stopped` | 已终止 |
  | 其他值 | 结束节点的标题（`step.title`） |
  | null | 已完成（同以前） |

  其他状态的文字照第一批已经做好的不变（`frontend/lib/runs.ts` 的 `statusLabels` 和 `stepText`：“等待确认”“进行到「…」”“停在「…」”“失败于「…」”“取消于「…」”等）。

### 人工节点在工作台上的样子

`pending` 新增两个字段，`kind` 多一种取值：

```jsonc
// 确认模式：和原来一样，多了显示文字
{"kind": "approval", "prompt": "确认这条回复吗？", "options": ["approve", "reject"], "comment_required": ["reject"],
 "option_labels": {"approve": "确认", "reject": "驳回"},     // 新增；etl 和老的运行可能没有，前端用原来的默认文字
 "changed_branch": null,                                       // 第二批新增，见下；没设时为 null
 "details": {"type": "flow_human", "node": "review", "show": "……Markdown……"}}

// 填写模式
{"kind": "form", "prompt": "请提供订单号", "options": ["submit"], "comment_required": [],
 "option_labels": {"submit": "提交"},
 "fields": [{"key": "order_id", "label": "订单号", "type": "text", "required": true}],   // 新增，写法同上
 "details": {"type": "flow_human", "node": "ask_order", "show": ""}}
```

`POST /runs/{id}/resume` 新增可选字段 `values`。填写模式用 `{"decision": "submit", "values": {"order_id": "A1001"}, "actor"?}`；缺必填字段或类型不对返回 422，`loc` 为 `["body", "values", "order_id"]`。不带题的确认模式不看 `values`。

#### 带扩展设置的人工节点（第二批）

带扩展设置的确认模式节点，等待时的 `pending` 是这样（以 `etl-lakehouse` 模板第 2 版的“确认方案”第 1 轮为例）：

```jsonc
{"kind": "approval", "prompt": "请检查方案……",
 "options": ["approve", "revise"], "comment_required": ["revise"],
 "option_labels": {"approve": "确认方案，开始做", "revise": "按我的意见修改方案"},
 "changed_branch": "revise",
 "fields": [{"key": "q1", "label": "统计哪段时间？", "type": "select", "required": true,
             "choices": [{"value": "all", "label": "源库现有的 12 个月"}, {"value": "y2026", "label": "只看 2026 年"}],
             "suggested": "all", "why": "需求没说时间范围", "affects": ["m1", "m2", "m3", "m4"]}],
 "details": {"type": "flow_human", "node": "review", "show": "……Markdown……",
             "view": "etl_plan_review",
             "data": {"brief": {…}, "plan": {…}, "answer": {…}, "uncited": […], "cleaned": […],
                      "returned": null, "previous": null},   // 引用替换后的值；第 1 轮时两个 record 还是 null
             "round": 1,
             "seen": "sha256:3f5a……",
             "dropped_fields": []}}
```

- `options`、`option_labels`、`comment_required` 只列显示的选项：`requires` 不成立的选项三处都去掉。
- `changed_branch`：节点设的值；没设时为 null，以前的运行也是 null。
- `fields`：确认模式运行时取到至少一题时才有，每题 `required` 都是 true，有 `suggested`、`why`、`affects` 时原样带上，没有 `default`；取到 0 题时 `pending` 不带 `fields`。本文说确认模式“带题”“不带题”，都是看 `pending` 里有没有 `fields`。
- 只要节点设了 `view` 或 `data`，或者是确认模式设了 `fields`（不管运行时取到几题、取到的是不是列表），`details` 就有 `view`、`data`、`round`、`seen`、`dropped_fields` 这五项；其他节点的 `details` 仍然只有 `type`、`node`、`show`。填写模式的 `fields` 是表单本身，不算扩展设置：只有 `fields` 的填写模式节点 `details` 不变。
  - “设了”指配置里有这个键、而且值不是 null：`data: {}`、`fields: []` 算设了；`view: null`、`data: null`、`fields: null`、`changed_branch: null` 和没写一样。
  - `round`：这个节点在这次运行里第几次等人处理，从 1 开始。
  - `seen`：这次显示内容的指纹，写成 `"sha256:" + 十六进制`。算法：整个 `pending` 去掉 `details.seen` 和 `details.round`，转成 JSON（键按字母排序、UTF-8 原样输出中文、不加空格），再算 SHA-256。
  - `dropped_fields`：运行时检查没通过、被丢掉的题，`[{"index": 3, "key": "q4", "problem": "建议的答案不在选项里"}]`；`index` 是在题目列表（写的列表或引用到的列表）里的位置，取到的值不是列表时为 null，`key` 认不出时为 null。

提交：

```jsonc
// POST /runs/{id}/resume
{"decision": "approve",
 "comment": "",                                    // 可选
 "actor": "韩梅梅",                                 // 可选，最多 100 个字符
 "values": {"q1": "y2026"},                         // 带题时每题都要答
 "notes": [{"item": "metric:m4", "text": "保留两位小数"}],   // 可选，只有确认模式能挂
 "seen": "sha256:3f5a……"}                          // 可选，页面把 details.seen 原样带回
```

请求体本身不合格（比如 `actor` 超过 100 个字符、`notes` 不是列表）先返回 422。然后按下面的顺序检查，第一个不过就返回：

1. 运行不在等人处理：409（同以前）。
2. 带了 `seen`，`pending` 里也有 `details.seen`，两者不一样：409，`detail` 为“内容已经更新，请刷新后再处理”。没带 `seen`，或者 `pending` 里没有，不比。
3. `decision` 不在 `pending.options` 里（包括因为 `requires` 没显示的选项）：422，`detail` 为“这一步现在不能选「approve」”。
4. `values`：填写模式缺必填字段或类型不对；带题的确认模式每题都要答（没有默认值），答案要合这题的类型和选项。422，`loc` 为 `["body", "values", "q1"]`。`values` 里多出来的键不管。
5. `notes`：最多 50 条；`text` 1–500 个字符，不能只有空格；`item` 的写法见下。422，`loc` 为 `["body", "notes", 0, "item"]` 或 `["body", "notes", 0, "text"]`；超过 50 条，或者填写模式带了非空的 `notes`（null 和 `[]` 当作没带），`loc` 为 `["body", "notes"]`。
6. 算 `changed`（有没有哪题的答案和它的 `suggested` 不一样），再看要不要改成 `changed_branch`：提交的选项不在 `comment_required` 里、也不是 `changed_branch`，而且改了建议答案、写了意见（只有空格不算）或挂了意见，就按 `changed_branch` 处理。比如带着一句意见点“确认方案，开始做”，按“按我的意见修改方案”处理。这一步在服务端做，不只靠页面。
7. 最后的选项在 `comment_required` 里时，要写了意见、挂了意见或改了建议答案，三者有一个，否则 422，`detail` 为“这个决定需要写明意见”。
8. 同时有人处理或取消了：409（同以前）。

成功仍返回 202 和 `{run_id, status}`，页面再读一次运行。

`notes[].item` 写成 `类别:标识`，指向页面上显示的某个条目：
- 类别的写法同节点 id；标识 1–200 个字符，是第一个 `:` 后面的全部。
- 标识要在 `details.data` 里找得到。按 `data` 的键名排序（按字符编码从小到大），逐个往深处找；里层对象的键同样按键名排序，列表按顺序。配置写的顺序不算数：配置存在 Postgres 的 jsonb 列里，键的顺序不保留，按写的顺序找，测试和生产会对上不同的对象。值是人工节点 `record`（同时有 `node`、`round`、`decision` 的对象）的整个跳过，所以不会对上以前某一轮记录里已经不在的条目。按下面的顺序，前一步找到就不再往下：
  1. 找对象：`id`、`key`、`name` 或 `path` 等于标识的对象，取第一个。
  2. 没有对象对得上时，找文字列表里等于标识的一项。
  3. 都没有时，如果标识在第一个 `.` 处能分成 `外层.内层`：按同样的顺序找 `id`、`key` 或 `name` 等于外层的对象，它里面（任意深度）又有一个对象的 `id`、`key` 或 `name` 等于内层，取第一个这样的。

  例如 `metric:m3` 对上 `brief.metrics` 里 `id` 为 `m3` 的指标，而不是 `uncited` 里的 `"m3"`；`column:ads_sales.city` 指方案里 `ads_sales` 这张表的 `city` 列；`file:src/pipeline.py` 指 `data.files` 里的一个文件。
- 平台不核对类别和找到的东西是否对得上，类别只决定 `feedback` 里怎么称呼：`metric` 指标、`dimension` 维度、`filter` 筛选、`rule` 规则、`question` 问题、`column` 列、`quality` 质量规则、`file` 文件，其他叫“条目”。
- 名称 `label` 由平台定：按第 1 步找到的取那个对象的 `label`、`name`、`text`、`title` 里第一个有的，都没有就用标识；按第 2 步找到的就是这一项本身；按第 3 步找到的就是标识本身。最多 60 个字符，超出的截断加“…”。
- 每种 `view` 用哪些类别，见 data-nodes.md 的“两个人工步骤的页面”。

确认模式的 `record`：

```jsonc
{"node": "review", "title": "确认方案", "round": 1,
 "actor": "韩梅梅", "at": "2026-10-30T08:12:03Z",
 "decision": "revise", "decision_label": "按我的意见修改方案",
 "submitted": "approve",          // 提交的选项被改成 changed_branch 时是提交的那个，否则为 null
 "seen": "sha256:3f5a……",         // pending 的 details.seen 原样抄来；没有时为 null
 "comment": "", "changed": true,
 "notes": [{"item": "metric:m4", "label": "客单价", "text": "保留两位小数"}],
 "answers": [{"key": "q1", "label": "统计哪段时间？", "value": "y2026", "value_label": "只看 2026 年",
              "suggested": "all", "suggested_label": "源库现有的 12 个月", "picked_suggested": false}],
 "dropped_fields": []}            // 同 details.dropped_fields；没有时为 []
```

- `answers` 按题的顺序；`value_label`：下拉题写选项名称，是否题写“是”或“否”，其他转成文字原样写。没写 `suggested` 的题，`suggested`、`suggested_label`、`picked_suggested` 都是 null。
- 不带题、没挂意见的节点，`answers` 和 `notes` 都是 `[]`，其他照样记。
- 第一阶段没有登录，`actor` 是页面上自己填的名字，平台不核对。

页面怎么用这些（所有编排应用通用）：
- 名字框叫“你的名字”，浏览器记住；页面要求先填名字再提交，接口本身不强制。
- 每道题都要点选，标“建议”的选项是现在的做法；有 `why` 时显示“为什么问”。
- 有 `changed_branch` 时，只要有一题没按建议答、写了意见或挂了意见，主按钮就换成 `changed_branch` 那个选项，并提示会按意见重新处理；服务端按同样的规则再判断一次。
- 页面认识这个 `view` 时按它的样子显示 `data`，“这里不对”挂在它规定的条目上；不认识时照旧显示 `show`，只能写总体意见。
- 提交时带上 `details.seen`；收到 409“内容已经更新”就重新读运行。

`etl` 代码应用的确认照旧：等待时不带题，也没有 `data`，挂意见会因为找不到条目返回 422，运行结果里的 `approvals` 不变。

### 流程视图

`GET /runs/{id}/flow` 对编排应用：

- `nodes` 是 `__start__`、配置里的节点（按配置顺序）、`__end__`。每个节点新增 `type`（节点类型，代码应用为 null）；`kind` 照旧：llm、classifier、agent 为 `model`，tool、knowledge 为 `tool`，human 为 `human`，condition、template、code 为 `code`，start、end 为 `terminal`。
- `edges` 新增 `label`：分支边是分支的显示文字（类别名、规则名、选项名，`other` 为“都不是”，`else` 为“否则”，`error` 为“出错”），普通边为 null。
- `design.prompt` 是这次实际用的提示词（引用替换前的模板）；`steps[].output` 里的密钥显示为 `******`。
