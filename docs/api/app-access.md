# 应用 API、API 密钥和 MCP 接口

后端和前端按这份约定并行开发。字段名 snake_case，时间是 ISO 8601 字符串。错误格式同 [datasources.md](datasources.md)：`{"detail": "中文说明"}`。

落在哪一层：**平台能力**。每个应用（编排应用、智能体应用和代码应用）都自动有 API 和 MCP 入口，应用不用写任何代码；搭建者只要给应用建一个 API 密钥。每个入口能单独关掉，见“入口”。

## API 密钥

- 一个密钥只属于一个应用，只能调这个应用。要让别的系统调两个应用，就建两个密钥。
- 密钥长这样：`aep_` 加 43 个字符。只在新建时显示一次，平台只存它的 SHA-256，丢了只能删掉重建。
- 搭建者（和管理员）在应用页的「发布」里新建、删除；列表里只显示前 12 个字符。
- 用密钥发起的运行，发起人写「API：密钥名称」，在运行记录里和别的运行一样能看到、能处理确认。
- 不管 `AEP_AUTH_ENABLED` 开没开，`/v1` 和 `/mcp` 都只认密钥，不认登录 Cookie。

```jsonc
// ApiKey
{
  "key_id": "key_…",
  "app": "retail-etl",
  "name": "CRM 系统",              // 1–50 字符
  "prefix": "aep_k3X9aB2c",         // 前 12 个字符
  "created_by": "王搭建" | null,
  "created_at": "…",
  "last_used_at": "…" | null
}
// 新建时多一个字段 "secret": "aep_…"（完整密钥，只返回这一次）
```

接口（搭建者）：

- `GET /apps/{app}/keys` → `[ApiKey]`，新的在前
- `POST /apps/{app}/keys`，请求体 `{"name": "CRM 系统"}` → 201 `ApiKey` + `secret`。一个应用最多 20 个密钥，再建返回 409「一个应用最多 20 个密钥」。
- `DELETE /apps/{app}/keys/{key_id}` → 204。删掉后用这个密钥的请求立刻返回 401。

不存在的应用返回 404「没有应用 xxx」，不存在的密钥返回 404「没有找到这个密钥」。

## 应用 API（`/v1`）

请求头带 `Authorization: Bearer aep_…`。缺少或无效返回 401「缺少或无效的 API 密钥」；密钥不属于路径里的应用返回 403「这个密钥不能调用应用 xxx」。

```jsonc
// RunResult
{
  "run_id": "…",
  "app": "retail-etl",
  "status": "running" | "waiting" | "done" | "error" | "cancelled",
  "result": {…} | null,          // 完成时应用的结果，同 RunDetail.result
  "pending": {                   // 等人处理时：
    "kind": "form" | "approval" | …,   // form 可以用 answer 接口填；其他要审批者在平台上处理
    "prompt": "…",
    "fields": [Field] | null      // 表单字段，同 flow-apps.md 人工节点
  } | null,
  "error": "…" | null,
  "link": "https://…/runs/…",    // 平台里这次运行的页面（AEP_WEB_URL）
  "conversation_id": "conv_…" | null,   // 智能体应用第 1 批新增：这次运行所在的会话，见“会话”；不在会话里为 null
  "turn": 2 | null,                     // 是会话的第几轮
  "created_at": "…",
  "updated_at": "…"
}
```

- `POST /v1/apps/{app}/runs`，请求体 `{"input": {…}, "wait": 30, "project": {…}}` → 201 `RunResult`。
  - `input` 同 `POST /runs`：编排应用按开始节点的 `inputs` 校验，代码应用 `etl` 是 `{"request": "…"}`。不合格返回 422。
  - `wait`：最多等多少秒（0–60，默认 0）再返回；运行在这之前结束或停下来等人，就提前返回。
  - `project`：可选，同 `POST /runs`（projects.md）。
  - `conversation_id`、`new_conversation`：可选，多轮对话用，见“会话”。
  - 运行的是应用的当前版本。
- `GET /v1/runs/{run_id}?wait=0` → `RunResult`。只能查这个密钥所属应用的运行，别的返回 404「没有找到这次运行」。`wait` 同上：运行还在跑时最多等这么久。
- `POST /v1/runs/{run_id}/answer`，请求体 `{"values": {…}, "wait": 0}` → `RunResult`。只能填表单（`pending.kind` 是 `form`）；等的是审批时返回 403「这一步要审批者在平台上确认」，没在等返回 409「这次运行现在没有在等表单」。

用 curl 跑一次：

```bash
curl -s -X POST https://平台地址/api/v1/apps/hello/runs \
  -H "Authorization: Bearer $AEP_KEY" -H "Content-Type: application/json" \
  -d '{"input": {"text": "你好"}, "wait": 30}'
```

## MCP（把应用当成工具）

地址：`{AEP_WEB_URL}/api/mcp/apps/{app}`，Streamable HTTP，无状态，请求头同样带 `Authorization: Bearer aep_…`。Claude、Cursor 等 MCP 客户端加一个远程 HTTP 服务器即可。

工具：

- `{app 名的 - 换成 _}`（例如 `retail_etl`）：运行这个应用。说明是应用的标题和简介；参数是编排应用开始节点的输入（`text`、`textarea`、`datasource` 是字符串，`number` 是数字，`boolean` 是布尔，`select` 是枚举），代码应用是 `{"request": "需求"}`。最多等 50 秒：完成就返回结果，停下来等人或还在跑就返回运行状态和链接，之后用 `get_run` 查。失败时工具结果标记为错误。
- `get_run`，参数 `{"run_id": "…", "wait_seconds": 0}`（0–50）：查这个应用的一次运行。

两个工具的结果都带 `structuredContent`（就是上面的 `RunResult`），文字内容是给模型看的中文摘要。审批、表单都要人在平台上处理，MCP 里不能代替人确认。

能对话的应用（智能体应用、开了对话的编排应用），运行工具的参数多两个可选的 `conversation_id`（字符串）和 `new_conversation`（布尔），说明里写“要接着上次聊，就传上次结果里的 conversation_id；要开始一段新的多轮对话，传 new_conversation: true”。规则同下面的“会话”。这样的应用，工具参数的 `required` 只有 `message`：接着聊时只能给 `message`，其他输入开会话时已经定了；开新会话或一次性调用时，开始节点的其他必填输入照样要给，没给时按“开始节点校验”报错（“…不能为空”），所以这些输入的说明末尾写上“（开新会话或单次调用时必填）”。

## 会话

智能体应用第 1 批新增。外部系统也能和应用多轮对话，会话的规则见 [conversations.md](conversations.md)。MCP 是无状态的，会话记在平台上，靠 `conversation_id` 接上。

- `POST /v1/apps/{app}/runs` 多两个可选字段，只对能对话的应用有效（别的应用给了 409「这个应用没有开对话」）：
  - `new_conversation: true`：开一个新会话，这次运行是第 1 轮。`input` 里的 `message` 是这一轮的消息，其余的输入成为会话的 `input`（conversations.md“开会话”）。
  - `conversation_id`：接着这个会话发下一条。`input` 只能有 `message`，别的输入开会话时已经定了，再给 422「会话里的其他输入在开会话时已经定了」。上一轮还没结束 409「上一条还没处理完」。
  - 两个都给 422。都不给就是以前的一次性运行，不在会话里。
- 用 API 开的会话，发起人写「API：密钥名称」，只有开它的那个密钥能接着发、能查；别的密钥（同一个应用的也一样）返回 404「没有找到这个会话」。密钥删掉后这个会话不能再接着发，平台里的管理员仍能按 id 打开看。
- `GET /v1/conversations/{conversation_id}` → `Conversation` 加 `turns`（同 `GET /conversations/{id}`），权限同上。
- MCP 的运行工具用同样的两个参数，返回的 `RunResult` 里有 `conversation_id`；给模型读的文字结果末尾也写上它（“要接着这段对话，下次传 conversation_id：…”）。
- 一次性运行（不带这两个字段）的行为和返回都不变，只多两个为 null 的字段。

## 入口

智能体应用第 1 批新增，所有应用都有。应用页「接入」标签列出这个应用的 4 个入口，每个可以单独开关，借鉴 Copilot Studio 的「渠道」页，但放在已有的标签里，不另开页。

```jsonc
// AppAccess：GET /apps/{app}/access
{
  "use":  {"enabled": true, "last_used_at": "…" | null},   // 平台里的使用者：使用页 /use/{app}、新建运行页、会话
  "api":  {"enabled": true, "last_used_at": "…" | null},   // 应用 API（/v1）
  "mcp":  {"enabled": true, "last_used_at": "…" | null},   // MCP（/mcp/apps/{app}）
  "apps": {"enabled": true, "last_used_at": "…" | null}    // 「允许被其他应用调用」：调用应用节点、智能体的 apps
}
```

- `PUT /apps/{app}/access`，请求体 `{"use"?: bool, "api"?: bool, "mcp"?: bool, "apps"?: bool}`，只改给了的项 → `AppAccess`。要搭建者（auth.md 规则 3）；记进操作记录「修改应用入口」。
- 默认全开，以前的应用也是全开。开关是应用的设置，不进版本，改了立刻生效，和密钥一样不随发布和回滚变化。
- `last_used_at`：最近一次经这个入口发起运行的时间，没有为 null。
- 关掉以后：
  - `use`：只有搭建者和管理员还能在平台里发起运行和会话（调试用），别人 `POST /runs`、`POST /conversations`、发消息返回 403「这个应用没有开放给使用者」，使用页和新建运行页的应用列表里也不列它。
  - `api`、`mcp`：对应入口返回 403「这个应用没有开放这个入口」；密钥不删，重新打开后照常能用。
  - `apps`：调用应用节点和智能体的 `apps` 保存时报“应用「…」不允许被其他应用调用”；已经发布的调用方运行时，调用应用节点以 `input_error` 失败，智能体不再把它作为工具给模型（agent-apps.md“调用应用”）。
- 定时运行（schedules.md）不看这些开关。已经在跑和在等人的运行不受影响。

## 对外入口

浏览器和外部系统都只经网页服务进来（`/api/…`）。网页服务把 `/api/v1/…` 和 `/api/mcp/…` 原样转给后端，只转发 `Authorization`、`Content-Type`、`Accept`、`Mcp-Session-Id`、`Mcp-Protocol-Version` 这些头，等待上限 90 秒。

部署在整站有访问口令的环境时（比如 VPS 上的 Caddy basic auth），外部系统带不了口令：口令和 API 密钥都要放在 `Authorization` 头里，只能放一个。这时要让反向代理对 `/api/v1/*` 和 `/api/mcp/*` 不要求口令，由这两个入口自己要求 API 密钥。仓库里的 infra/vps/Caddyfile 就是这样放行的（2026-11-02 定）。

## 操作记录

`/v1` 的发起、填表单记进操作记录（auth.md），操作人为空，`target` 里多一个 `key`（密钥名称）。新建、删除密钥也记录。

## 页面

应用页「接入」标签：

- 入口列表（智能体应用第 1 批新增）：使用页、应用 API、MCP、被其他应用调用，每行有开关、最近使用时间和一句说明；「被其他应用调用」那一行另列哪些应用在调用它（按它们的当前版本）。
- 「API 和 MCP」：调用地址、curl 示例、MCP 客户端配置示例（JSON）。能对话的应用另给一段多轮对话的 curl 示例。
- 密钥列表：名称、前缀、谁建的、最近使用；新建后在对话框里显示一次完整密钥并提供复制按钮，关掉就再也看不到。

## 测试

后端：密钥的增删和只存哈希、`/v1` 的三种错误（401、403、404）、`wait`、表单和审批的区别、`/mcp` 的工具列表和调用（用 mcp 客户端库经 ASGI 调）。会话：`/v1` 和 MCP 开会话、接着发、别的密钥 404、上一轮没完 409。入口：四个开关各自关掉后的报错，`use` 关掉时搭建者仍能跑，定时运行不受影响。前端：Playwright 的 mock 覆盖密钥的新建（只显示一次）、删除和调用示例。
