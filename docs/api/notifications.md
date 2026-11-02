# 通知接口

后端和前端按这份约定并行开发。字段名 snake_case，时间是 ISO 8601 字符串。错误格式同 [datasources.md](datasources.md)：`{"detail": "中文说明"}`；请求体不合格返回 422，`detail` 是一句中文。

落在哪一层：**平台底座**。运行的状态一变，平台统一发通知，应用和节点不用写任何通知代码。

## 什么时候发、发给谁

| 事件 | 代号 | 发给谁 |
| --- | --- | --- |
| 待确认 | `run.waiting` | 等的是表单（人工节点 `mode: form`）：发起人；其他确认（审批、方案确认、上线审批）：所有审批者（有 `approver` 角色的账号，管理员也算） |
| 完成 | `run.done` | 发起人 |
| 失败 | `run.error` | 发起人 |

- 取消不发通知（是人自己取消的）。
- 草稿的试跑和评测运行（不属于任何版本的运行）不发通知，免得评测一次几十条。
- 「发起人」是运行记录里的 `created_by`，按显示名找账号；找不到账号（比如登录关着时手填的名字）就只发站内通知给所有人看。
- 登录关着时（[auth.md](auth.md)）没有账号，站内通知不分人，谁打开都看得到全部；邮件不发；Webhook 照常发。
- 停用的账号不收通知。

每条通知都带运行页的链接 `{AEP_WEB_URL}/runs/{run_id}`，点开直达确认页或结果页。

## 渠道

1. **站内通知**：总是有。页面左下角的铃铛显示未读数，点开是通知中心（`/notifications`）。保留 90 天。
2. **邮件**：服务器配了 SMTP、账号填了邮箱才发。SMTP 用环境变量配置，密码不进库：
   - `AEP_SMTP_HOST`（不填就不发邮件）、`AEP_SMTP_PORT`（默认 587）
   - `AEP_SMTP_USER`、`AEP_SMTP_PASSWORD`（可以不填，表示不登录）
   - `AEP_SMTP_FROM`（发件人，默认等于 `AEP_SMTP_USER`）
   - `AEP_SMTP_SECURITY`：`starttls`（默认）、`ssl` 或 `none`
3. **Webhook**：管理员在「通知渠道」页登记，按事件（和可选的应用范围）推送，不分人。格式：
   - `generic`：通用 JSON（见下面 `WebhookPayload`），自己的系统接收
   - `wecom`：企业微信群机器人（markdown 消息）
   - `dingtalk`：钉钉群机器人（markdown 消息）；机器人的安全设置请用「自定义关键词」，关键词填 `AEP`
   - `feishu`：飞书群机器人（文本消息）；安全设置同样用关键词 `AEP`

发送失败不影响运行，也不重试；Webhook 记下最近一次的结果，在「通知渠道」页能看到。

## 数据结构

```jsonc
// Notification
{
  "id": 42,
  "event": "run.waiting",            // run.waiting | run.done | run.error
  "run_id": "…",
  "app": "retail-etl",
  "title": "「零售 ETL」等你确认",      // 一句话
  "body": "方案确认：请确认清洗方案……",  // 可以为空字符串；最多 500 字
  "link": "/runs/…",                  // 站内路径；邮件和 Webhook 里是带 AEP_WEB_URL 的完整地址
  "created_at": "…",
  "read": false
}

// NotificationPage
{"items": [Notification], "unread": 3, "next": 41 | null}   // next 传给 before 取更早的

// Webhook（url 只在新建、修改时写入，读出来是打码的）
{
  "webhook_id": "wh_…",
  "name": "数据组群",                       // 1–50 字符
  "kind": "generic" | "wecom" | "dingtalk" | "feishu",
  "url_hint": "https://qyapi.weixin.qq.com/…a1b2",   // 只给协议、主机名和最后 4 个字符
  "events": ["run.waiting", "run.error"],  // 至少一个
  "apps": ["retail-etl"] | null,           // null：所有应用
  "enabled": true,
  "created_at": "…",
  "last_sent_at": "…" | null,
  "last_status": "ok" | "error" | null,
  "last_error": "…" | null                 // 最近一次失败的中文原因
}

// ChannelStatus（GET /notification-channels）
{
  "email": {"configured": true, "from": "aep@example.com" | null},
  "web_url": "https://…",                  // 链接用的地址（AEP_WEB_URL）
  "webhooks": [Webhook]
}

// WebhookPayload（generic 格式推送的请求体）
{
  "event": "run.done",
  "run_id": "…",
  "app": "retail-etl",
  "app_title": "零售 ETL",
  "title": "…",
  "body": "…",
  "link": "https://…/runs/…",
  "at": "…"
}
```

## 接口

### 我的通知（每个账号，登录关着时是所有人）

- `GET /notifications?limit=20&before=&unread=false` → `NotificationPage`。`limit` 1–100，新的在前；`unread=true` 只要未读的。
- `POST /notifications/read`，请求体 `{"ids": [42, 41]}`，不给 `ids` 表示全部已读 → `{"unread": 0}`。只改得到自己的通知，别人的 id 忽略。

### 通知渠道（管理员）

- `GET /notification-channels` → `ChannelStatus`
- `POST /webhooks`，请求体 `{name, kind, url, events, apps?, enabled?}` → 201 `Webhook`。`url` 必须是 `http://` 或 `https://` 开头，最长 1000 字符。
- `PATCH /webhooks/{webhook_id}`，请求体是上面字段的任意子集（`url` 不给就不改）→ `Webhook`
- `DELETE /webhooks/{webhook_id}` → 204
- `POST /webhooks/{webhook_id}/test` → `Webhook`（发一条测试消息，结果写进 `last_status`、`last_error`）

不存在的 `webhook_id` 返回 404「没有找到这个 Webhook」。

### 账号的邮箱

[auth.md](auth.md) 的 `User` 加一个字段 `email`（`string | null`）。`POST /users` 和 `PATCH /users/{user_id}` 可以带 `email`；`PATCH` 里给空字符串表示清掉。格式不对返回 422「「邮箱」的格式、长度或范围不正确」。

## 页面

- 左下角账号区上方的铃铛：未读数（超过 99 显示 99+），每 30 秒刷新一次。点开是通知中心。
- 通知中心 `/notifications`：每条显示标题、应用、时间、正文，未读的加粗；点一条标为已读并打开运行页；「全部标为已读」；「加载更早的」。
- 通知渠道 `/notification-channels`（管理员，导航里在「操作记录」上面）：邮件是否配好（只读，教你配哪些环境变量）、链接地址；Webhook 列表，新建、修改、停用、删除、发测试消息，看最近一次结果。
- 账号页的新建、修改对话框多一个「邮箱」。

## 存储

```sql
notifications(id bigserial, tenant_id, user_id text NULL, event, run_id, app, title, body, link,
              created_at, read_at)          -- user_id 为空：登录关着时发的，所有人可见
webhooks(webhook_id, tenant_id, name, kind, url, events text[], apps text[] NULL, enabled,
         created_at, last_sent_at, last_status, last_error)
users.email text NULL
```

Webhook 的地址里常带机器人的密钥，所以接口只返回打码的 `url_hint`；地址存在平台库里，和应用密钥一样不出现在日志和操作记录里。

## 测试

后端：`MemoryNotificationStore` + 假的发送器（记下要发的邮件和 Webhook 请求），不连外网；覆盖三种事件的收件人、草稿运行不发、登录开关两种情况、已读、Webhook 的格式和打码、失败只记结果。前端：Playwright 的 mock 覆盖铃铛、通知中心、通知渠道页。
