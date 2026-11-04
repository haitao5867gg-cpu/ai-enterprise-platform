# 智能体应用接口

后端和前端按这份约定并行开发。本文只写在 [apps-and-skills.md](apps-and-skills.md)、[flow-apps.md](flow-apps.md) 之上新增和改变的部分；没提到的接口和字段保持不变。字段名 snake_case，错误格式同前。设计说明见[智能体应用设计](https://claude.ai/artifact/Tmg82JosEMpqqfwC5YmzSq)，Copilot Studio 的调研见[对照文档](https://claude.ai/artifact/4qXSiBmzRdFzyvarkK2Y4g)。

落在哪一层：**平台应用类型**。智能体应用是第三种应用（`kind: "agent"`），和编排应用、代码应用并列，登记在 `apps` 表里。它不另起运行时：平台把它的配置生成为一张固定的「开始 → 智能体 → 结束」编排图，交给 `aep.runtime.flow` 运行，所以版本、评测门槛、追踪、预算、外发策略、运行页都照旧。智能体节点新增的能力（指令、历史、只依据资料回答、调用应用）都是节点能力，编排应用也能用（[flow-apps.md](flow-apps.md#节点类型)「智能体节点的新设置」）。

## 概念

- **智能体应用**：一张表单配好的一个智能体。没有画流程这一步，能多轮对话（[conversations.md](conversations.md)），也能被一次性调用（`POST /runs`、应用 API、MCP、调用应用节点、别的智能体）。
- 一次运行就是一轮：输入一条消息，智能体按需调工具、查知识库、调别的应用，给出一个回答。
- 需要加审批、分支、固定步骤时，用「转成编排应用」复制出一个等价的编排应用继续改。

## 配置

```jsonc
// AgentAppConfig：智能体应用的 AppConfig
{
  "schema_version": 1,
  "env": {"vars": [EnvVar], "secrets": ["crm_token"]},   // 同编排应用
  "agent": {
    "instructions": "你是公司的报销政策助手……",   // 必填，1–20000 字符；角色、做事规则。可以引用 {{env.x}}，不能引用密钥
    "model": null,                              // 模型别名，null 用平台默认对话模型
    "skills": [{"name": "expense-policy", "version": 2}],   // 0–10 个，按需加载，同智能体节点
    "tools": [{"datasource": "ds_1a2b", "tools": null}],    // 0–10 个，写法同智能体节点，但 datasource 只能写数据源 id，不能写引用
    "knowledge": null,                          // 可选，写法同智能体节点的 knowledge（knowledge.md）
    "apps": [AppTool],                          // 0–10 个，见「调用应用」
    "grounded_only": true,                      // 只依据知识库、工具和被调应用的结果回答，见下；默认 true
    "max_steps": 8,                             // 每轮最多几步（一次工具调用、一次检索、一次调用应用各算一步），1–20
    "history_turns": 10                         // 每轮带给模型的历史轮数，0–50；0 等于不记上下文
  },
  "chat": {
    "opening": "你好，我可以回答报销相关的问题。",   // 可选，开场白，最多 500 字符；只在使用页和调试对话里显示，不进历史
    "suggestions": ["出差住宿标准是多少？"]          // 0–5 个推荐问题，每个 1–100 字符
  }
}

// AppTool：把一个已发布的应用当成工具
{"app": "order-lookup",          // 被调用的应用：编排应用或智能体应用，不能是代码应用、不能是自己
 "when": "用户问订单状态时",       // 必填，1–500 字符；模型靠它决定调不调
 "enabled": true}                 // false：配置保留，但不给模型
```

保存草稿时的检查（`loc` 指到出错的字段，比如 `["body", "config", "agent", "apps", 0, "when"]`）：

- 模型别名在网关上存在、是对话用途，并且平台默认外发策略允许（同智能体节点；运行时还按这次运行的策略再查一次）。
- Skill 的版本存在；数据源存在，`tools` 列的工具名在保存时能连上数据源就核对，连不上不阻止保存（运行时以 `tool_error` 报）；工具服务（`kind` 为 `mcp`）和只读数据源都可以，代码仓库（`git`）不行。
- 知识库存在，规则同智能体节点。
- `apps`：应用存在且已发布；不是代码应用；不是自己；`app` 不重复；对方的「允许被其他应用调用」开着（app-access.md「入口」）；对方的生效版本里没有人工节点（智能体等不了人）；智能体应用之外的被调应用，开始节点的必填输入都能由模型填（类型是 text、textarea、number、boolean、select；有 `datasource` 类型必填输入的不行）。
- `tools`、`knowledge`、`apps` 可以都为空：那就是只有指令和 Skill 的对话助手。

## 运行

### 输入和结果

- 输入：`{"message": "……"}`，1–8000 字符。`POST /runs`、`/v1`、MCP、调用应用节点、别的智能体调用它都用这个形状。
- 结果（`result`）：

```jsonc
{
  "reply": "按城市分三档……",       // 给人看的回答
  "steps": [...],                  // 调用过的工具，同智能体节点的 steps；调用应用的一步是 {"tool": "call_app", "datasource": null, "app": "order-lookup", "run_id": "…", "args": {...}, "error": null}
  "hits": [...],                   // 知识库命中的片段，同智能体节点
  "grounded": true                 // 只依据资料回答的检查是否通过；grounded_only 不生效时总是 true
}
```

### 生成的图

```
start（inputs: message，textarea，必填）
  → agent（智能体节点：instructions、model、skills、tools、knowledge、apps、grounded_only、max_steps 取自配置；
           task 为 "{{start.message}}"；history 为 true）
  → end（outputs: reply ← {{agent.text}}，steps ← {{agent.steps}}，hits ← {{agent.hits}}，grounded ← {{agent.grounded}}）
```

节点 id 固定为 `start`、`agent`、`end`，标题为「收到消息」「智能体」「回答」。这张图只在平台内部存在，不进配置；`GET /apps/{app}` 的 `nodes`、`edges` 和运行页的流程视图按它显示。`agent.history_turns` 生成到图的 `conversation.history_turns`。

### 模型看到什么

- 系统提示词：平台固定的工具协议（有工具时）+ `instructions` + 工具列表 + Skill 列表 + 规则（有知识库时要按 `[n]` 标出处；`grounded_only` 生效时只能依据工具和资料回答，查不到就直说）。
- 然后是历史（conversations.md「历史怎么带」），最后是这一轮的消息。
- `tools`、`knowledge`、`apps` 都为空（或 `apps` 全部停用）时，不走工具协议，直接按普通对话调一次模型，回答就是 `reply`。

### 只依据资料回答

`grounded_only` 为 true，并且配了至少一个工具、知识库或启用的被调应用时生效。一轮结束时平台检查：

- 这一轮里有一次**成功**的数据源或工具服务调用、或一次成功的应用调用；或者
- 回答里引用了这一轮知识库命中的至少一个编号 `[n]`（判断规则同 knowledge.md「运行记录里的出处」的 `cited`）。

两条都不满足时，平台把回答换成固定的「没有在已接入的资料和数据里找到依据，暂时答不了这个问题。」，`result.grounded` 为 false。模型原来的回答留在这一步的产出 `ungrounded_text` 里，搭建者在运行页的流程视图能看到，不进 `result`、不进历史。读 Skill 不算依据。

这一项借鉴 Copilot Studio 的「Allow ungrounded responses」开关，但默认相反：它默认允许不依据资料回答，我们默认不允许。

### 调用应用

`apps` 里每个启用的应用是一个工具，代号 `a1`、`a2`…（数据源工具是 `t1`…，知识库是 `kb`，不会撞）：

- 给模型的说明：应用标题、简介和 `when`；参数是对方开始节点的输入（智能体应用是 `{"message": "要问它的话"}`），类型映射同 MCP（app-access.md）。
- 调一次就发起对方的一次运行，规则同调用应用节点（flow-apps.md「app 调用应用」）：对方的生效版本、对方自己的外发策略、提出人沿用、项目沿用；被调运行的 `trigger` 为 `{"kind": "app", "run_id", "node", "title"}`；嵌套最多 3 层，调用链上已有的应用不能再调；等它结束最多 300 秒。
- 给模型的结果：对方是智能体应用时是它的 `reply`；是编排应用时是它的整个 `result`（JSON，截到 8000 字符）。对方失败、超时、停下来等人时，告诉模型出错了和原因（这一步记 `error`），不让整个运行失败，和数据源工具出错一样；停下来等人的那次运行由平台取消。
- 被调的一方不带历史，需要的上下文由模型写进参数。
- 运行时对方已经关了「允许被其他应用调用」、已归档或没有生效版本时，这个工具不给模型，`steps` 里不记；运行页的流程视图在这一步的 `design.tools` 里标「已停用」。

## 新建、模板、导出导入

- `GET /app-templates` 每项加 `kind`（`flow` | `agent`）。智能体模板 3 个：
  - `agent-blank` 通用助手：只有指令，没有工具。
  - `agent-knowledge` 知识库助手：`needs` 含 `knowledge_base`，`grounded_only` 为 true，指令要求先检索再回答、标出处。和 `knowledge-qa` 一样走「版本 1 空白、模板配置进草稿」。
  - `agent-data` 数据问答助手：`needs` 含 `datasource`，指令要求先看表结构再查询、只用查到的数字回答；和上面一样版本 1 空白、模板配置进草稿，选好数据源后发布。
- `POST /apps` 的 `From` 新增 `{"blank_agent": true}`：版本 1 是 `agent-blank`，可以直接对话。用模板新建时，应用的 `kind` 跟着模板走。复制（`{"app": "...", "version"}`）可以复制智能体应用，`kind` 不变。
- 智能体应用支持导出和导入，文件格式同编排应用（`format: aep-app/1`），多一个顶层 `kind: agent`；没有 `kind` 的旧文件按编排应用。`requires` 同样列出 Skill、数据源、知识库、密钥，另加 `apps`（被调用的应用标识）。导入时缺被调应用也只列进 `problems`，不阻止导入。
- 设计页的「代码」视图读写的就是导出文件里的 `config` 部分（YAML），保存时走 `PUT /apps/{app}/draft` 的同样校验。编排应用也能用这个视图。

## 转成编排应用

`POST /apps/{app}/convert-to-flow`，请求体 `{"app": "新应用标识", "title"?, "summary"?, "version"?, "actor"?}` → 201 `AppDetail`（新的编排应用）。

- 把智能体应用的某个版本（不写为当前版本）复制成一个新的编排应用，版本 1 的图就是上面「生成的图」，配置里 `conversation` 已打开（`reply` 为 `reply`），`env` 原样复制；密钥和复制应用时一样只带名字，值要在新应用里重新设。评测集一起复制。原应用不变。
- 新应用和原应用用同样的输入跑，结果一样（同一个模型、同样的提示词和历史）。
- 反过来（编排应用转智能体应用）不支持。代码应用和编排应用调这个接口 409「只有智能体应用能转成编排应用」；新标识重名 409。

## 应用接口的变化

- `AppSummary.kind` 和 `AppDetail.kind` 多一个值 `agent`。
- `AppDetail` 对智能体应用：`inputs` 是 `[{"key": "message", "label": "消息", "type": "textarea", "required": true}]`；`nodes`、`edges` 按生成的图；`chat` 是当前版本配置里的 `chat`（使用页用）；`options` 同编排应用，另加 `apps`：可以放进 `apps` 的应用列表 `[{"app", "title", "kind", "callable": true, "reason": null}]`（`callable` 为 false 时 `reason` 写明原因，比如「有人工节点」「没有开放给其他应用调用」）。
- `PATCH`、`DELETE`（归档）、版本、草稿、评测、发布、回滚、密钥、API 密钥、定时运行都和编排应用一样。
- 单节点试跑：智能体应用只能试 `agent` 这一个节点，`values` 里给 `start.message`。

## 页面（第 4 批）

- 新建应用先选类型：「智能体」（问答、助手、查资料查数据）或「编排」（步骤固定、要审批的业务流程），再选空白或模板。只有名称必填。
- 应用页四个标签不变（概览 · 编排 · 评测与发布 · 接入），智能体应用的「编排」标签叫「设计」：左边是配置表单（角色和指令 · 模型 · 知识和工具 · 对话 · 高级），一个设置只在一处；上方「表单 / 代码」切换；右边是调试对话（草稿会话，conversations.md），每条回答能展开看调了哪些工具、引用了哪些资料，并有「存为评测用例」（evals.md）。
- 「接入」标签和「概览」标签的变化见 app-access.md「入口」和 ops.md「应用的使用情况」，所有应用都有。
- 使用页 `/use/{app}`：智能体应用和开了对话的编排应用显示聊天界面（会话列表、新对话、开场白和推荐问题、每条回答的「查看过程」和点赞点踩）。

## 测试

- 用模板新建三种智能体应用；草稿试跑、评测、发布、回滚都通。
- 没有工具的智能体直接对话；有工具时按工具协议跑，`steps` 正确。
- `grounded_only`：没有调成功工具、也没引用编号时回答被换掉，`grounded` 为 false，`ungrounded_text` 留在步骤产出里；只读了 Skill 也被换掉；调成功一次工具就不换。不生效的情况（没配任何工具）总是 true。
- `apps`：主智能体按 `when` 调两个子智能体；停用的不给模型；对方关了被调用、有人工节点、是代码应用、是自己时保存报错；超过 3 层、调用链重复时告诉模型出错。
- 转成编排应用：同一输入（mock 模型）结果一致。
- 导出再导入结果一致；没有 `kind` 的旧文件仍按编排应用导入。
