# 知识库接口

后端和前端按这份约定并行开发。本文只写新增和改变的部分，没提到的接口和字段保持不变；字段名 snake_case，错误格式同 [apps-and-skills.md](apps-and-skills.md)：`{"detail": "中文说明"}`，校验错误 422。

## 概念

- **知识库**：一组文档，用同一个向量模型、同一套切分设置。平台把文档切成片段、算向量、分词，存进 Postgres（pgvector 存向量，全文索引存关键词），按问题找最相关的片段。
- **用在哪里**：编排应用的「知识检索」节点（`knowledge`）、「智能体」节点（`agent`，知识库作为它可以调用的检索工具），以及 etl 应用「出方案」节点的设置（方案引用资料并标出处）。
- **检索方式**（第二期起）：向量、关键词、混合（两者加权），混合之后可以再用重排模型排一次，见[检索](#检索)。按标签过滤、自定义词典放后面。

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
  "score": 0.82,                       // 最终分数，0–1，越大越相关；含义看检索方式，见“检索”一节
  "scores": {"vector": 0.82, "keyword": 0.5, "rerank": null}   // 各路的分数，没走这一路为 null
}

// Retrieval：检索方式，检索接口、知识检索节点、智能体节点共用
{
  "mode": "hybrid",                    // "vector" | "keyword" | "hybrid"
  "vector_weight": 0.7,                // 只在 hybrid 时用，0–1；关键词的权重是 1 − vector_weight
  "rerank": null                       // 或 {"model": "company-rerank", "candidates": 20}，见“重排”
}
```

不写 `retrieval`（或者其中某个字段）时取 `{"mode": "vector", "vector_weight": 0.7, "rerank": null}`，和第一期的行为一样，所以已有的应用不用改。页面和新节点的默认值是 `hybrid`（来自 `GET /knowledge-bases/options` 和 `GET /node-types`）。

## 接口

| 方法 | 路径 | 请求体 | 返回 |
| --- | --- | --- | --- |
| GET | `/knowledge-bases/options` | | `{"embedding_models": ["mock-embed", "company-embed"], "default_embedding_model": "mock-embed", "chunking": {"size": 800, "overlap": 100}, "retrieval": {"mode": "hybrid", "vector_weight": 0.7, "rerank": null}, "rerank_models": [{"model": "mock-rerank", "kind": "rerank"}, {"model": "company", "kind": "llm"}]}`；`retrieval` 是页面的默认值，`rerank_models` 见“重排” |
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
| POST | `/knowledge-bases/{kb}/search` | `{"query", "top_k"?: 5, "min_score"?: 0.3, "retrieval"?: Retrieval}` | `{"hits": Hit[], "duration_ms": 120}`；页面上的“检索测试” |

上传的限制：只收 `.pdf`、`.docx`、`.md`、`.txt`（按扩展名，大小写不限），每个文件最多 20 MB，一次最多 20 个文件，一个知识库最多 500 个文档；超出时整次上传 422，`detail` 写明哪个文件、什么原因，不保存任何文件。`query` 1–2000 字符，`top_k` 1–20，`min_score` 0–1；`retrieval` 的校验见“检索”一节，不合格 422。

## 文档怎么处理

1. 上传：原文件存到 `AEP_KNOWLEDGE_DIR`（默认 `/data/knowledge`）下 `<kb_id>/<document_id>/原文件名`，记一条 `queued` 的文档，立即返回 202。
2. 后台处理：API 进程里有一个处理任务，从数据库领 `queued` 的文档（`FOR UPDATE SKIP LOCKED`，多副本也不会重复处理），改成 `processing`，处理中每算完一批向量就刷新 `updated_at`。处理任务每分钟（以及启动时）把 `updated_at` 超过 10 分钟没变的 `processing` 文档改回 `queued`，所以进程崩溃后不管多久重启，卡住的文档都会被重新处理。
3. 抽文字：txt、md 按 UTF-8（失败再试 GB18030）；pdf 用 pypdf 按页抽，记页码；docx 用 python-docx 读段落和表格（表格每行用 ` | ` 连接），Word 的标题样式进标题路径。抽不出文字时 `error`，原因“没有可提取的文字（可能是扫描件）”。
4. 切分：先按标题和空行分段，段太长再按句号、问号、感叹号、分号（中英文）和换行切，最后硬切；相邻片段重叠 `overlap` 个字符；片段不跨 PDF 页、不跨标题。少于 20 个非空白字符的片段丢掉。
5. 算向量：每批 10 段，失败的批次重试 2 次（间隔 2 秒、4 秒），还是失败就整篇 `error`，原因写网关的报错；已写入的片段删掉。
6. 写入：一篇文档的片段在一个事务里替换，成功后 `ready`。

## 检索

只检索 `ready` 的文档。一次检索分四步：每一路取候选 → 合并打分 → 重排（可选）→ 按 `min_score` 过滤、取前 `top_k`。

### 两路候选

- **向量**：用知识库的向量模型算问题的向量，按余弦距离取候选。分数 `scores.vector` 是余弦相似度（0–1，小于 0 按 0）。
- **关键词**：问题和片段用同一个分词器（见“分词”）切成词，在全文索引里找含有这些词的片段（任意一个词即可），按含词多少排序取候选。分数 `scores.keyword` 是**问题里的词有多大比例出现在片段里**（去重后计数，0–1）；比如问题切出 4 个词、片段里有 3 个，就是 0.75。问题切不出词（全是停用词或标点）时这一路没有候选。关键词这一路不调向量模型，所以 `keyword` 模式在向量模型不可用时也能用。
- 每一路取的候选数 `pool = max(20, top_k × 4)`，开了重排时再和 `rerank.candidates` 取大。

### 合并打分

- `vector`：只走向量；`score = scores.vector`，`scores.keyword` 为 null。
- `keyword`：只走关键词；`score = scores.keyword`，`scores.vector` 为 null。
- `hybrid`：两路候选取并集，**并集里每个片段两种分数都要算**（只被一路找到的片段，另一路的分数现算，不当作 0）；`score = vector_weight × vector + (1 − vector_weight) × keyword`。
- 同分时按 `chunk_id` 排，结果确定。
- 多个知识库一起检索时（节点里），每个库各自取候选、合并打分，然后合在一起按 `score` 排序；重排在合并之后做一次。

### 重排

- `rerank: {"model": "<别名>", "candidates": 20}`：把合并后排名前 `candidates` 个（5–50，且不少于 `top_k`）交给重排模型重新打分，`score = scores.rerank`（0–1），然后过滤、取前 `top_k`。三种模式都可以开重排。
- 两类别名，`GET /knowledge-bases/options` 的 `rerank_models` 里用 `kind` 区分：
  - `kind: "rerank"`：专门的重排模型，由设置 `AEP_RERANK_MODELS`（默认 `["mock-rerank"]`）列出，经网关的 `/rerank` 接口调用（Cohere 格式：`query`、`documents`、`top_n`，返回 `results[{index, relevance_score}]`）。新增网关别名 `company-rerank`（`COMPANY_RERANK_MODEL`，与 `company` 共用地址和密钥）。**公司网关有没有重排模型还没确认**，所以默认不在列表里；确认有了，在 `.env` 设好 `COMPANY_RERANK_MODEL`，再把 `company-rerank` 加进 `AEP_RERANK_MODELS`。
  - `kind: "llm"`：用对话模型打分，平台上能用的对话模型（和 `AppDetail.options.models` 相同）都列出来。一次调用：把问题和编号的候选（每段截到 600 字符）发给模型，要求只输出 JSON `{"scores": [{"i": 1, "score": 0–10}]}`；没打分的候选算 0；`scores.rerank = score / 10`。输出不合格式时重试一次，仍不合格按失败处理。
  - `mock-rerank`：测试和 CI 用，**和 `mock-embed` 一样不经过网关**，在本地算：问题的字符二元组有多大比例出现在候选里（0–1）。
- 发给重排模型的文字是 `标题路径 + 换行 + 正文`（没有标题路径时只有正文）。
- 重排失败（网关报错、超时、格式不合）就是检索失败：检索接口返回 502，`detail` 写原因；节点里是 `knowledge_error`。不悄悄退回不重排的结果，免得用户以为重排生效了。

### 校验

`mode` 只能是三种之一；`vector_weight` 0–1；`rerank` 为 null 或对象，`model` 必须在 `rerank_models` 里，`candidates` 5–50 且不少于 `top_k`（不写取 `max(20, top_k)`）。检索接口不合格 422；节点里在保存草稿时报，`loc` 指到 `retrieval` 下的字段。

### `min_score` 怎么定

`min_score` 过滤的是最终的 `score`，所以不同方式下分数的意思不一样：向量是余弦相似度，关键词是问题里的词出现的比例，混合是两者加权，开了重排就是重排分数。默认值仍是 0.3；页面上的“检索测试”同时显示 `scores` 里的各路分数，方便调。

### 分词

- 新模块 `aep.knowledge.keywords`，函数 `terms(text) -> list[str]`：先做 NFKC 规范化、转小写，用 jieba 的搜索引擎模式（`cut_for_search`，长词会再切出短词，召回更好）分词，去掉不含字母、数字、汉字的词和停用词（内置一份常用中英文停用词表，放在包里的文本文件），保持原顺序。
- 片段的关键词：对 `标题路径 + 换行 + 正文` 取 `terms`，用空格连起来存进 `keywords` 列；Postgres 用 `to_tsvector('simple', keywords)` 建全文索引。`simple` 配置不再切分，只按空格分开，所以中文词也能对上。
- 问题的关键词：同一个 `terms`，去重，最多取前 32 个。
- 不用 Postgres 的中文分词扩展（zhparser、pg_jieba）：Azure 的托管 Postgres 装不了，本地和 Azure 要用同一套做法。
- jieba 第一次分词要加载词典（约 1 秒），API 进程启动时在后台线程里预先加载；词典缓存写到可写的临时目录，不写进代码目录。

## 编排应用：知识检索节点

在 [flow-apps.md](flow-apps.md) 的节点类型里新增：

**knowledge 知识检索**

```jsonc
{"knowledge_bases": ["kb-uuid-1", "kb-uuid-2"],   // 1–5 个
 "query": "{{start.question}}",                   // 必填，最多 2000 字符；引用替换后为空时产出为空、不报错
 "top_k": 5,                                      // 1–20
 "min_score": 0.3,                                // 0–1
 "retrieval": {"mode": "hybrid", "vector_weight": 0.7, "rerank": null}}   // 可选，Retrieval；不写等于只用向量
```

产出：
- `hits`：`Hit[]`，每项多一个 `index`（引用编号）。编号在**一次运行里统一分配**：从 1 开始，同一个片段在这次运行里不管被哪个知识检索节点、第几次命中，都用同一个编号；新片段接着已用过的最大编号往下编。所以一个提示词里放了几个检索节点的 `context`，`[n]` 也不会撞号。编号记在运行状态里（变量池之外，比如 `citations: {chunk_id: index}`）。单节点试跑从 1 开始。
- `context`：可以直接放进提示词的文字，每段形如 `[1]《退换货政策.pdf》第三章 退货 > 3.2 期限（第 5 页）\n片段内容`，段之间空一行；没有命中时为 `""`。

无分支。可以设 `on_error`，出错类型新增 `knowledge_error`（知识库不存在、向量模型报错）。`GET /node-types` 里 `category` 为 `"tool"`，`error_handling: true`，默认设置 `{"knowledge_bases": [], "query": "", "top_k": 5, "min_score": 0.3, "retrieval": {"mode": "hybrid", "vector_weight": 0.7, "rerank": null}}`；流程视图里 `kind` 为 `tool`。

- 保存草稿时检查知识库存在；导出文件的 `requires` 新增 `knowledge_bases: [{kb_id, name}]`，导入时缺失的知识库在 `problems` 里报 `{"kind": "knowledge_base", "kb_id", "name", "message": "没有这个知识库，请在节点里重新选择"}`。
- 单节点试跑（`POST /apps/{app}/nodes/{node}/test`）支持这个节点，`rendered` 里有替换后的 `query`。
- `AppDetail.options` 新增 `rerank_models`（同 `GET /knowledge-bases/options`），给节点表单的重排下拉框用。
- `AppDetail.options` 新增 `knowledge_bases: [{"kb_id", "name", "document_count"}]`，给节点表单的下拉框用。
- 模板新增一个 `knowledge-qa`（知识问答）：开始（question）→ 知识检索 → 调用模型（提示词里放 `{{kb.context}}`，要求按 `[n]` 标出处、资料里没有就直说）→ 结束（`answer`）。`needs` 含 `"knowledge_base"`。模板里的知识库留空，过不了保存检查，所以用 `needs` 含 `knowledge_base` 的模板新建应用时，和带缺失的导入一样：版本 1 是空白应用，模板的配置放进草稿，返回的 `AppDetail` 里有这份草稿；用户在编辑页选好知识库、保存、发布。前端新建后直接进编辑页。

## 编排应用：智能体调用知识库

[flow-apps.md](flow-apps.md) 的 `agent` 节点新增设置 `knowledge`：

```jsonc
{"tools": [],                                     // 改为 0–10 个；tools 和 knowledge 至少要有一个
 "knowledge": {                                   // 可选，null 或不写表示不用知识库
   "knowledge_bases": ["kb-uuid-1"],              // 1–5 个，保存时检查存在
   "top_k": 5,                                    // 每次检索返回几段，1–10
   "min_score": 0.3,
   "retrieval": {"mode": "hybrid", "vector_weight": 0.7, "rerank": null}},
 ...}
```

- **给模型的工具**：设了 `knowledge` 时，工具列表里多一项，代号固定为 `kb`（数据源工具的代号是 `t1`、`t2`…，不会撞），说明写成：在知识库里检索资料；列出每个知识库的名称和说明（`description`，所以知识库的说明要写清楚里面是什么）；参数 `{"query": "要查的内容", "knowledge_bases": ["知识库名称"]}`，`knowledge_bases` 可选，不写查全部，写了只查这几个（按名称对，名称不在列表里就报给模型重来，不算一步）。
- 系统提示词里加一句：用到检索到的资料时，在回答里按资料前的 `[n]` 标出处；资料里没有的不要编。
- **执行**：一次检索算一步（计入 `max_steps`），检索方式按节点设置。命中的片段按运行里统一的编号规则分配 `index`（和知识检索节点共用编号表），返回给模型的内容就是 `context` 格式的文字（截到 8000 字符，同 `MAX_TOOL_RESULT`）；没有命中时返回“没有找到相关资料”。检索出错时把错误告诉模型、这一步记 `error`，不让整个节点失败（和数据源工具出错一样）。
- **产出**：`text`、`steps` 照旧，`steps` 里检索那一步是 `{"tool": "search_knowledge", "datasource": null, "args": {"query", "knowledge_bases"}, "error": null, "hits": 3}`（`hits` 是命中条数，只有检索步骤有）；新增 `hits`：这个节点所有检索命中的片段（`Hit + index`，按 `index` 去重排序），没有为 `[]`。命中的片段进运行的 `sources`，`node` 是这个 agent 节点。
- **其他地方一并算上 agent 的知识库**：保存检查（`loc` 指到 `knowledge` 下）、导出的 `requires.knowledge_bases`、导入时缺失的知识库、删除知识库时“被应用当前版本引用”的 409。
- `GET /node-types` 里 agent 的默认设置加 `"knowledge": null`。
- 模板新增 `knowledge-agent`（知识库智能体）：开始（question）→ 智能体（`tools: []`，`knowledge` 选知识库，`task` 要求先检索再回答、按 `[n]` 标出处，`max_steps` 6）→ 结束（`answer`）。`needs` 含 `"knowledge_base"`，新建时和 `knowledge-qa` 一样走“版本 1 空白、模板配置进草稿”。

和知识检索节点怎么选：问题固定、每次都要查资料，用“知识检索 + 调用模型”，步骤可控、便宜；要模型自己决定查不查、查几次、换个说法再查，或者要同时查数据源和知识库，用智能体。

### 运行记录里的出处

`GET /runs/{id}` 新增 `sources`：这次运行里分配过编号的全部片段，按 `index` 排序，没有时为 `[]`。每项是 `Hit + index`，另加最后一次命中它的节点 `node` 和节点标题 `node_title`。

```jsonc
"sources": [{"index": 1, "node": "kb", "node_title": "检索售后政策", ...Hit}]
```

前端在结果下面显示“引用来源”，结果文字里的 `[n]` 显示成可以点的角标，点开看片段原文，并能下载原文件。

## etl 应用：方案引用资料

- 「出方案」（`plan`）节点的设置新增：
  - `{"key": "knowledge_bases", "label": "参考知识库", "type": "knowledge_bases", "help": "出方案前按需求检索这些知识库，方案里标出依据"}`，默认 `[]`。`Setting.type` 新增 `knowledge_bases`（多选，选项来自 `AppDetail.options.knowledge_bases`）。
  - `{"key": "knowledge_top_k", "label": "检索片段数", "type": "integer", "min": 1, "max": 20}`，默认 5。
  - （第二期）`{"key": "knowledge_mode", "label": "检索方式", "type": "enum", "choices": [{"value": "hybrid", "label": "混合"}, {"value": "vector", "label": "向量"}, {"value": "keyword", "label": "关键词"}]}`，默认 `hybrid`，`vector_weight` 固定 0.7、不重排。已保存的 etl 配置里没有这个键，按默认值 `hybrid` 算（etl 的设置本来就按默认值补齐）。
- 选了知识库时，出方案前用用户的需求检索，命中的片段按 `[n]` 编号放进提示词；方案的 `summary`、`assumptions`、目标表和字段的说明里可以用 `[n]` 标出处。
- 方案新增 `references`：方案里实际出现过的编号对应的片段（`Hit + index`），按编号排序；编号超出范围的标记在校验时去掉。没选知识库或没有引用时为 `[]`。
- 方案卡片显示“依据的资料”，`[n]` 同上显示成角标。检索失败不阻止出方案：`references` 为 `[]`，流程视图里这一步记一条警告。

## 存储

```sql
CREATE EXTENSION IF NOT EXISTS vector;
-- knowledge_bases(kb_id, tenant_id, name, description, embedding_model, chunk_size, chunk_overlap, created_at, updated_at)
-- knowledge_documents(document_id, kb_id, tenant_id, name, kind, size_bytes, path, status, error, chunk_count, created_at, updated_at)
-- knowledge_chunks(chunk_id, document_id, kb_id, tenant_id, position, text, heading, page, embedding vector(1024),
--                  keywords text,            -- 第二期：空格连起来的词；NULL 表示还没分词
--                  tsv tsvector GENERATED ALWAYS AS (to_tsvector('simple', coalesce(keywords, ''))) STORED)
CREATE INDEX ... ON knowledge_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX ... ON knowledge_chunks USING gin (tsv);
```

第二期的两列用 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 加在 `SCHEMA` 里，已有的库启动时自动升级。新写入的片段在 `replace_chunks` 里一起写 `keywords`。已有片段的 `keywords` 是 NULL，由后台处理任务补算：启动时和之后每分钟，每次取 500 个 `keywords IS NULL` 的片段分词写回，直到没有为止（只分词，不重算向量）；补算完成前，这些片段在关键词这一路找不到，向量这一路不受影响。

业务表都带 `tenant_id`（默认 `'default'`）。测试用内存实现（Python 里算余弦），需要 pgvector 的测试读 `AEP_TEST_DATABASE_URL`。
