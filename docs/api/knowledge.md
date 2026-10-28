# 知识库接口

后端和前端按这份约定并行开发。本文只写新增和改变的部分，没提到的接口和字段保持不变；字段名 snake_case，错误格式同 [apps-and-skills.md](apps-and-skills.md)：`{"detail": "中文说明"}`，校验错误 422。

## 概念

- **知识库**：一组文档，用同一个向量模型、同一套切分设置。平台把文档切成片段、算向量，存进 Postgres（pgvector），按问题找最相近的片段。
- **用在哪里**：编排应用的「知识检索」节点（`knowledge`），以及 etl 应用「出方案」节点的设置（方案引用资料并标出处）。
- 第一版只做向量检索。混合检索（关键词）、重排、按标签过滤、知识库作为 Agent 的工具放后面。

## 向量模型

- 向量一律经模型网关算（`aep.llm.embed`），代码里只写别名。新增两个别名：
  - `company-embed`：公司网关的 `text-embedding-v4`（`COMPANY_EMBED_MODEL`，默认 `openai/text-embedding-v4`，与 `company` 共用 `COMPANY_API_BASE`、`COMPANY_API_KEY`）。
  - `mock-embed`：测试和 CI 用。**例外**：它不经过网关，由 `aep.llm.embed` 在本地算（字符二元组哈希到 1024 维再归一化，结果确定，相近文字的向量也相近）。
- 维度固定 1024：调用时传 `dimensions=1024`，返回维度不对按失败处理。
- 可选的别名由设置 `AEP_EMBEDDING_MODELS`（默认 `["mock-embed", "company-embed"]`）决定，新建知识库的默认值是 `AEP_DEFAULT_EMBEDDING_MODEL`（默认 `mock-embed`）。
- 每批最多 10 段文字。

## 数据结构

```jsonc
// KnowledgeBase
{
  "kb_id": "uuid",
  "name": "售后政策",                  // 1–50 字符，不重名
  "description": "",                   // 最多 500 字符
  "embedding_model": "company-embed",  // 有文档后不能改
  "chunking": {"size": 800, "overlap": 100},   // size 200–2000 字符，overlap 0 到 size 的一半
  "document_count": 12,
  "chunk_count": 340,
  "status_counts": {"queued": 0, "processing": 1, "ready": 10, "error": 1},
  "created_at": "……", "updated_at": "……"
}

// Document
{
  "document_id": "uuid",
  "kb_id": "uuid",
  "name": "退换货政策.pdf",            // 上传时的文件名；同一知识库里重名时加 (2)、(3)
  "kind": "pdf" | "docx" | "md" | "txt",
  "size_bytes": 182344,
  "status": "queued" | "processing" | "ready" | "error",
  "error": null,                       // 失败原因，如“没有可提取的文字（可能是扫描件）”
  "chunk_count": 28,
  "created_at": "……", "updated_at": "……"
}

// Chunk
{
  "chunk_id": "uuid",
  "document_id": "uuid",
  "position": 0,                       // 在文档里的顺序，从 0 开始
  "text": "……",
  "heading": "第三章 退货 > 3.2 期限",  // Markdown / Word 的标题路径，没有为 ""
  "page": 5                            // PDF 的页码（从 1 开始），其他格式为 null
}

// Hit：检索结果
{
  "kb_id": "uuid", "kb_name": "售后政策",
  "document_id": "uuid", "document_name": "退换货政策.pdf",
  "chunk_id": "uuid", "position": 3,
  "text": "……", "heading": "……", "page": 5,
  "score": 0.82                        // 余弦相似度，0–1，越大越相近
}
```

## 接口

| 方法 | 路径 | 请求体 | 返回 |
| --- | --- | --- | --- |
| GET | `/knowledge-bases/options` | | `{"embedding_models": ["mock-embed", "company-embed"], "default_embedding_model": "mock-embed", "chunking": {"size": 800, "overlap": 100}}` |
| GET | `/knowledge-bases` | | `KnowledgeBase[]`，按名称排序 |
| POST | `/knowledge-bases` | `{"name", "description"?, "embedding_model"?, "chunking"?}` | 201 `KnowledgeBase`；重名 409；别名不在列表里 422 |
| GET | `/knowledge-bases/{kb}` | | `KnowledgeBase` |
| PATCH | `/knowledge-bases/{kb}` | `{"name"?, "description"?, "embedding_model"?, "chunking"?}` | `KnowledgeBase`；有文档时改 `embedding_model` 返回 409；改 `chunking` 只影响之后上传或重建的文档 |
| DELETE | `/knowledge-bases/{kb}` | | 204，连同文档、片段和原文件一起删除；有应用的**当前版本**引用它时 409，`detail` 列出应用 |
| GET | `/knowledge-bases/{kb}/documents` | | `Document[]`，新的在前 |
| POST | `/knowledge-bases/{kb}/documents` | `multipart/form-data`，字段 `files`（可多个） | 202 `Document[]`，状态 `queued` |
| GET | `/knowledge-bases/{kb}/documents/{doc}` | | `Document` |
| DELETE | `/knowledge-bases/{kb}/documents/{doc}` | | 204，连同片段和原文件 |
| GET | `/knowledge-bases/{kb}/documents/{doc}/file` | | 原文件下载（`Content-Disposition` 带原文件名） |
| GET | `/knowledge-bases/{kb}/documents/{doc}/chunks?offset=0&limit=50` | | `{"total": 28, "items": Chunk[]}`，按 `position`；`limit` 最多 200 |
| POST | `/knowledge-bases/{kb}/documents/{doc}/reindex` | | 202 `Document`，重新切分、算向量 |
| POST | `/knowledge-bases/{kb}/reindex` | | 202 `{"queued": 12}`，全部文档重建 |
| POST | `/knowledge-bases/{kb}/search` | `{"query", "top_k"?: 5, "min_score"?: 0.3}` | `{"hits": Hit[], "duration_ms": 120}`；页面上的“检索测试” |

上传的限制：只收 `.pdf`、`.docx`、`.md`、`.txt`（按扩展名，大小写不限），每个文件最多 20 MB，一次最多 20 个文件，一个知识库最多 500 个文档；超出时整次上传 422，`detail` 写明哪个文件、什么原因，不保存任何文件。`query` 1–2000 字符，`top_k` 1–20，`min_score` 0–1。

## 文档怎么处理

1. 上传：原文件存到 `AEP_KNOWLEDGE_DIR`（默认 `/data/knowledge`）下 `<kb_id>/<document_id>/原文件名`，记一条 `queued` 的文档，立即返回 202。
2. 后台处理：API 进程里有一个处理任务，从数据库领 `queued` 的文档（`FOR UPDATE SKIP LOCKED`，多副本也不会重复处理），改成 `processing`。进程重启时，`processing` 超过 10 分钟没更新的文档重新排队。
3. 抽文字：txt、md 按 UTF-8（失败再试 GB18030）；pdf 用 pypdf 按页抽，记页码；docx 用 python-docx 读段落和表格（表格每行用 ` | ` 连接），Word 的标题样式进标题路径。抽不出文字时 `error`，原因“没有可提取的文字（可能是扫描件）”。
4. 切分：先按标题和空行分段，段太长再按句号、问号、感叹号、分号（中英文）和换行切，最后硬切；相邻片段重叠 `overlap` 个字符；片段不跨 PDF 页、不跨标题。少于 20 个非空白字符的片段丢掉。
5. 算向量：每批 10 段，失败的批次重试 2 次（间隔 2 秒、4 秒），还是失败就整篇 `error`，原因写网关的报错；已写入的片段删掉。
6. 写入：一篇文档的片段在一个事务里替换，成功后 `ready`。

## 检索

- 用知识库的向量模型算问题的向量，按余弦距离取前 `top_k` 个，过滤掉 `score < min_score` 的。
- 多个知识库一起检索时（节点里），每个库各取 `top_k`，合在一起按 `score` 排序再取前 `top_k`。
- 只检索 `ready` 的文档。

## 编排应用：知识检索节点

在 [flow-apps.md](flow-apps.md) 的节点类型里新增：

**knowledge 知识检索**

```jsonc
{"knowledge_bases": ["kb-uuid-1", "kb-uuid-2"],   // 1–5 个
 "query": "{{start.question}}",                   // 必填，最多 2000 字符；引用替换后为空时产出为空、不报错
 "top_k": 5,                                      // 1–20
 "min_score": 0.3}                                // 0–1
```

产出：
- `hits`：`Hit[]`，每项多一个 `index`（从 1 开始的编号）。
- `context`：可以直接放进提示词的文字，每段形如 `[1]《退换货政策.pdf》第三章 退货 > 3.2 期限（第 5 页）\n片段内容`，段之间空一行；没有命中时为 `""`。

无分支。可以设 `on_error`，出错类型新增 `knowledge_error`（知识库不存在、向量模型报错）。`GET /node-types` 里 `category` 为 `"tool"`，`error_handling: true`，默认设置 `{"knowledge_bases": [], "query": "", "top_k": 5, "min_score": 0.3}`；流程视图里 `kind` 为 `tool`。

- 保存草稿时检查知识库存在；导出文件的 `requires` 新增 `knowledge_bases: [{kb_id, name}]`，导入时缺失的知识库在 `problems` 里报 `{"kind": "knowledge_base", "kb_id", "name", "message": "没有这个知识库，请在节点里重新选择"}`。
- 单节点试跑（`POST /apps/{app}/nodes/{node}/test`）支持这个节点，`rendered` 里有替换后的 `query`。
- `AppDetail.options` 新增 `knowledge_bases: [{"kb_id", "name", "document_count"}]`，给节点表单的下拉框用。
- 模板新增一个 `knowledge-qa`（知识问答）：开始（question）→ 知识检索 → 调用模型（提示词里放 `{{kb.context}}`，要求按 `[n]` 标出处、资料里没有就直说）→ 结束（`answer`）。`needs` 含 `"knowledge_base"`，新建时知识库留空，用户在节点里选（保存时会报“至少选一个知识库”）。

### 运行记录里的出处

`GET /runs/{id}` 新增 `sources`：这次运行里每个知识检索节点**最近一次**的命中，没有时为 `[]`。

```jsonc
"sources": [{"node": "kb", "title": "检索售后政策", "hits": [Hit + index]}]
```

前端在结果下面显示“引用来源”，结果文字里的 `[n]` 显示成可以点的角标，点开看片段原文，并能下载原文件。

## etl 应用：方案引用资料

- 「出方案」（`plan`）节点的设置新增：
  - `{"key": "knowledge_bases", "label": "参考知识库", "type": "knowledge_bases", "help": "出方案前按需求检索这些知识库，方案里标出依据"}`，默认 `[]`。`Setting.type` 新增 `knowledge_bases`（多选，选项来自 `AppDetail.options.knowledge_bases`）。
  - `{"key": "knowledge_top_k", "label": "检索片段数", "type": "integer", "min": 1, "max": 20}`，默认 5。
- 选了知识库时，出方案前用用户的需求检索，命中的片段按 `[n]` 编号放进提示词；方案的 `summary`、`assumptions`、目标表和字段的说明里可以用 `[n]` 标出处。
- 方案新增 `references`：方案里实际出现过的编号对应的片段（`Hit + index`），按编号排序；编号超出范围的标记在校验时去掉。没选知识库或没有引用时为 `[]`。
- 方案卡片显示“依据的资料”，`[n]` 同上显示成角标。检索失败不阻止出方案：`references` 为 `[]`，流程视图里这一步记一条警告。

## 存储

```sql
CREATE EXTENSION IF NOT EXISTS vector;
-- knowledge_bases(kb_id, tenant_id, name, description, embedding_model, chunk_size, chunk_overlap, created_at, updated_at)
-- knowledge_documents(document_id, kb_id, tenant_id, name, kind, size_bytes, path, status, error, chunk_count, created_at, updated_at)
-- knowledge_chunks(chunk_id, document_id, kb_id, tenant_id, position, text, heading, page, embedding vector(1024))
CREATE INDEX ... ON knowledge_chunks USING hnsw (embedding vector_cosine_ops);
```

业务表都带 `tenant_id`（默认 `'default'`）。测试用内存实现（Python 里算余弦），需要 pgvector 的测试读 `AEP_TEST_DATABASE_URL`。
