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
  "max_visits": 3,            // 一次运行里最多跑几次，1–10，不写为 3；超过时运行失败并写明原因
  "on_error": OnError,        // 只有 llm、classifier、tool、agent、code 可以设；不写等于 {"then": "stop"}
  "ui": {"x": 120, "y": 40}   // 留给以后的画布，平台原样保存，不参与运行
}

// OnError
{
  "retry": {"max": 2, "interval_seconds": 1},   // max 0–3，interval_seconds 0–60；不写为不重试
  "then": "stop" | "default" | "branch",         // 重试后仍失败：停止运行 / 用默认值继续 / 走 error 分支
  "default": {"text": "暂时无法回答"}            // then 为 default 时必填，键是这个节点的产出字段
}
```

节点出错并且 `then` 是 `default` 或 `branch` 时，这个节点除了产出（`default` 时是默认值，`branch` 时为空），还有 `error_type` 和 `error_message` 两个变量，后面的节点可以引用。`error_type` 取值：`timeout`、`model_error`（网关或模型报错）、`output_format`（JSON 输出不合格式）、`tool_error`、`code_error`、`sandbox_unavailable`、`step_limit`（Agent 达到步数上限）。

### 变量引用

- 写法 `{{节点标识.字段}}`，例如 `{{start.question}}`、`{{draft.text}}`；`{{env.refund_limit}}` 引用常量；`{{secret.crm_token}}` 引用密钥。
- 前端可以用这个正则找出引用：`\{\{\s*([a-z][a-z0-9_]*)\.([a-z][a-z0-9_]*)\s*\}\}`。
- 文本里的引用按文本插入（对象和数组插入为 JSON）。还没有值的变量插入为空字符串。
- 工具参数和代码节点输入里，如果整个值恰好是一个引用（如 `"{{lookup.result}}"`），传原始值而不是文本。
- `{{secret.*}}` 只能出现在工具节点的 `args` 里，写在别处保存时报错。密钥的值不会出现在运行记录、流程视图、追踪和日志里。
- 只能引用一定已经跑过的节点。例外：循环里可以引用本轮循环后面的节点（如起草节点引用 `{{review.comment}}`），第一次跑时为空。一个节点重复跑时，变量取最近一次的值。

### 节点类型

每种类型的设置字段如下（公共字段不再重复）。“产出”是这个节点写进变量池的字段，“分支”是它的出边要用的 `branch` 名。

**start 开始**：恰好一个。

```jsonc
{"inputs": [{"key": "question", "label": "客户问题", "type": "text" | "textarea" | "number" | "boolean" | "select" | "datasource",
             "required": true, "default": null, "choices": [{"value": "a", "label": "A"}],   // choices 只用于 select
             "max_length": 2000}]}   // 1–20 个；max_length 只用于 text、textarea
```

产出：每个输入字段。`datasource` 类型的值是 `datasource_id`。无分支。

**llm 调用模型**

```jsonc
{"model": "company",                  // 模型别名，null 为平台默认模型
 "skills": [{"name": "after-sales-policy", "version": 2}],
 "system": "你是售后客服……",           // 可选，最多 20000 字符；拼在 Skill 正文后面
 "prompt": "客户问：{{start.question}}", // 必填，最多 20000 字符
 "output": {"kind": "text"}
        |  {"kind": "json", "fields": [{"key": "amount", "type": "string" | "number" | "boolean" | "array" | "object", "description": "退款金额"}]}}
```

产出：`text`；JSON 输出时是每个字段（不再有 `text`）。模型没按格式回答时自动重试一次，仍不对就按 `output_format` 出错处理。无分支（设了失败分支时另有 `error`）。

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
{"datasource": "ds_1a2b",               // 已登记数据源的 datasource_id
 "tool": "run_query",                   // 该数据源 GET /datasources/{id}/tools 里的工具名
 "args": {"sql": "select count(*) from orders where id = '{{ask_order.order_id}}'"}}
```

产出：`result`（工具返回的内容；是 JSON 就解析成对象）。这一期只能调已登记数据源的工具，数据源连接器都是只读的。

**agent 智能体**：有步数上限的“模型 → 工具 → 模型”循环。

```jsonc
{"model": null,
 "skills": [{"name": "sales-glossary", "version": 1}],   // 按需加载：提示词里只放名称和说明，模型要用时再读正文
 "task": "根据数据回答：{{start.question}}",               // 必填，最多 20000 字符
 "tools": [{"datasource": "ds_1a2b", "tools": null}],     // tools 为 null 表示这个数据源的全部工具，否则列工具名
 "max_steps": 8}                                           // 1–20，一次工具调用算一步
```

产出：`text`（最终回答）、`steps`（`[{"tool", "datasource", "args", "error"}]`，按调用顺序）。

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

产出：确认模式是 `decision`、`comment`、`actor`；填写模式是每个字段和 `actor`。分支：确认模式每个选项的 key；填写模式无分支。

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

**end 结束**：至少一个，没有出边。

```jsonc
{"outputs": [{"key": "reply", "from": ["{{draft.text}}", "{{answer.text}}", "{{handoff.text}}"]}]}   // 1–20 个
```

`from` 依次取第一个有值的引用，所以几个分支可以汇到同一个结束节点。运行到结束节点时，`outputs` 组成运行的 `result`，例如 `{"reply": "……"}`。

### 保存草稿时的检查

`PUT /apps/{app}/draft` 校验不过返回 422，`loc` 指到具体位置。节点用 id 而不是下标定位，方便前端把错误显示在对应节点的对应设置下面：

| 问题 | loc 示例 |
| --- | --- |
| 节点设置不合法、引用了不存在的变量、引用了可能没跑过的节点 | `["body", "config", "graph", "nodes", "draft", "prompt"]` |
| 列表里的某一项 | `["body", "config", "graph", "nodes", "start", "inputs", 0, "key"]` |
| 某个分支没连、连了不存在的分支 | `["body", "config", "graph", "nodes", "route", "branches", "product"]` |
| 出边指向不存在的节点 | `["body", "config", "graph", "edges", 3, "to"]` |
| 节点从开始到不了、到不了结束、循环没有经过人工 / 条件 / 分类节点 | `["body", "config", "graph", "nodes", "answer"]` |
| 整张图的问题（没有开始节点、超过 50 个节点） | `["body", "config", "graph"]` |
| 常量 | `["body", "config", "env", "vars", 0, "key"]` |

检查的完整清单：恰好一个开始节点，至少一个结束节点；节点 id 不重复；每个节点的每个分支恰好一条出边、没有多余的出边；从开始能到每个节点，每个节点能到某个结束节点；每个循环至少经过一个人工、条件或分类节点；变量引用的节点和字段存在且一定已经跑过（循环例外见上）；模型别名在网关上、Skill 版本已发布、数据源已登记、工具在该数据源上；`{{secret.*}}` 只在工具参数里且已在 `env.secrets` 里声明；最多 50 个节点、200 条边、50 个常量、20 个密钥。

运行时还有总步数上限：一次运行最多执行 200 个节点。

## 节点类型清单

`GET /node-types` 给“添加节点”和设置表单用。字段的完整写法以本文为准，这个接口提供显示用的文字、新节点的默认设置和预设。

```jsonc
[{
  "type": "llm",
  "label": "调用模型",
  "description": "调一次模型，产出一段文字或几个字段",
  "category": "model" | "logic" | "tool" | "human" | "terminal",
  "error_handling": true,                 // 能否设 on_error
  "outputs": [{"key": "text", "description": "模型的回答"}],   // 固定产出；随设置变化的（JSON 字段、表单字段、代码返回值）不在这里
  "defaults": {"model": null, "skills": [], "prompt": "", "output": {"kind": "text"}},   // 新建节点时用，不含公共字段
  "presets": [{"key": "extract", "label": "抽取字段", "description": "从文本里抽取结构化字段",
               "settings": {"output": {"kind": "json", "fields": []}, "prompt": "从下面的文本中抽取字段：\n{{start.question}}"}}],
  "max_count": null                        // 一个应用里最多几个，start 为 1
}]
```

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
- 模板有 3 个：`qa-approval`（问答加确认，和 hello 一样）、`intent-routing`（意图分类路由）、`datasource-qa`（数据源问答，Agent 调只读数据源工具，`needs` 含 `datasource`，从模板新建后要在节点里选数据源才能运行）。
- `From`：`{"blank": true}`（开始 → LLM → 结束，可以直接运行）、`{"template": "intent-routing"}`、`{"app": "hello", "version": 2}`（复制已有编排应用的某个版本，`version` 不写为当前版本；不能复制代码应用）。
- 归档的应用不再出现在 `GET /apps`，`GET /apps/{app}` 返回 404，`POST /runs` 返回 404；它的运行记录照常可以查看。
- `PUT /apps/{app}/draft` 对编排应用同样只管配置；改名称、简介用 `PATCH`。
- 代码应用不支持 `nodes/{node}/test`、导出和导入，返回 409。

### 单节点试跑

`POST /apps/{app}/nodes/{node}/test` 只跑一个节点，不建运行记录，不进评测。

- `config`：要试的配置，一般是编辑页里还没保存的配置；不传时用草稿，没有草稿用当前版本。只校验被试的这个节点，图的其他问题不影响试跑。
- `values`：这个节点引用的变量的测试值，键是 `"节点.字段"`，如 `{"start.question": "我要退货", "review.comment": ""}`。没给的变量按空值。常量取配置里的值，密钥取已设的值。
- 可以试跑的类型：llm、classifier、condition、tool、agent、template、code。start、human、end 返回 422。
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
- 编排应用的 `messages`：开头是输入里的文本字段（user），每个 llm、agent 节点的回答记为 assistant，每次人工处理记为 user。
- 运行失败时 `error` 写明节点：`节点「起草售后回复」(draft)：模型没有按格式回答`。

### 人工节点在工作台上的样子

`pending` 新增两个字段，`kind` 多一种取值：

```jsonc
// 确认模式：和原来一样，多了显示文字
{"kind": "approval", "prompt": "确认这条回复吗？", "options": ["approve", "reject"], "comment_required": ["reject"],
 "option_labels": {"approve": "确认", "reject": "驳回"},     // 新增；etl 和老的运行可能没有，前端用原来的默认文字
 "details": {"type": "flow_human", "node": "review", "show": "……Markdown……"}}

// 填写模式
{"kind": "form", "prompt": "请提供订单号", "options": ["submit"], "comment_required": [],
 "option_labels": {"submit": "提交"},
 "fields": [{"key": "order_id", "label": "订单号", "type": "text", "required": true}],   // 新增，写法同上
 "details": {"type": "flow_human", "node": "ask_order", "show": ""}}
```

`POST /runs/{id}/resume` 新增可选字段 `values`。填写模式用 `{"decision": "submit", "values": {"order_id": "A1001"}, "actor"?}`；缺必填字段或类型不对返回 422，`loc` 为 `["body", "values", "order_id"]`。确认模式不用 `values`。

### 流程视图

`GET /runs/{id}/flow` 对编排应用：

- `nodes` 是 `__start__`、配置里的节点（按配置顺序）、`__end__`。每个节点新增 `type`（节点类型，代码应用为 null）；`kind` 照旧：llm、classifier、agent 为 `model`，tool 为 `tool`，human 为 `human`，condition、template、code 为 `code`，start、end 为 `terminal`。
- `edges` 新增 `label`：分支边是分支的显示文字（类别名、规则名、选项名，`other` 为“都不是”，`else` 为“否则”，`error` 为“出错”），普通边为 null。
- `design.prompt` 是这次实际用的提示词（引用替换前的模板）；`steps[].output` 里的密钥显示为 `******`。
