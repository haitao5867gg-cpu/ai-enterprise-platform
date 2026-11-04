# 会话（多轮对话）

后端和前端按这份约定并行开发。字段名 snake_case，时间是 ISO 8601 字符串。错误格式同前：`{"detail": "中文说明"}`，校验错误为 422，`detail` 是 `[{"loc": [...], "msg": "..."}]`。设计说明见[智能体应用设计](https://claude.ai/artifact/Tmg82JosEMpqqfwC5YmzSq)。

落在哪一层：**平台能力**（运行时和接口）。智能体应用（[agent-apps.md](agent-apps.md)）总能对话；编排应用在配置里打开 `conversation` 后也能（[flow-apps.md](flow-apps.md#对话)）。应用不用写代码。

## 概念

- **会话**是同一个应用的一串运行。用户每发一条消息，平台就发起这个应用的一次普通运行（一轮），并把同一会话前几轮的问答带给模型。
- 不做「一个一直不结束的运行」：每轮各是一次运行，运行列表、运行页、重试、取消、评测、用量统计、预算、追踪都照旧按运行算。
- 一个会话同一时间只有一轮在进行：上一轮还在跑或在等人处理时，不能发下一条。
- 每轮用应用当时的生效版本（草稿会话用当时的草稿），记在那次运行上；会话中途发布了新版本，下一轮就用新版本。

## 数据结构

```jsonc
// Conversation
{
  "conversation_id": "conv_…",
  "app": "expense-helper",
  "app_title": "报销政策助手",
  "title": "出差住宿标准是多少？",     // 默认取第一条消息的前 30 个字，可改，1–80 字符；还没有消息也没起名时为「新对话」
  "draft": false,                     // true：调试对话，每轮用草稿配置（应用页「设计」右侧）
  "input": {"datasource": "ds_1a2b"}, // 开会话时给的其他输入，见「开会话」；没有时为 {}
  "created_by": "王搭建" | null,      // 同运行的 requester
  "created_at": "…",
  "updated_at": "…",                  // 最近一轮的开始时间；没有轮次时等于 created_at
  "expires_at": "…" | null,           // updated_at 加保留天数；不自动删时为 null
  "turn_count": 3,
  "busy": false                       // 最后一轮在跑或在等人处理
}

// Turn：GET /conversations/{id} 多一个 turns，列出各轮，旧的在前
{
  "turn": 1,                          // 从 1 开始
  "run_id": "…",
  "message": "出差住宿标准是多少？",
  "reply": "按城市分三档……" | null,   // 这一轮的回答，没结束或没有回答时为 null；见「回答」
  "status": "running" | "waiting" | "done" | "error" | "cancelled",   // 这次运行的 status
  "pending": {...} | null,            // 等人处理时同 GET /runs/{id} 的 pending
  "sources": [...],                   // 同 GET /runs/{id} 的 sources
  "app_version": 3 | null,
  "created_at": "…",
  "finished_at": "…" | null
}
```

运行记录（`GET /runs` 和 `GET /runs/{id}`）新增 `conversation`：`{"conversation_id", "title", "turn"}`，不在会话里的运行为 null。会话被删除后它的运行这一项也为 null，另一项新增的 `cleared` 为 true（见「保存多久」）。

## 接口

| 方法 | 路径 | 请求体 | 返回 |
| --- | --- | --- | --- |
| POST | `/conversations` | `{"app", "draft"?: false, "input"?: {}, "title"?, "requester"?, "project"?}` | 201 `Conversation` |
| GET | `/conversations?app=&draft=false&limit=50` | | `Conversation[]`，按 `updated_at` 新的在前；`limit` 最多 200 |
| GET | `/conversations/{id}` | | `Conversation` 加 `turns: [Turn]` |
| POST | `/conversations/{id}/messages` | `{"message", "requester"?}` | 201，这一轮的运行（同 `POST /runs` 的返回）加 `turn` |
| PATCH | `/conversations/{id}` | `{"title"}` | `Conversation` |
| DELETE | `/conversations/{id}` | | 204，立即删除，效果同到期删除 |

- 谁能看到：会话只有发起人（`created_by` 是自己）和管理员看得到、能发消息。别人的会话按不存在处理，返回 404「没有找到这个会话」。没开登录时所有人算同一个人（auth.md）。列表只列自己能看到的；管理员也只列自己的，按 id 打开别人的。
- 开会话：
  - 应用不存在或已归档 404；应用不能对话（代码应用，或没开 `conversation` 的编排应用）409「这个应用没有开对话」；`draft` 为 true 而应用没有草稿 409「没有草稿可以试」；应用关了「使用页」入口而自己不是搭建者或管理员，403「这个应用没有开放给使用者」（app-access.md“入口”）。草稿会话只有搭建者和管理员能开。
  - `input` 是开始节点里除 `message` 以外的输入，整个会话不变，每轮和 `message` 一起作为运行的输入。按开始节点校验，`loc` 为 `["body", "input", key]`；必填又没默认值的要在这里给。智能体应用没有其他输入，`input` 只能是 `{}`。
  - `project` 同 `POST /runs`（projects.md），开始节点设了 `project` 的应用在这里选，每轮都绑定它（每轮按项目当时的设置重新取快照）。
  - 开会话不发起运行，`turn_count` 为 0。
- 发消息：
  - `message` 1–8000 字符（去掉首尾空白后不能为空），否则 422。
  - 会话 `busy` 时 409「上一条还没处理完」。等人处理的那一轮在运行页或会话里处理完（`POST /runs/{id}/resume`）后才能发下一条；也可以取消它（`POST /runs/{id}/cancel`）。
  - 这一轮的运行和 `POST /runs` 一样检查和发起（版本、外发策略、预算、项目），失败时的报错也一样；运行记录的 `title` 是这条消息。
  - 草稿会话每轮都用当时的草稿运行（同 `POST /runs` 的 `draft: true`）；草稿被删掉后发消息 409「没有草稿可以试」。
- 运行的重试和取消照旧在运行上做：重试接着这一轮跑，不算新的一轮；「复制并重新发起」（`rerun`）发起的是一次不在会话里的运行。会话里同时只有一轮在跑或在等人：只有会话的最后一轮能重试，后面已经有轮次时 409「会话里已经有后面的消息，这一轮不能重试」；重试和发消息按会话依次处理（同一时刻只有一个能成功），所以不会因为同时重试和发消息而有两轮在跑。

## 回答

每轮的 `reply` 是这次运行结果里的一个文字字段：智能体应用是 `result.reply`；编排应用是配置 `conversation.reply` 指定的结束节点输出。字段不是文字（或为空）时 `reply` 为 null，这一轮在历史里只带用户消息。

## 历史怎么带

一轮开始时，平台从同一会话里取历史，按时间顺序放进这次运行，给模型用：

- 只取已经 `done` 的轮次，每轮只带用户消息和 `reply`，不带工具调用和中间结果。失败、取消、还在跑的轮次不带。
- 最多带最近 `history_turns` 轮（智能体应用在 `agent.history_turns`，编排应用在 `conversation.history_turns`，默认 10，0–50）。总字数（消息加回答）超过 20000 字时，从最早的一轮开始整轮丢掉，直到不超过。
- **外发策略**：历史和节点自己的提示词一样，只发给这次运行的外发策略允许的模型。另外：
  - 数据样本按「脱敏」（`masked`）外发时，模型写的回答里的标记是平台还原成原值后才给人看的。带进历史的是**还原之前**的样子（再按那一轮的还原表重新脱敏），模型在下一轮看到的不会比它当时看到的多。会话里的标记按会话算（用 `conversation_id` 而不是 `run_id` 生成），前几轮的还原表随历史带进新一轮，所以模型在新一轮里写回前几轮的标记时也能还原。
  - 某一轮运行时的样本级别比现在宽（比如那时是「完整」，现在是「脱敏」），这一轮不进历史，免得把那时的原值带给现在的模型。级别从宽到严是：完整、脱敏、只发统计、只发结构。
- 模型看到的形式：智能体按对话消息给（每轮一条用户消息、一条助手消息，放在系统提示词之后、这一轮的消息之前）；编排应用见 flow-apps.md「对话」。
- 运行页的流程视图里，每一步的 `input` 多一个 `conversation`：`{"id", "turn", "history"}`，`history` 是这一轮实际带给模型的历史（已按上面的规则处理过）。

## 保存多久

- 保留天数是平台配置 `AEP_CONVERSATION_RETENTION_DAYS`，默认 30；0 表示不自动删。
- 会话最后一轮的开始时间（`updated_at`）超过保留天数后，平台每天清理一次：会话记录整条删除；它的各轮运行留下状态、版本、耗时和用量（运营看板、预算照算），但输入、结果、消息、待处理内容和报错原文清空，运行记录的 `cleared` 为 true，运行页显示「这次运行属于已过期的会话，内容已清除」。还在跑或在等人处理的那一轮先取消。
- 已经存成评测用例的那一份（用例里的输入、历史和参考答案）不受影响。
- 删除和清理都记进操作记录（auth.md）：删除记「删除会话」，清理记一条「清理过期会话」，`target` 里写清掉的会话数。

## 应用 API 和 MCP

见 [app-access.md](app-access.md#会话)：`/v1/apps/{app}/runs` 和 MCP 的运行工具多两个可选字段 `new_conversation`（开新会话）和 `conversation_id`（接着发），另有 `GET /v1/conversations/{id}`。用 API 开的会话只有开它的那个密钥能接着用，平台里只有管理员能按 id 打开看。

## 追踪

每轮仍是一条 trace（id 由 run_id 推出）。会话里的运行，trace 的 `session_id` 设为 `conversation_id`，在 Langfuse 里可以按会话看。

## 存储

新表 `conversations`（带 `tenant_id`）：上面 `Conversation` 的字段（`turn_count`、`busy`、`expires_at` 读的时候算），另记会话属于谁（登录的账号，或开它的 API 密钥）和开会话时选的项目。`runs` 加 `conversation_id`、`conversation_turn`（同一会话的轮次唯一，两条消息同时发只有一条成功）和 `cleared_at`（内容随会话清掉的时间）。新表 `conversation_replies`（带 `tenant_id`）按运行记每轮的回答：给人看的、模型写的（还原之前）、还原表和那一轮的样本级别，历史从这里取。

## 测试

- 3 轮对话：第 3 轮发给模型的请求里有前 2 轮的消息和回答，没有工具结果。
- 上一轮在等人处理时发消息 409；处理完后能发。
- `history_turns` 和 20000 字上限：超出时丢最早的整轮。
- 脱敏策略下：历史里的回答是标记不是原值；第 2 轮模型写回第 1 轮的标记能还原。第 1 轮按「完整」、第 2 轮按「脱敏」时，第 1 轮不进历史。
- 别人的会话 404；没开对话的编排应用 409。
- 清理：过期会话被删，运行内容清空但用量还在；存成用例的不受影响。
- 不在会话里的运行，行为和接口都不变。
