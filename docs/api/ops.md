# 运营看板和模型预算接口

后端和前端按这份约定并行开发。字段名 snake_case，错误格式 `{"detail": "中文说明"}`，校验错误 422。

落在哪一层：**平台资源和平台策略**。看板只读平台已有的记录（运行、模型调用记录、资产目录），再加一张算力用量表；预算是平台在调模型前统一执行的策略，和数据外发策略同一层，应用一行不改。

参考了主流平台的做法：Microsoft Foundry 和 Copilot Studio 的分析页（会话量、成功率、按 Agent 看费用）、AWS AgentCore 的可观测看板、腾讯云 ADP 的应用统计、LiteLLM 和 Dify 的按应用预算。共同点是按应用看量、成功率、耗时和费用，预算按应用设上限，到了提醒或拦截。

## 看板

`GET /ops/summary?days=30`（`days` 只能是 7、30、90，默认 30）返回：

```jsonc
{
  "days": 30,
  "since": "…",                       // 统计起点（现在往前 days 天）
  "totals": Metrics,
  "apps": [ {"app": "etl-lakehouse", "title": "…", "budget": Budget | null, ...Metrics} ],   // 有运行或有预算的应用，按运行数从多到少
  "daily": [ {"date": "2026-11-01", "runs": 3, "done": 2, "error": 1, "model_cost_usd": 0.42} ],   // 按平台时区的日期，没有运行的日子也列出
  "reuse": Reuse,
  "compute_hourly_usd": 2.5 | null   // 算力每小时单价，见「Databricks 费用」
}
```

```jsonc
// Metrics
{
  "runs": 12, "done": 9, "error": 2, "cancelled": 1, "active": 0,   // 统计期内新建的运行，按现在的状态；active 是还在跑或等人的
  "delivery_seconds_median": 840 | null,   // 交付时长：成功的运行从新建到结束的中位数
  "wait_seconds_median": 600 | null,       // 其中等人确认的时间（中位数），以前的运行没记等待时间的不算
  "first_pass_rate": 0.75 | null,          // 一次通过率，见下
  "model_cost_usd": 1.23 | null,           // 模型费用（网关报了费用的调用之和）；一次都没报时为 null
  "model_calls": 40, "unpriced_calls": 2,  // 调用次数；网关没报费用的次数
  "prompt_tokens": 120000, "completion_tokens": 8000,
  "compute_seconds": 3600,                 // Databricks 作业时长之和
  "compute_cost_usd": 2.5 | null           // compute_seconds × 单价；没设单价时为 null
}
```

- **一次通过率**：统计期内新建的“首次运行”（不是重跑出来的）里，已经结束的那些中，没被重跑、并且成功结束（`done`，`outcome` 不是 `stopped`）的比例。重跑、失败、终止都算没一次通过。还没结束的不算进分母；分母为 0 时为 null。
- **交付时长**：只算 `done` 的运行；包括等人的时间（交付是端到端的），等人的部分另列 `wait_seconds_median`。
- **模型费用**：来自模型页的调用记录（`model_calls`，见 [models.md](models.md)），按调用所在的运行归到应用；不在运行里的调用（知识库算向量、检索测试、页面上的试用）归到 `totals`，不归到应用。费用是网关按它的价格表算的，网关不知道价格的模型不计费用，计入 `unpriced_calls`。
- 调用记录只保留 90 天（和模型页一样），所以 `days` 最多 90。

### Databricks 费用

平台读不到 Databricks 的账单（要 `system.billing` 的权限，客户环境一般不给），所以记**作业时长**，每条算力用量记运行、节点、种类、集群和秒数：

- `job`：数据节点在 Databricks 上跑的作业（开发验证、发布后的首次运行、导入），按 Jobs API 返回的 `run_duration`（没有时用准备、执行、清理三段之和）。
- `command`：在集群上执行的命令（检查、取数），按平台等它返回的时间。

发布到 prod 后按计划在 Databricks 上跑的作业不经过平台，不计入。费用 = 时长 × 平台设置的每小时单价：

| 方法 | 路径 | 请求 | 响应 |
| --- | --- | --- | --- |
| PUT | `/ops/settings` | `{"compute_hourly_usd": 2.5 \| null}`（0–1000） | `{"compute_hourly_usd"}` |

单价按集群规格自己估（DBU 单价 × 每小时 DBU 数 + 云主机费用），页面上写明“按单价估算，不是账单”。只有管理员能改。

### 资产复用

```jsonc
// Reuse（来自资产目录，见 catalog.md）
{
  "assets": 40,                          // 资产目录里的资产数（不含模板）
  "reused": 6,                           // 被 2 个及以上应用用到的资产数
  "apps_from_templates": 5,              // 从模板新建的应用数
  "top": [ {"kind": "skill", "id": "etl-plan", "title": "…", "link": "/skills/etl-plan", "used_by": 4} ]   // 被用得最多的 5 项
}
```

## 预算

每个应用可以设每月模型预算（美元，按平台时区的自然月）。

```jsonc
// Budget
{
  "monthly_usd": 20,          // 0.01–100000
  "warn_at": 0.8,             // 用到这个比例时提醒，0.5–1
  "block": true,              // 用完后拦截这个应用的模型调用；false 只提醒
  "month_cost_usd": 12.3,     // 本月已用（网关报了费用的调用之和）
  "state": "ok",              // ok | warn（过了提醒线）| over（用完）
  "updated_by": "张三" | null, "updated_at": "…"
}
```

| 方法 | 路径 | 请求 | 响应 |
| --- | --- | --- | --- |
| GET | `/apps/{app}/budget` | | `Budget`，没设时 `null` |
| PUT | `/apps/{app}/budget` | `{"monthly_usd", "warn_at"?, "block"?, "actor"?}` | `Budget` |
| DELETE | `/apps/{app}/budget` | | 204 |

- 看板和预算开发者能看，只有管理员能设和删、能改单价（和模型页一样，见 [auth.md](auth.md)）。
- **拦截**：平台在每次调模型前（`aep.llm`，和数据外发策略同一处）查这次运行所属应用的预算；`block` 为 true 且本月已用不少于预算时，这次调用不发出去，节点按模型调用失败处理（走节点的出错设置），错误是“应用「…」本月的模型预算 $20 已经用完，请管理员在运营看板调高预算或下月再用”。子应用节点调起的运行按被调的应用算。不在运行里的调用不受预算限制。
- 本月已用按 30 秒缓存，所以可能超出预算一点点（最后几次并发调用）；页面上写明。
- **提醒**：本月第一次过提醒线、第一次用完时，各发一条通知（事件 `app.budget`，发给管理员，渠道同 [notifications.md](notifications.md)），链接到运营看板。改了预算金额或提醒线，本月的提醒重新算（调高后再用到线上还会提醒）；只改 `block` 不重新算。
- 应用页「评测与发布」标签和运营看板的应用表格都显示预算和本月用量。

## 页面

导航加「运营看板」（`/ops`，管理员和开发者可见）：

- 顶部选 7 / 30 / 90 天。
- 总览卡片：运行数、成功率、一次通过率、交付时长中位数（含等人）、模型费用、Databricks 时长和估算费用。
- 每天的运行数和模型费用（柱状图）。
- 应用表格：应用、运行数、成功、失败、一次通过率、交付时长、模型费用、Databricks 时长、预算（本月已用 / 预算，状态着色），点「设预算」弹框。
- 资产复用：几个数和被用得最多的 5 项。
- Databricks 单价设置（管理员）。

## 存储

```sql
-- compute_usage(id, tenant_id, run_id, node, kind, cluster_id, seconds, at)
-- app_budgets(tenant_id, app, monthly_usd, warn_at, block, updated_by, updated_at, warned_month, over_month)
-- 单价存在平台设置里（models.md 的 SettingStore，键 ops.compute_hourly_usd）
```
