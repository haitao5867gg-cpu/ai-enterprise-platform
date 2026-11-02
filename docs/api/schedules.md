# 定时运行接口

后端和前端按这份约定并行开发。字段名 snake_case，时间是 ISO 8601 字符串（UTC）。错误格式同 [datasources.md](datasources.md)：`{"detail": "中文说明"}`；请求体不合格返回 422。

落在哪一层：**平台底座**。任何应用（编排应用和代码应用）都能按时发起运行，应用自己不用写任何定时代码。和上线节点的区别：上线节点（data-nodes.md，`databricks_release`）把代码部署成 Databricks 上按时跑的 Job，之后由 Databricks 自己跑；定时运行是平台按时把整个应用跑一遍（比如每天早上出一份报表、每月 1 日按新月份跑一次 ETL 并请人审核）。

参考了主流平台的做法：Dify 的「定时触发」、Coze 的「触发器」、n8n 的 Schedule Trigger、Databricks Jobs 的定时。共同点是每个定时属于一个应用、用 cron 写时间、带时区、能暂停、能“立即跑一次”、列出最近一次的结果；上一次还没跑完时默认跳过这一次（Databricks 的 `max_concurrent_runs: 1`）。

## 数据结构

```jsonc
// Schedule
{
  "schedule_id": "sch_3f9a1c2e",
  "app": "retail-etl",
  "title": "每天早上的销售日报",            // 1–100 个字符
  "cron": "0 8 * * 1-5",                   // 5 段 cron，见下
  "timezone": "Asia/Shanghai",             // IANA 时区
  "input": {"request": "按城市统计昨天的销售额"},   // 运行输入，同 POST /runs 的 input
  "project": {"project_id": "jade-ibf", "environment": "dev", "params": {"update_month": "2026-11"}} | null,
                                           // 同 POST /runs 的 project；每次运行时按项目当时的配置绑定
  "enabled": true,
  "created_by": "张三",                    // 新建的人（登录关着时是新建时填的名字）
  "created_at": "…",
  "updated_at": "…",
  "next_run_at": "…" | null,              // 下一次什么时候跑；停用时为 null
  "next_runs": ["…", "…", "…"],           // 接下来 3 次（停用时为 []），给页面预览
  "last_run": {"run_id": "…", "at": "…", "status": "done"} | null,   // 最近一次发起的运行和它现在的状态
  "last_error": "上一次运行还没结束，这次跳过（2026-11-03 08:00）" | null   // 最近一次没发起成功的原因；发起成功后清空
}
```

### cron 的写法

标准 5 段：分 时 日 月 周，空格分开。

- 每段可写 `*`、数字、范围 `a-b`、步长 `*/n` 或 `a-b/n`、用逗号分开的多项。
- 分 0–59，时 0–23，日 1–31，月 1–12，周 0–7（0 和 7 都是周日）。不支持英文缩写、`?`、`L`、`W`、`#`。
- 日和周都不是 `*` 时，两者满足一个就跑（同 Unix cron）。
- 不合格时 422“定时写法不对：……”，说明哪一段错了（如“第 2 段（时）的 24 超出 0–23”）。
- 平台自己解析，不新增依赖。一分钟最多跑一次，所以最小间隔是 1 分钟；平台每 `AEP_SCHEDULE_POLL_SECONDS`（默认 30）秒检查一次到点的定时，所以会晚最多这么多秒。

时区不合格时 422“时区 … 不认识”。默认 `AEP_SCHEDULE_TIMEZONE`（默认 `Asia/Shanghai`）。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/apps/{app}/schedules` | 这个应用的定时，按新建时间排；应用不存在 404 |
| POST | `/apps/{app}/schedules` | 新建，返回 201 和 `Schedule` |
| GET | `/schedules/{schedule_id}` | 一个定时；不存在 404“没有定时 …” |
| PATCH | `/schedules/{schedule_id}` | 改 `title`、`cron`、`timezone`、`input`、`project`、`enabled` 里给了的项，返回 `Schedule` |
| DELETE | `/schedules/{schedule_id}` | 删除，204；已经发起的运行不受影响 |
| POST | `/schedules/{schedule_id}/run` | 立即按这个定时跑一次（停用的也可以），返回 201 和 `{"run_id", "status"}`；不改 `next_run_at` |

新建的请求体：

```jsonc
{"title": "每天早上的销售日报", "cron": "0 8 * * 1-5", "timezone": "Asia/Shanghai",
 "input": {}, "project": null, "enabled": true}   // timezone、input、project、enabled 可以不写
```

保存（新建和修改）时检查：

- 应用要有已发布的版本（代码应用总是有），否则 409“应用 … 还没有发布，不能定时运行”。定时运行总是用应用当时的生效版本，不跑草稿，也不固定版本：应用发布了新版本，下一次就用新版本。
- `input` 按生效版本的开始节点检查，规则和 POST /runs 一样（必填、类型、选项、数据源）；不合格 422，`loc` 是 `["body", "input", "<key>"]`。
- `project` 按 POST /runs 的规则检查（项目存在、环境存在、参数），不合格 422。
- 每个应用最多 20 个定时，超过 409“每个应用最多 20 个定时”。

## 到点时发生什么

- 平台的后台任务每 `AEP_SCHEDULE_POLL_SECONDS` 秒取出 `enabled` 且 `next_run_at` 已到的定时，先把 `next_run_at` 推到下一次（从现在算，不补跑停机期间错过的次数），再发起运行。多个 API 实例同时检查时用 `FOR UPDATE SKIP LOCKED`，一个定时一次只会被一个实例取到。
- 发起的运行和 POST /runs 一样：生效版本、`input`、`project`；`created_by` 是定时的 `created_by`（通知照常发给他，见 notifications.md）；运行记录新增 `trigger: {"kind": "schedule", "schedule_id": "…", "title": "…"}`（别的运行为 null），运行页和运行列表显示“定时：每天早上的销售日报”。“立即跑一次”发起的运行也带 `trigger`。
- 上一次由这个定时发起的运行还没结束（`running`、`waiting`）时，这一次跳过，不发起运行，`last_error` 记“上一次运行还没结束，这次跳过（时间）”。“立即跑一次”不看这一条。
- 发起失败（应用删了、没有生效版本了、输入按新版本不合格、项目删了）时记进 `last_error`，定时照旧往后排，不停用；应用删了时定时一起删掉。
- 登录打开时，后台任务发起的运行不需要谁在线。

## 权限

登录打开时（auth.md）：看定时要登录；新建、修改、删除、立即跑一次要搭建者（`builder`），否则 403“没有权限：这项操作需要搭建者”。

## 前端

应用页「接入」标签加一节「定时运行」：

- 列表：名称、什么时候跑（cron 的中文说法，如“周一到周五 08:00”，说不清时显示原文）、时区、下一次、最近一次（链接到运行，带状态）、`last_error`（有时用提示色）、启用开关、“立即跑一次”、编辑、删除（要确认）。
- 新建、编辑在对话框里：名称；什么时候跑：常用的几种（每天几点、工作日几点、每周几几点、每月几号几点、每小时第几分）点选，另有“自己写 cron”；时区；运行输入按生效版本的开始节点生成表单（同发起运行页）；绑定项目的应用另选项目、环境和参数。保存前显示“接下来 3 次”（从后端返回的 `next_runs`）。
- 运行页和运行列表显示定时发起的运行“定时：名称”。
- mock：定时存在内存里，“立即跑一次”照 POST /runs 发起 mock 运行；后台不自动跑。

## 测试

- cron：每段的写法和越界、日和周的“或”、跨月跨年、月底（31 日在小月跳过）、时区（含夏令时的时区，如 `America/New_York`）、下一次和接下来 3 次。
- 接口：新建、修改、删除、列表、立即跑一次；没有生效版本 409；输入和项目的 422；20 个上限；权限。
- 后台任务：到点发起运行并推后 `next_run_at`、带 `trigger` 和 `created_by`；停用的不跑；上一次没结束时跳过并记 `last_error`；发起失败记 `last_error`；两个实例同时取只发起一次（Postgres，`AEP_TEST_DATABASE_URL`）；应用删除时定时一起删。
- 前端：Playwright（mock）新建、编辑、停用、立即跑一次、删除。
