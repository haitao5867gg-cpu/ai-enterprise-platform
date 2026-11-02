# 模型接口

后端和前端按这份约定并行开发。字段名 snake_case，时间是 ISO 8601 字符串。错误格式同 [datasources.md](datasources.md)：`{"detail": "中文说明"}`；请求体不合格也返回 422，`detail` 是一句中文。

落在哪一层：**平台资源 + 网关配置**。模型仍然一律经 LiteLLM 网关调用，业务代码和应用配置里仍然只写模型别名；这一版只是让别名可以在页面上增删，并让平台记下每次调用。应用不需要任何改动。

## 概念

- **模型**：网关上的一个别名（`alias`），节点和知识库按别名调用它。来源有两种：
  - `config`：写在 `infra/litellm/config.yaml` 里的（`mock`、`company`、`company-embed` 等）。页面上只读，改它们仍然改配置文件和 `.env`。
  - `page`：在页面上新增的。网关把它存在自己的数据库里（LiteLLM 的 `store_model_in_db`），加上、改完立刻能调，不用重启。
- **用途**（`purpose`）：`chat`（对话，节点用）、`embedding`（向量，知识库用）、`rerank`（重排）。页面上只能新增 `chat` 和 `embedding`；重排模型仍在配置文件里加。
- **key 不进平台**：页面上填的 key 直接交给网关，网关加密后存进它自己的库（用 `LITELLM_SALT_KEY` 加密）。平台的库、日志和接口都不保存、不返回 key，只记 key 是「页面上填的」还是「服务器环境变量 `XXX_API_KEY`」。这和「凭据留在连接器那边」是同一条约定。
- **显示名和用途**记在网关里这个模型的 `model_info` 上（`aep_display_name`、`aep_purpose`、`aep_description`、`aep_provider`、`aep_key`），平台不另存一份，所以网关是模型清单的唯一来源。配置文件里的模型也在 `model_info` 里写上这几项。
- **平台默认模型**：没指定模型的节点用默认对话模型，新建知识库默认用默认向量模型。页面上可以改；没改过时分别是 `AEP_DEFAULT_MODEL`、`AEP_DEFAULT_EMBEDDING_MODEL`。
- **调用记录**：平台经 `aep.llm` 发出的每次模型调用（对话、向量、对话模型重排）都记一行：时间、别名、哪个运行和节点、token、费用、成败。模型页的调用量来自这里，以后的运营看板和预算也用它。

## 数据结构

```jsonc
// Model
{
  "alias": "claude-sonnet",              // 小写字母开头，小写字母、数字、- 和 _，2–40 个字符；登记后不能改
  "display_name": "Claude Sonnet 5.5",    // 1–60 字符；配置文件里没写时等于 alias
  "purpose": "chat" | "embedding" | "rerank",
  "provider": "anthropic" | "openai" | "azure" | "bedrock" | "dashscope" | "deepseek" | "openai_compatible" | "mock" | "other",
  "model": "claude-sonnet-5-5",           // 供应商那边的型号（去掉 LiteLLM 的 provider/ 前缀）；配置文件里引用环境变量的，是网关解析后的值
  "api_base": null | "https://aigw.example.com/v1",   // 只有 openai_compatible、azure 和填了地址的 openai 有
  "key": {"source": "page" | "env" | "none" | "unknown", "env": null | "ANTHROPIC_API_KEY"},
                                          // 配置文件里的模型是 unknown（平台看不到配置文件）
  "description": "",                      // 最多 500 字符
  "source": "config" | "page",
  "is_default": false,                    // 是不是它这个用途的平台默认模型
  "allowed": true,                        // 平台默认的数据外发策略允不允许运行用它（AEP_ALLOWED_MODELS 为空时都允许）
  "context_window": 1000000 | null,       // 网关知道的最大输入 token
  "price": {"input": 3.0, "output": 15.0} | null,   // 美元 / 百万 token，网关知道时才有
  "usage": {                              // 近 30 天，来自调用记录
    "calls": 12, "errors": 1,
    "prompt_tokens": 34000, "completion_tokens": 5200,
    "cost_usd": 0.18 | null,              // 网关算不出费用的调用不计入；一次都算不出时为 null
    "last_call_at": "……" | null,
    "last_error": null | "认证失败：key 不对或已失效。"   // 最近一次调用失败时的说明（下面「问题分类」里的一句）；最近一次成功时为 null
  }
}

// ModelList
{
  "gateway": "ok" | "no_db" | "down",     // no_db：网关没接数据库，只能看不能新增；down：网关连不上，models 为 []
  "models": [Model],                      // 先对话、再向量、再重排；同一用途里默认模型在前，其余按 alias
  "defaults": {"chat": "company", "embedding": "company-embed"}
}

// TestResult
{"ok": true, "latency_ms": 820, "reply": "OK" | null, "problem": null | {"code": "auth", "message": "认证失败：……"}}

// Usage
{"apps": [{"app": "retail-etl", "title": "零售数据 ETL", "version": 3, "nodes": ["生成方案", "写代码"], "via": "literal" | "default"}]}
```

## 供应商

新增时按供应商要填的字段。`model` 是供应商的型号，平台按下表加上 LiteLLM 的前缀。

| provider | 页面名称 | 前缀 | 要填 | key |
| --- | --- | --- | --- | --- |
| `anthropic` | Anthropic（Claude） | `anthropic/` | 型号，如 `claude-sonnet-5-5` | 必填 |
| `openai` | OpenAI | `openai/` | 型号；地址可选 | 必填 |
| `azure` | Azure OpenAI | `azure/` | 部署名、地址、`api_version` | 必填 |
| `bedrock` | AWS Bedrock | `bedrock/` | 型号、`aws_region` | 不填：用网关环境里的 AWS 凭据 |
| `dashscope` | 通义千问（DashScope） | `dashscope/` | 型号 | 必填 |
| `deepseek` | DeepSeek | `deepseek/` | 型号 | 必填 |
| `openai_compatible` | OpenAI 兼容网关 | `openai/` | 型号、地址 | 必填 |

`mock` 和 `other` 只出现在配置文件的模型上。

key 两种填法：
- `{"source": "page", "value": "sk-..."}`：当场粘贴，1–500 个可打印字符。
- `{"source": "env", "env": "ANTHROPIC_API_KEY"}`：网关所在服务器上的环境变量名。只能是大写字母、数字和 `_`，以 `_API_KEY` 结尾，不能以 `LITELLM_` 或 `AEP_` 开头（防止把网关自己的密钥或平台的其他凭据发给模型地址）。变量要配进服务器的 `.env` 并重启网关后才生效。

地址（`api_base`）：只能是 `http` 或 `https`，规则同连接器地址检查的第 1～4 条（格式、不能带账号和敏感参数、不能是元数据地址、不能是平台内部服务），另外不能是回环地址；不用白名单，因为模型服务一般在外网。前后空白去掉，其余原样保存。

## 接口

| 方法 | 路径 | 请求体 | 返回 |
| --- | --- | --- | --- |
| GET | `/models` | | `ModelList` |
| POST | `/models` | `{"alias", "display_name", "purpose", "provider", "model", "api_base"?, "api_version"?, "aws_region"?, "key"?, "description"?}` | 201 `Model`。先检查重名（409），再在网关上加上并测试；测试没通过就从网关删掉，返回 422，什么都不留 |
| GET | `/models/{alias}` | | `Model` |
| PATCH | `/models/{alias}` | `{"display_name"?, "description"?, "model"?, "api_base"?, "api_version"?, "aws_region"?, "key"?}` | `Model`。只能改 `page` 模型；`alias`、`purpose`、`provider` 不能改（422）。只改显示名和说明不测试；改了型号、地址或 key 要测试，没通过返回 422，网关上的设置保持原样 |
| DELETE | `/models/{alias}` | | 204。只能删 `page` 模型。是平台默认模型、或者有应用的当前版本在用时 409 |
| POST | `/models/{alias}/test` | | 200 `TestResult`，没通过也是 200，看 `ok` |
| GET | `/models/{alias}/usage` | | `Usage` |
| PUT | `/models/defaults` | `{"chat"?, "embedding"?}` | `{"chat", "embedding"}`。别名要存在、用途要对，否则 422 |

不存在的别名一律 404。`gateway` 不是 `ok` 时，POST、PATCH、DELETE 返回 503。

测试怎么做：
- 对话模型：发一条「请只回复：OK」，`max_tokens` 16，`reply` 是回复的前 200 个字符。
- 向量模型：对「测试」取一次向量，要求 1024 维（平台的知识库按 1024 维建索引）；不是 1024 维、或者模型不支持指定维度时不通过。
- 重排模型：不测试，返回 `{"ok": false, "problem": {"code": "unsupported", ...}}`。
- 超时 30 秒。测试调用也记一行调用记录（不带运行）。

使用情况：看每个应用（编排应用和代码应用）的**当前版本**，规则同数据源（草稿、旧版本、已归档应用不算）。`literal`：节点写明了这个别名，或知识库检索用它重排，或检索的知识库用它做向量；`default`：节点没写模型，用的是平台默认模型。一个应用一行，`nodes` 是用到它的节点标题。

## 编排页面里的模型下拉框

`GET /apps/{app}` 的 `options` 新增（只增不改）：

```jsonc
"model_choices": [{"alias": "claude-sonnet", "label": "Claude Sonnet 5.5", "purpose": "chat", "allowed": true}]
```

`models`、`default_model` 不变。下拉框只列 `purpose` 为 `chat` 的模型，显示 `label`（和别名不同时写成「Claude Sonnet 5.5（claude-sonnet）」），外发策略不允许的标「（外发策略不允许）」。网关连不上时 `model_choices` 为 `[]`，页面退回按 `models` 显示。

知识库的向量模型选项（`GET /knowledge-bases/options` 的 `embedding_models`）在 `AEP_EMBEDDING_MODELS` 之外加上页面新增的向量模型，`default_embedding_model` 改为平台默认向量模型。新建知识库时同样接受这些模型。

## 问题分类

测试没通过、或者调用失败时的 `code` 和说明：

| code | 说明（`message`） |
| --- | --- |
| `auth` | 认证失败：key 不对或已失效。 |
| `quota` | 额度不足或被限流：请检查这个账号的余额和限额，稍后再试。 |
| `not_found` | 模型不存在：供应商那边没有这个型号，或者这个 key 没有权限用它。 |
| `bad_request` | 请求被拒绝：{供应商给的原因，最多 200 个字符}。 |
| `unreachable` | 连不上模型服务：网关所在的服务器访问不到这个地址（有的供应商不对部分地区开放）。 |
| `timeout` | 超时：模型在 30 秒内没有回复。 |
| `dimensions` | 向量维度不对：平台需要 1024 维，这个模型给的是 {n} 维，或者它不支持指定维度。 |
| `unsupported` | 重排模型暂不支持在页面上测试。 |
| `gateway` | 模型网关报错：{原因，最多 200 个字符}。 |

供应商的原始报错只写进平台日志，不带 key。

其他错误：

| 情况 | 状态 | `detail` |
| --- | --- | --- |
| 别名不存在 | 404 | 没有找到这个模型 |
| 别名已存在 | 409 | 已经有叫「{alias}」的模型了 |
| 改或删配置文件里的模型 | 409 | 「{alias}」写在网关的配置文件里，只能在配置文件里修改 |
| 删默认模型 | 409 | 「{alias}」是平台默认的{对话/向量}模型，请先换一个默认模型 |
| 删正在用的模型 | 409 | 这些应用正在用「{alias}」：{应用标题，用「、」隔开}。请先在应用里换掉它 |
| 网关没接数据库 | 503 | 模型网关没有接数据库，只能在配置文件里加模型。请按部署说明给网关配置 DATABASE_URL 和 LITELLM_SALT_KEY |
| 网关连不上 | 503 | 连不上模型网关 |
| 保存时测试没通过 | 422 | 测试没有通过：{message} |
| 供应商缺必填项 | 422 | {页面名称}要填{字段名}（型号、地址、API 版本、区域、key） |
| 填了供应商不用的项 | 422 | {页面名称}不用填{字段名}；Bedrock 填了 key：「AWS Bedrock不用填 key，网关用它所在环境里的凭据」 |
| 改别名、用途或供应商 | 422 | {别名/用途/供应商}登记后不能修改 |
| 新增重排模型 | 422 | 页面上只能新增对话模型和向量模型，重排模型请在配置文件里加 |
| key 写法不对 | 422 | 环境变量名要以 _API_KEY 结尾，只能有大写字母、数字和下划线，不能以 LITELLM_ 或 AEP_ 开头 |
| 地址不合格 | 422 | 元数据地址、平台内部服务同连接器地址检查对应的那一句；格式不对：「地址格式不对。请填完整的地址，例如 https://api.example.com/v1：以 http:// 或 https:// 开头，不含空格、中文和 # 号，端口在 1～65535 之间。」；带账号或敏感参数：「地址里不能带账号密码，也不能带 password、token、secret、key 这类参数。key 请填在 key 一栏。」；回环地址：「不能用本机地址，网关在容器里访问不到它」 |
| 默认模型用途不对 | 422 | 「{alias}」不是{对话/向量}模型 |
| 其他字段不合格 | 422 | 「别名」的格式、长度或范围不正确（「显示名」「型号」「地址」「说明」同理） |

## 网关

- `infra/litellm`：镜像里装上 `prisma` 并在构建时生成客户端；`config.yaml` 的 `general_settings` 加 `store_model_in_db: true` 和 `disable_spend_logs: true`（调用记录由平台记）。
- 数据库：和平台共用一个 Postgres 服务，用单独的库 `litellm`（网关的迁移不支持非 `public` 的 schema）。网关启动时如果库不存在就先建库。本地编排默认 `DATABASE_URL=postgresql://aep:aep@postgres:5432/litellm`。没设 `DATABASE_URL` 时网关照旧只读配置文件，页面显示 `no_db`。
- `LITELLM_SALT_KEY`：加密页面上填的 key。本地编排有一个开发用的默认值；VPS 的 `deploy.sh` 第一次部署时生成一个写进服务器 `.env`。**生成以后不能再改**，否则已经存的 key 解不开，只能在页面上重新填。
- 网关的管理接口（`/model/*`）由平台 API 用 `LITELLM_MASTER_KEY` 调用，网关端口不对外开放（VPS 上只绑 `127.0.0.1`）。

## 存储

新增两张平台表，都带 `tenant_id`：

- `model_calls`：`at`、`model`（别名）、`kind`（`chat` | `embedding` | `rerank`）、`run_id`、`node`、`prompt_tokens`、`completion_tokens`、`cost_usd`（网关响应头 `x-litellm-response-cost`，没有时为空）、`ok`、`error`（问题分类的 `code`）、`duration_ms`。启动时删掉 180 天以前的记录。记录失败只写日志，不影响调用本身。
- `platform_settings`：`key`、`value`（JSON），这一版只存默认模型 `default_models`。

## 权限

这一版还没有登录，能打开平台的人都能管理模型（VPS 上靠访问口令挡在外面）。第 2 批加上登录和角色后，模型页只给管理员。

## 测试

测试不连网关：用一个假的网关管理接口（`tests/fake_gateway_admin.py`，进程内的 httpx MockTransport），模型调用照旧用 `FakeGateway`。
