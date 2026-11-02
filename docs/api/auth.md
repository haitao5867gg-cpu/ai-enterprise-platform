# 登录、角色和操作记录接口

后端和前端按这份约定并行开发。字段名 snake_case，时间是 ISO 8601 字符串。错误格式同 [datasources.md](datasources.md)：`{"detail": "中文说明"}`；请求体不合格返回 422，`detail` 是一句中文。

落在哪一层：**平台底座**。登录、角色检查和操作记录都在平台 API 的入口统一做，应用、节点和连接器不用改。

## 开关

- `AEP_AUTH_ENABLED`（默认 `false`）：关着时和以前一样，谁都能用，所有请求都当成「本地用户」（拥有全部角色），页面上仍然可以自己填名字。打开后除了下面列出的公开接口，都要先登录。
- `AEP_SESSION_HOURS`（默认 `168`，即 7 天）：登录多久后要重新登录。
- `AEP_COOKIE_SECURE`（默认 `false`）：只走 HTTPS 时打开，登录 Cookie 只经 HTTPS 发送。

打开开关的部署第一次打开页面时，平台里还没有账号，页面引导建第一个管理员（`POST /auth/setup`）。这个入口只在一个账号都没有时可用。

## 角色

| 角色 | 代号 | 能做什么 |
| --- | --- | --- |
| 使用者 | `user` | 看运行记录、发起运行、上传文件作数据源、取消、重试、复制运行、填人工节点的表单（`mode: form`）；可以查看应用、Skill、知识库、数据源、项目，但不能改 |
| 审批者 | `approver` | 使用者能做的，加上处理人工确认（人工节点 `mode: approval`、ETL 应用的方案确认和上线审批） |
| 搭建者 | `builder` | 使用者能做的，加上新建和修改应用、Skill、知识库、数据源、项目，测试节点，评测和发布应用，查看模型页 |
| 管理员 | `admin` | 全部，包括管理模型、账号和查看操作记录 |

- 一个账号可以有多个角色，至少一个。每个登录的账号都有使用者的权限。
- 搭建者不自动有审批者的权限：做的人和批的人可以分开。管理员拥有全部权限。
- 至少要留一个没停用的管理员：去掉最后一个管理员的管理员角色、停用或删掉它，返回 409。

## 数据结构

```jsonc
// User
{
  "user_id": "u_...",
  "username": "zhangsan",          // 登录名：小写字母开头，小写字母、数字、. _ -，2–40 个字符；建好后不能改
  "display_name": "张三",           // 1–50 字符；运行记录、审批记录里写的就是它
  "roles": ["builder", "approver"], // 按 user、approver、builder、admin 的顺序
  "disabled": false,
  "created_at": "…",
  "last_login_at": "…" | null
}

// AuthStatus
{"enabled": true, "setup_needed": false, "user": User | null}

// AuditEvent
{
  "id": 123,
  "at": "…",
  "user": {"user_id": "u_...", "username": "zhangsan", "display_name": "张三"} | null,
  "action": "POST /apps/{app}/draft/publish",   // 方法 + 接口路径模板；登录类是 login、login_failed、logout、setup
  "label": "发布应用",                            // 中文说明；没有专门说明的接口给 action 本身
  "target": {"app": "retail-etl"},               // 路径里的参数；登录失败时是 {"username": "..."}
  "status": 200,                                  // 接口返回的状态码
  "ip": "203.0.113.5" | null
}
```

## 接口

### 登录

| 方法 | 路径 | 请求体 | 返回 |
| --- | --- | --- | --- |
| GET | `/auth/status` | | `AuthStatus`。公开。开关关着时 `{"enabled": false, "setup_needed": false, "user": 本地用户}` |
| POST | `/auth/setup` | `{"username", "display_name", "password"}` | 201 `User`，并登录（设 Cookie）。公开，只在开关打开、一个账号都没有时可用，否则 409「已经有账号了，请直接登录」。建出来的账号是管理员 |
| POST | `/auth/login` | `{"username", "password"}` | `User`，设 Cookie。公开。账号或密码不对 401「账号或密码不对」；停用的账号同样 401；同一个登录名 15 分钟内错 5 次后 429「登录失败次数太多，请 15 分钟后再试」 |
| POST | `/auth/logout` | | 204，清 Cookie |
| GET | `/auth/me` | | `User`；没登录 401 |
| PUT | `/auth/me/password` | `{"old_password", "password"}` | 204。旧密码不对 422「原密码不对」；这个账号的其他登录都失效 |

- 登录用 Cookie `aep_session`：`HttpOnly`、`SameSite=Lax`、`Path=/`，`AEP_COOKIE_SECURE` 打开时加 `Secure`。Cookie 里是随机令牌，平台库里只存它的 SHA-256。
- 密码 8–128 个字符，用 scrypt 加盐存储。
- 第 3 批的应用 API 用 `Authorization: Bearer` 的 API key，不走 Cookie。

### 账号（管理员）

| 方法 | 路径 | 请求体 | 返回 |
| --- | --- | --- | --- |
| GET | `/users` | | `[User]`，按登录名排序 |
| POST | `/users` | `{"username", "display_name", "roles", "password"}` | 201 `User`。登录名已存在 409「已经有登录名为「…」的账号了」 |
| PATCH | `/users/{user_id}` | `{"display_name"?, "roles"?, "disabled"?}` | `User`。停用后这个账号的登录全部失效 |
| PUT | `/users/{user_id}/password` | `{"password"}` | 204。管理员重设密码，这个账号的登录全部失效 |
| DELETE | `/users/{user_id}` | | 204。不能删自己（409「不能删除自己的账号」）。运行和审批记录里写的是名字，删账号不影响它们 |

不存在的账号 404「没有找到这个账号」。去掉最后一个管理员 409「至少要留一个管理员」。

### 操作记录（管理员）

| 方法 | 路径 | 参数 | 返回 |
| --- | --- | --- | --- |
| GET | `/audit` | `limit`（1–200，默认 50）、`before`（上一页最后一条的 `id`）、`user`（登录名）、`q`（在 `action` 和 `label` 里找） | `{"events": [AuditEvent], "next": 122 | null}`，新的在前 |

平台记下所有改动类的请求（POST、PUT、PATCH、DELETE，无论成功与否，被拒绝的也记）和登录、登录失败、退出、建第一个管理员。查看类请求（GET）不记。只记谁、什么时候、调了哪个接口、对哪个对象、结果状态码和 IP，**不记请求内容**（里面可能有密码、key 和客户数据）。保留 365 天，启动时删掉更早的。开关关着时也记，`user` 为 null。

## 权限怎么检查

在平台 API 入口按「方法 + 接口路径模板」统一检查，规则按顺序第一条匹配的生效：

1. 公开：`GET /healthz`、`GET /readyz`、`GET /auth/status`、`POST /auth/setup`、`POST /auth/login`。
2. 管理员：`/users` 下的全部、`/audit`、`/models` 下的 POST、PUT、PATCH、DELETE。
3. 搭建者：`/models` 下的 GET，`POST /llm/chat`；`/apps`、`/skills`、`/knowledge-bases`、`/datasources`、`/projects` 下的 POST、PUT、PATCH、DELETE（`POST /datasources/files` 除外）。
4. 其余：登录即可。

没登录 401「请先登录」；角色不够 403「没有权限：这项操作需要{角色}」。

`POST /runs/{id}/resume` 另外看这一步是什么：表单（`kind` 为 `form`）登录即可；其余（人工确认、方案确认、上线审批）要审批者，否则 403「没有权限：这一步要审批者确认」。

开关打开时，下面这些地方的人名**一律用登录人的显示名**，请求体里自己填的 `actor`、`requester` 不再生效：发起运行和复制运行的 `requester`、`resume`、`cancel`、`retry` 的 `actor`，应用和 Skill 的新建、保存草稿、导入、发布的 `actor`。

## 页面

- 开关打开且没登录时，任何页面都跳到 `/login`；没有账号时 `/login` 显示「建第一个管理员」。
- 侧栏底部显示当前账号、角色和「退出」；「修改密码」在账号菜单里。
- 导航按角色显示：使用者只看到「运行记录」「新建运行」；搭建者多看到「应用」「Skill」「知识库」「数据源与工具」「项目」「模型」；管理员再多「账号」「操作记录」。直接打开没权限的页面显示「没有权限」。
- 页面上让人填名字的地方（「你的名字」），开关打开时改为只读显示登录人。
- 待确认的运行：没有审批者权限的人看得到内容，按钮不能点，提示「这一步要审批者确认」。

## 存储

新增三张平台表，都带 `tenant_id`：`users`、`sessions`（令牌的 SHA-256、账号、过期时间）、`audit_events`。

## 测试

后端测试用内存版的账号库（`MemoryAuthStore`）；开关默认关，已有测试不受影响。需要真实 Postgres 的测试读取 `AEP_TEST_DATABASE_URL`。前端测试的模拟后端默认开关关着；带 Cookie `aep_mock_auth=on` 时按打开处理。
