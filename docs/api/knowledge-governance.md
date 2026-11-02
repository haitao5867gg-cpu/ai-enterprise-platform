# 知识库治理接口

后端和前端按这份约定并行开发。在 [knowledge.md](knowledge.md) 之上补知识库的治理：文档负责人、生效和失效日期、文档版本、检索用例回归、引用次数、口径文档模板。没提到的接口和字段保持不变；字段名 snake_case，错误格式 `{"detail": "中文说明"}`，校验错误 422。

落在哪一层：**平台资源**。全部做在知识库上，知识检索节点、智能体节点、etl 应用和检索测试共用，应用一行不改。没设日期的文档照旧都能检索到，所以已有的知识库和应用行为不变。

参考了主流平台的做法：Dify 的文档启用和停用、召回测试；腾讯云 ADP 的文档有效期和“问答对”评测；Microsoft Foundry 和 Copilot Studio 的知识源负责人和刷新时间。共同点是每份文档有人负责、过期的不再被检索、改了文档能马上回归检查检索质量、看得到哪些文档真被用到。

## 文档的新字段

`Document` 新增：

```jsonc
{
  "owner": "张三" | null,          // 负责人，最多 50 个字符
  "valid_from": "2026-01-01" | null,   // 生效日期（含当天），null 表示一直有效
  "valid_until": "2026-12-31" | null,  // 失效日期（含当天，过了这天就失效），null 表示不失效
  "validity": "active",            // active（有效）| pending（还没生效）| expired（已失效），按平台时区的今天算
  "version": 2,                    // 从 1 开始，替换上传一次加 1
  "uploaded_by": "李四" | null,    // 最近一次上传的人
  "hits_30d": 12,                  // 近 30 天检索到这个文档的运行数（检索测试不算）
  "last_hit_at": "…" | null        // 最近一次被运行检索到的时间
}
```

- 平台时区是 `AEP_SCHEDULE_TIMEZONE`（默认 `Asia/Shanghai`，和定时运行共用）。
- 不是 `active` 的文档在**所有检索**里都跳过：知识检索节点、智能体检索、etl 出方案、检索测试。检索测试可以带 `"include_inactive": true` 把它们也查出来，方便核对。
- `KnowledgeBase` 新增 `validity_counts`: `{"active": 10, "pending": 1, "expired": 1}`。
- 已有的文档：`owner`、日期为 null，`version` 为 1，`uploaded_by` 为 null。

## 接口

| 方法 | 路径 | 请求 | 响应 |
| --- | --- | --- | --- |
| PATCH | `/knowledge-bases/{kb}/documents/{doc}` | `{"owner"?, "valid_from"?, "valid_until"?}`，给了的才改，null 表示清空 | `Document`。失效日期早于生效日期 422“失效日期不能早于生效日期”；日期写法不对 422 |
| GET | `/knowledge-bases/{kb}/documents/{doc}/versions` | | `[{"version", "size_bytes", "uploaded_by", "uploaded_at"}]`，新的在前 |
| POST | `/knowledge-bases/{kb}/documents` | 新增表单字段 `replace`（`"true"` / 不填）和 `uploaded_by`（登录关着时页面填的名字） | 同前。`replace` 为 true 时，和已有文档同名的文件**替换**它（见下） |
| GET | `/knowledge-bases/doc-templates` | | 口径文档模板，见下 |
| GET | `/knowledge-bases/{kb}/regression` | | `Regression`，见下 |
| PUT | `/knowledge-bases/{kb}/regression` | `{"cases", "retrieval"?, "top_k"?, "min_score"?, "actor"?}` | `Regression` |
| POST | `/knowledge-bases/{kb}/regression/run` | 可选 `{"actor"?}` | `Regression`（跑完才返回）。没有用例 409“先加检索用例再运行回归” |

### 替换上传（文档版本）

- 同名（文件名完全一样）的文档被替换：`document_id` 不变，`version` 加 1，原文件换成新的，状态回到 `queued` 重新切分和算向量；负责人、日期、引用次数保留。
- 每次上传记一条版本记录（版本号、大小、上传的人、时间）；旧版本的原文件不保留，要留底请用资产目录把知识库提交到资产仓库。
- 不带 `replace` 时和以前一样：同名文件加 (2)、(3)。
- 替换时一次上传里不能有两个同名文件：422“这次上传里有两个「…」，替换时每个文件名只能传一个”。
- `actor`、`uploaded_by` 只在登录关着时用；登录开着时记登录的人。
- 替换只能用同一种文件类型（`.pdf` 换 `.pdf`），否则 422“「…」原来是 pdf，替换时也要传 pdf”。

### 检索用例回归

每个知识库可以有一组检索用例，改了文档后一键回归，看该找到的文档还找不找得到。

```jsonc
// Regression
{
  "kb_id": "uuid",
  "cases": [
    {"id": "c1", "query": "退货期限是几天", "expected": ["退换货政策.pdf"]}
    // id 1–40 位小写字母、数字、_-，不重复；query 1–500 字符；expected 1–5 个文档名，必须是这个知识库里的文档
  ],
  "retrieval": {"mode": "hybrid", "vector_weight": 0.7, "rerank": null},   // 同 knowledge.md 的 Retrieval，默认取页面默认值
  "top_k": 5,                      // 1–20
  "min_score": 0.3,                // 0–1
  "updated_by": "张三" | null, "updated_at": "…" | null,
  "last_run": {
    "at": "…", "by": "张三" | null,
    "passed": 9, "total": 10, "score": 0.9,
    "results": [
      {"id": "c1", "passed": true, "found": ["退换货政策.pdf"], "missing": [], "top": ["退换货政策.pdf", "会员权益.md"], "error": null}
    ]
  } | null,
  "stale": true                    // 上次回归之后文档有变化（上传、替换、删除、改日期），或者用例改了；没跑过也为 true（有用例时）
}
```

- 一条用例通过：`expected` 里的每个文档都在前 `top_k` 个命中里（按文档算，一个文档命中多个片段只算一次）。`top` 是命中的文档名，按名次去重。
- 最多 50 条用例。保存时文档名不在知识库里 422“知识库里没有文档「…」”；文档后来被删掉的用例照跑，算没通过。
- 回归按当前有效的文档检索（同节点）；一条用例检索出错时记 `error`、算没通过，不影响其他用例。
- 资产目录里知识库的质量分改为最近一次回归的 `score`（`basis`“检索回归通过率”，`detail` 如“10 条用例，通过 9 条”；`stale` 时加“，文档改过还没重跑”）；没有回归时仍是文档处理成功率。

### 口径文档模板

`GET /knowledge-bases/doc-templates` 返回：

```jsonc
[{"id": "metric", "title": "指标口径", "filename": "指标口径-{名称}.md", "body": "# {名称}\n\n## 定义\n…"}, …]
```

内置三个：`metric`（指标口径：定义、计算公式、统计粒度、时间口径、包含和排除、数据来源、负责人、生效日期）、`table`（表说明：用途、粒度、主键、字段说明、更新频率、上游）、`rule`（业务规则：规则、适用范围、例外、依据、生效日期）。正文里的 `{名称}` 由页面换成填的名称。页面「用模板写文档」：选模板、填名称、在文本框里改正文，保存时作为 `.md` 上传（走原来的上传接口）。

## 引用次数怎么记

- 运行里的知识检索节点、智能体的检索、etl 出方案检索到片段时，按（运行、文档）各记一次；同一运行多次检索到同一文档只算一次。检索测试和回归不记。
- `hits_30d` 是近 30 天的运行数，`last_hit_at` 是最近一次。记录失败不影响检索（记日志）。
- 文档列表可以按 `hits_30d` 排序，页面上标出近 30 天没被用到的文档（“近 30 天没有运行用到”），便于清理。

## 页面

知识库详情页：

- 文档表格加列：负责人、有效期（“长期有效”“2026-01-01 起”“到 2026-12-31”，失效的标红“已失效”，没到期的标“未生效”）、版本（v2，点开看版本记录）、近 30 天被用到（次数）。
- 每个文档的「编辑」：负责人、生效日期、失效日期。
- 上传对话框加勾选「同名文件替换原文档（版本加 1）」。
- 新标签「检索回归」：用例表格（问题、应找到的文档，多选），检索设置，「保存」「运行回归」；结果逐条显示通过与否、找到了什么、缺了什么；`stale` 时提示“文档改过，建议重跑回归”。
- 「用模板写文档」按钮。

## 存储

```sql
ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS owner text;
ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS valid_from date;
ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS valid_until date;
ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS version integer NOT NULL DEFAULT 1;
ALTER TABLE knowledge_documents ADD COLUMN IF NOT EXISTS uploaded_by text;
-- knowledge_document_versions(tenant_id, document_id, version, size_bytes, uploaded_by, uploaded_at)
-- knowledge_hits(tenant_id, kb_id, document_id, run_id, at)，主键 (tenant_id, run_id, document_id)
-- knowledge_regressions(tenant_id, kb_id, cases jsonb, retrieval jsonb, top_k, min_score, updated_by, updated_at, last_run jsonb)
ALTER TABLE knowledge_bases ADD COLUMN IF NOT EXISTS docs_changed_at timestamptz;
```

`knowledge_bases.docs_changed_at` 是知识库文档最近一次变化的时间（上传、替换、删除、改日期都会更新），`stale` 由它和 `last_run.at`、用例的 `updated_at` 比较得出。
