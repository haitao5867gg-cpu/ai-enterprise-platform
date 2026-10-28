# 应用配置与 Skill 接口（第 1 阶段）

后端（Claude）和前端（Codex）按这份约定并行开发。字段名一律 snake_case，时间是 ISO 8601 字符串。错误沿用 FastAPI 格式：`{"detail": "中文说明"}`，校验错误为 422，`detail` 是 `[{"loc": [...], "msg": "..."}]`。

## 概念

- **Skill**：一段可复用的做法说明，采用开放的 Agent Skills 格式：SKILL.md 正文，加上可选的参考资料（`references/*.md`）。第 1 阶段只支持文字，不支持脚本。Skill 有版本：草稿发布后成为新版本，已发布的版本不可修改。
- **应用版本**：应用的配置（每个节点用哪个模型、哪些 Skill 的哪个版本、哪些参数）。流程结构仍然写在代码里。草稿发布后成为新版本；同一时间只有一个"当前版本"，新运行默认使用它，也可以切回旧版本（回滚）。
- **发布前评测**：有评测集的应用（目前是 `etl`），草稿必须先跑评测，得分达到阈值（默认 0.8）才能发布。没有评测集的应用（`hello`）可以直接发布。
- 模型节点的提示词 = 引用的 Skill 正文 + 平台固定的输出格式要求。改 Skill 只影响"怎么做"，不会破坏平台解析输出。

## Skill

### 数据结构

```jsonc
// SkillSummary
{
  "name": "etl-plan",                // ^[a-z][a-z0-9-]{1,63}$
  "description": "设计 ETL 方案的做法：……",
  "published_version": 2,            // 还没发布过时为 null
  "has_draft": false,
  "updated_at": "2026-10-28T02:00:00+00:00",
  "used_by": [{"app": "etl", "node": "plan", "version": 3, "skill_version": 2}]  // 当前版本里引用它的应用节点；version 是应用版本号，skill_version 是引用的 Skill 版本
}

// SkillContent：某个版本，或草稿
{
  "version": 2,                      // 草稿为 null
  "description": "……",
  "body": "SKILL.md 正文（Markdown，不含 frontmatter）",
  "references": {"glossary.md": "……"},   // 文件名 ^[\w.-]{1,64}\.md$，最多 10 个
  "note": "发布说明",                 // 草稿为 null
  "created_by": "张三",               // 可为 null
  "created_at": "2026-10-28T02:00:00+00:00"
}

// SkillDetail
{
  ...SkillSummary,
  "draft": SkillContent | null,
  "versions": [{"version": 2, "note": "……", "created_by": null, "created_at": "……"}],  // 新的在前
  "current": SkillContent | null     // 最新发布的版本
}
```

限制：`body` 最多 20000 字符，每个参考资料最多 20000 字符，`description` 最多 500 字符且不能为空。

### 接口

| 方法 | 路径 | 请求体 | 返回 |
| --- | --- | --- | --- |
| GET | `/skills` | | `SkillSummary[]`，按名称排序 |
| GET | `/skills/{name}` | | `SkillDetail`；不存在 404 |
| GET | `/skills/{name}/versions/{version}` | | `SkillContent`；不存在 404 |
| POST | `/skills` | `{"name", "description", "body", "references"?, "actor"?}` | 201 `SkillDetail`（只建草稿）；重名 409 |
| PUT | `/skills/{name}/draft` | `{"description", "body", "references"?, "actor"?}` | `SkillDetail`；没有草稿时以此新建草稿 |
| DELETE | `/skills/{name}/draft` | | 204；没有草稿 404 |
| POST | `/skills/{name}/publish` | `{"note", "actor"?}` | `SkillDetail`；没有草稿 409；`note` 不能为空 |

平台内置的 Skill（`etl-plan`、`etl-codegen`）在第一次启动时登记为版本 1，`created_by` 为 null，`note` 为"平台内置"。

## 应用

### 数据结构

```jsonc
// AppSummary
{
  "app": "etl",
  "title": "自然语言 ETL 工作流",
  "summary": "……",
  "current_version": 3,
  "has_draft": true,
  "has_eval_set": true
}

// NodeConfig：一个节点的设置；只出现在有可调设置的节点上
{
  "model": "company",                          // 模型别名；null 表示用平台默认模型
  "skills": [{"name": "etl-plan", "version": 2}],
  "params": {"max_attempts": 3}
}

// AppConfig
{"nodes": {"plan": NodeConfig, "codegen": NodeConfig, "profile": {"model": null, "skills": [], "params": {"sample_rows": 5}}}}

// Eval：一次发布前评测
{
  "status": "running" | "passed" | "failed" | "error",
  "score": 0.86,                    // 各用例得分的平均值；运行中为 null
  "threshold": 0.8,
  "started_at": "……",
  "finished_at": null,
  "error": null,                    // status 为 error 时的说明
  "cases": [{"id": "orders-clean", "status": "done", "score": 1.0, "run_id": "……", "error": null}]
}

// AppVersion
{
  "version": 3,                     // 草稿为 null
  "status": "current" | "draft" | "previous",
  "config": AppConfig,
  "note": "改了方案的 Skill",
  "created_by": "张三",
  "created_at": "……",
  "eval": Eval | null               // 草稿最近一次评测，或该版本发布时的评测
}

// Setting：节点上一项可调设置的说明，前端据此渲染表单
{"key": "model", "label": "模型", "type": "model"}
{"key": "skills", "label": "Skill", "type": "skills"}
{"key": "max_attempts", "label": "最多重写次数", "type": "integer", "min": 1, "max": 5, "help": "校验不通过时让模型重写的次数"}
{"key": "language", "label": "默认代码语言", "type": "enum", "choices": [{"value": "pyspark", "label": "PySpark"}, {"value": "sql", "label": "SQL"}]}
// type 还可以是 "boolean"、"string"（可带 "max_length"）

// AppDetail
{
  ...AppSummary,
  "pattern": "……", "knowledge": "……", "skills_text": "……",   // 应用设计说明（与 GET /runs/{id}/flow 的 design 相同）
  "nodes": [{
    "id": "plan", "title": "出方案", "kind": "model" | "tool" | "human" | "code" | "terminal",
    "design": {...},                 // 与 flow 接口里节点的 design 相同；起止节点为 null
    "settings": [Setting]            // 没有可调设置时为空数组
  }],
  "edges": [{"source": "plan", "target": "review", "conditional": false}],
  "current": AppVersion,
  "draft": AppVersion | null,
  "options": {
    "models": ["company", "mock", "deepseek"],     // 模型网关上可用的别名
    "default_model": "company",
    "skills": [{"name": "etl-plan", "description": "……", "published_version": 2}]
  }
}
```

### 接口

| 方法 | 路径 | 请求体 | 返回 |
| --- | --- | --- | --- |
| GET | `/apps` | | `AppSummary[]` |
| GET | `/apps/{app}` | | `AppDetail`；不存在 404 |
| GET | `/apps/{app}/versions` | | `AppVersion[]`，新的在前（不含草稿） |
| PUT | `/apps/{app}/draft` | `{"config": AppConfig, "actor"?}` | `AppDetail`；校验不过 422。保存草稿会清空草稿上次的评测结果 |
| DELETE | `/apps/{app}/draft` | | 204；没有草稿也是 204 |
| POST | `/apps/{app}/draft/eval` | `{}` | 202 `AppDetail`（`draft.eval.status` 为 running）；没有草稿 409；没有评测集 409；已有评测在跑 409 |
| POST | `/apps/{app}/draft/publish` | `{"note", "actor"?}` | `AppDetail`（草稿成为当前版本）；有评测集但草稿评测未通过 409 |
| POST | `/apps/{app}/versions/{version}/activate` | `{"actor"?}` | `AppDetail`（该版本成为当前版本，即回滚） |

草稿评测是后台任务，前端轮询 `GET /apps/{app}`（建议每 5 秒一次）查看 `draft.eval`。评测用例就是 `make eval` 的 ETL 评测集，每个用例是一次真实运行，`run_id` 可以链接到运行详情页。

## 运行

- `POST /runs` 新增可选字段 `app_version`（整数），不传则用当前版本。
- `GET /runs/{id}` 和 `GET /runs` 的每条记录新增 `app_version`。
- `GET /runs/{id}/flow` 的模型节点新增 `skills: [{"name", "version"}]`，`design` 的 `prompt` 字段改为"这次实际用的提示词"。
- 等待确认的 `pending` 新增 `comment_required`：需要写意见的选项（如 ETL 上线审批的 `reject`）；不带意见 resume 会返回 422。
- 草稿评测起的运行 `app_version` 为 null。
