# 数据源与工具接口

后端和前端按这份约定并行开发。字段名 snake_case，时间是 ISO 8601 字符串。错误格式同 [knowledge.md](knowledge.md)：`{"detail": "中文说明"}`；请求体不合格也返回 422，`detail` 是一句中文，不是 `loc` 数组。

## 概念

- **连接器**：一个 MCP 服务（Streamable HTTP）。平台只保存它的地址。数据库账号密码配在连接器上（本地放 `.env`，Azure 放 Key Vault），平台不保存。
- **数据源**：给 ETL 和数据节点读数据用的连接器，类型是 `postgres`、`mysql`、`sqlserver`、`file`（上传的 CSV、XLSX，由平台的文件连接器提供）或 `databricks`（Databricks 上已有的表，见“Databricks 数据源”）。数据源必须提供 ETL 需要的 6 个工具：`list_schemas`、`list_tables`、`describe_table`、`table_stats`、`sample_rows`、`read_rows`，参数和返回同 `connectors/postgres`。
- **工具服务**：新类型 `mcp`。它只提供工具，不用于 ETL，比如查工单、查 CRM。工具名不限，但至少要有一个。工具服务只能用在编排应用的 `tool`（调用工具）节点和 `agent`（智能体）节点里；`etl_profile`、`databricks_ingest` 和 `etl` 应用都不接受它。
- **代码仓库**：类型 `git`（第 5 批）。Git 连接器（GitHub 或 Bitbucket Data Center），只用在编排应用的「提交 PR」节点和资产仓库里，别的节点和 `etl` 应用都不接受它；测试连接时要求 6 个 Git 工具。见 [git.md](git.md)。
- **只读标记**：工具的 `read_only` 取自连接器自报的 `annotations.readOnlyHint`，没报就是 `false`。它只用来展示，不是安全边界（代码沙箱的 `run_python` 也自称只读）。
- **第一阶段只支持只读工具**：数据源连接器都是只读的；登记工具服务的人要确认它的工具不会修改数据。平台不核实 `read_only`，也不拦没声明只读的工具，所以第一阶段不提供「试一下工具」。

## 数据结构

```jsonc
// DataSource
{
  "datasource_id": "uuid",
  "name": "MySQL 示例库",               // 1–100 字符，去掉首尾空白；不能和别的数据源、工具服务重名
  "kind": "postgres" | "mysql" | "sqlserver" | "file" | "databricks" | "mcp" | "git",
  "connector_url": "http://connector-mysql:8002/mcp",
                                        // 旧数据里如果带账号或敏感参数，返回时这部分换成 ***，库里存的不变
  "description": "本地 MySQL 示例源库……",  // 最多 2000 字符
  "created_at": "……"
}

// Tool
{
  "name": "describe_table",
  "description": "……",                  // 最多 2000 字符，更长的截掉
  "input_schema": {"type": "object", "properties": {"schema": {"type": "string"}, "table": {"type": "string"}},
                   "required": ["schema", "table"]},   // 连接器给的不是对象时为 {}
  "read_only": true                     // 连接器自报；没报为 false
}

// Problem：测试没通过的一条原因
{"code": "refused", "message": "连接被拒绝：这个地址和端口上没有服务。请检查端口，或确认连接器已经启动。"}

// TestResult
{
  "ok": false,                          // problems 为空时才是 true
  "latency_ms": 35,                     // 连上并列出工具花的时间；没连上为 null
  "tools": [{"name": "list_schemas", "description": "……", "read_only": true}],   // 没连上为 []
  "missing_tools": ["table_stats", "read_rows"],
                                        // 数据源类型缺的 ETL 工具，按上面 6 个的顺序；工具服务总是 []
  "problems": [Problem]
}

// Health：连通状态
{
  "ok": true,
  "latency_ms": 35,                     // 没连上为 null
  "tool_count": 6,                      // 没连上为 null
  "error": null | "timeout" | "refused" | "not_mcp" | "other",
  "message": null | "超时：连接器在 5 秒内没有响应。……",   // error 对应的那一句，见「问题分类」
  "checked_at": "……"                    // 这个结果是什么时候查的；用缓存时是原来的时间
}

// Usage
{"apps": [{"app": "retail-etl", "title": "零售数据 ETL", "version": 3, "via": "literal" | "default_input"}]}
```

## 接口

| 方法 | 路径 | 请求体 | 返回 |
| --- | --- | --- | --- |
| GET | `/datasources` | | `DataSource[]`，按名称排序，数据源和工具服务都在里面。不带连通状态和使用情况，页面对每一行再调 `/health` 和 `/usage` |
| POST | `/datasources/test` | `{"kind", "connector_url"}` | 200 `TestResult`，没通过也是 200，看 `ok`；`kind` 为 `file` 时 422 |
| POST | `/datasources` | `{"name", "kind", "connector_url", "description"?}` | 201 `DataSource`。重名先返回 409，不测试；再按 `/test` 测试，没通过返回 422；`kind` 为 `file` 时 422。保存的是规范化以后的地址 |
| POST | `/datasources/files` | `multipart/form-data`：`name`、`files` | 不变（201 `{"datasource", "dataset_id", "schema", "tables"}`）。地址由平台生成，不测试；重名的 409 和上面一样 |
| GET | `/datasources/{id}` | | `DataSource` |
| PATCH | `/datasources/{id}` | `{"name"?, "description"?, "connector_url"?}` | `DataSource`。只改名称和说明不测试。`connector_url` 和现在存的不同才测试，没通过 422，原记录不变。带 `kind` 时 422。上传文件的数据源不能改地址（422）。重名 409（改地址时在测试之前） |
| GET | `/datasources/{id}/health` | 查询参数 `fresh=true` 可选 | `Health`。同一数据源、同一地址 60 秒内返回缓存，失败也缓存；`fresh=true` 不看缓存、重新检查（页面上的「重新检查」） |
| GET | `/datasources/{id}/usage` | | `Usage`，按 `app` 排序 |
| GET | `/datasources/{id}/tools` | | `Tool[]`（新增 `read_only`）。超时同测试连接。连不上返回 502，`detail` 是「问题分类」里对应的那一句，不带原始报错 |

不存在的 id 一律 404。不提供 DELETE，删除和归档放到第二阶段。

`PATCH` 的 `connector_url` 按规范化以后的形式比较（协议、主机名不分大小写，默认端口写不写都一样，末尾的 `/` 不算）。`GET` 返回的是打码以后的地址，原样发回去算改了地址，会被当成带账号的地址拒绝，所以页面只在用户改了地址时才发这个字段。

## 地址检查

只在三处检查：`POST /datasources/test`、`POST /datasources`，以及 `PATCH` 时地址确实变了。已经登记的数据源不重新检查：运行、`/health`、`/tools` 和列表照常可用。

按下面的顺序检查，遇到第一条不通过就停，不会去连这个地址：

1. 格式（`bad_url`）：
   - 以 `postgres://`、`postgresql://`、`mysql://`、`mariadb://`、`sqlserver://`、`mssql://`、`oracle://`、`mongodb://` 或 `jdbc:` 开头的（不分大小写），是数据库的地址，单独提示。协议后面带 `+驱动`（字母、数字或 `_`）的也算，比如 `postgresql+psycopg2://`、`mysql+pymysql://`、`mongodb+srv://`；
   - 其他：只能是 `http` 或 `https`；最长 2048 个字符；首尾空白先去掉，中间只能有可打印的 ASCII 字符，不能有空白和 `\`；不能带 `#` 片段；端口在 1–65535 之间；主机名只能有字母、数字、`.`、`-`、`_`。
   - 主机写成 IP 时：`0.0.0.0/8`、`::`、组播和广播地址不行；`2852039166`、`0xa9fea9fe`、`0251.0376.0251.0376`、`127.1` 这类非标准的数字写法，按 IPv4 解析后如果是第 3 条的元数据地址算 `metadata`，否则算 `bad_url`。
2. 账号和敏感参数（`credentials`）：主机前面带 `@` 的（`用户名@`、`用户名:密码@`，`@` 前面是空的也算），拒绝。查询参数按 `&` 和 `;` 分开，参数名解码以后（不分大小写）含 `pass`、`pwd`、`token`、`secret`、`key`、`auth`、`credential`、`signature`，或者正好是 `sig`、`code` 的，也拒绝，比如 `api_key`、`access_token`、`x-api-key`、`%74oken`。
3. 元数据地址（`metadata`）：`169.254.0.0/16`、`fe80::/10`、`fd00:ec2::254`、`168.63.129.16`（Azure）、`100.100.100.200`（阿里云）。`::ffff:169.254.169.254` 这类 IPv4 映射地址按 IPv4 判断。
4. 平台内部服务（`internal_host`）：
   - 平台本身：本地编排里的 `api`、`web`，以及 `AEP_WEB_URL` 的主机；
   - 代码沙箱：`AEP_SANDBOX_URL` 的主机，以及 `sandbox-relay`、`connector-sandbox`；
   - 模型网关、Langfuse、平台数据库、Redis：`AEP_LLM_GATEWAY_URL`、`LANGFUSE_HOST`、`AEP_DATABASE_URL`（写了多个主机时每一个都算）、`AEP_REDIS_URL` 的主机；
   - 本地编排里的 `litellm`、`langfuse-*`、`postgres`、`redis`、`source-postgres`、`source-mysql`。

   不论端口都拒绝。在 Kubernetes 里，同一个服务还叫 `<名字>.<命名空间>`，后面还可以接 `.svc` 和集群域名（比如 `sandbox-relay.aep.svc.cluster.local`），这些名字也都拒绝；上面的设置里写的是这种长名字时，短名字和其他长名字也一样拒绝。只有两段的主机名一律按 `<名字>.<命名空间>` 看，所以 `api.corp` 也算 `api`。

   例外：这几个地址的主机如果是回环地址（`localhost` 和它的子域名、`127.0.0.0/8`、`::1`，都算同一台），只拒绝那个端口。黑名单先于白名单检查，白名单里写了也没用。
5. 白名单（`host_not_allowed`）：主机和端口要在 `AEP_CONNECTOR_HOSTS` 里，写法见下。另外，文件连接器（`AEP_FILES_CONNECTOR_URL`）和 `AEP_DEFAULT_DATASOURCES` 里各地址的主机和端口总是允许。回环地址只有在白名单里写明主机和端口时才允许，`*` 写法匹配不到它。

只看地址本身，不解析域名。主机名解析出来是什么 IP，靠白名单只写具体的连接器主机来保证。

保存的是规范化以后的地址：协议和主机名转小写，IPv6 地址写成最短形式，去掉默认端口（http 80，https 443），路径和查询参数原样保留。

### `AEP_CONNECTOR_HOSTS`

JSON 列表，每一项是下面一种写法：

| 写法 | 匹配 |
| --- | --- |
| `connector-postgres:8001` | 这个主机的这个端口 |
| `connector-postgres:*` | 这个主机的任意端口 |
| `connector-postgres` | 这个主机的默认端口（http 80，https 443） |
| `[fd00::5]:8001` | IPv6 地址 |
| `*.connectors.svc.cluster.local:*` | 这个后缀下的子域名，不含后缀本身；端口写法同上 |

不接受单独一个 `*`，也不接受 `*:8001` 这样主机只有 `*` 的写法；写法不对时平台启动失败。默认值是 `["connector-postgres:8001", "connector-mysql:8002", "connector-files:8003", "connector-databricks:8005"]`，也就是本地编排里的四个数据源连接器。不用 docker 单独开发后端时，按需加上本机连接器的地址：`localhost:*` 这类写法对回环地址不起作用，要写明端口，例如 `localhost:8001`；不要写平台自己的端口（如 `localhost:8000`）。

后缀写法不要盖住平台自己所在的命名空间。平台只认得出第 4 条里那些名字的服务；部署时改过名的服务（比如 Helm 加了前缀的平台接口）认不出来，会被后缀写法放进来。

登记新的主机名时，连接器那边也要允许这个主机名（各连接器的 `AEP_*_MCP_ALLOWED_HOSTS`）。不允许的话，连接器返回 421，这里会报「不是 MCP 服务」。

## 测试连接

1. 先做上面的地址检查。没通过时只有一条问题，不去连这个地址。
2. 再在 `AEP_CONNECTOR_CHECK_SECONDS`（默认 5 秒，最多 8 秒）内连上连接器、完成握手、列出工具。工具分页时最多读 10 页、500 个工具。
3. 数据源类型对照 6 个 ETL 工具，缺了就列在 `missing_tools` 里，并加一条 `missing_tools` 问题；代码仓库对照 6 个 Git 工具，问题文字见 [git.md](git.md)。工具服务一个工具都没有时，加一条 `no_tools` 问题。

连不上时只给出分类和中文说明，原始报错（里面可能有地址、对方的返回内容）只写进平台日志。

重定向：MCP 客户端只跟随同源的重定向（协议、主机、端口都相同，或者同一主机从 http 到默认端口的 https），并且要保持请求方法（POST 只跟 307、308）。跳到别的主机（比如 `169.254.169.254`）的重定向不跟随、不去连，按 `not_mcp` 报告。

## 连通状态

列出连接器的工具，超时和上面一样，不做地址检查。结果按数据源缓存 60 秒，缓存只在同一个连接器地址下有效：`PATCH` 改了地址，缓存就作废；新登记或改地址时，测试结果直接作为第一份缓存；`GET /{id}/tools` 连上或连不上，也顺便更新这份缓存，所以详情页的状态和工具列表不会互相矛盾。缓存在每个 API 进程里各存一份。

## 使用情况

只看每个未归档编排应用的**当前版本**，草稿、旧版本和已归档应用都不算，规则和 Skill 的「被哪些应用使用」一样。代码应用（比如 `etl`）的数据源是运行时填的，也不算。按运行记录统计放到第二阶段。

一个应用最多出现一次：
- `literal`：`tool`、`etl_profile`、`databricks_ingest`、`data_check`、`git_pr` 节点的 `datasource`，或 `agent` 节点 `tools[].datasource`，直接写了这个 id；或者写成整段 `{{env.xxx}}`，而常量（`env.vars` 里 `key` 为 `xxx` 的那一项）的 `value` 是这个 id。
- `default_input`：开始节点（`type` 为 `start` 的那个节点，id 不一定是 `start`）的输入里，`datasource` 类型的字段默认值是这个 id；或者某个字段被上面这些设置整段引用（`{{<开始节点的 id>.xxx}}`），它的默认值是这个 id。`select` 字段的选项不算，只看默认值。

两种都有时算 `literal`。比较 id 时不分大小写，`{…}` 写法的 UUID 也认。某个应用的配置读不出来时跳过它，不影响别的应用。

## 问题分类

| code | 说明（`message`） |
| --- | --- |
| `bad_url` | 地址格式不对。请填完整的连接器地址，例如 http://connector-postgres:8001/mcp：以 http:// 或 https:// 开头，不含空格、中文和 # 号，端口在 1～65535 之间。 |
| `bad_url`（数据库地址） | 这是数据库的地址。这里要填连接器的地址（一般以 /mcp 结尾），数据库地址和账号密码配在连接器上。 |
| `credentials` | 地址里不能带账号密码，也不能带 password、token、secret、key 这类参数。数据库账号密码配在连接器上，平台只保存地址。 |
| `metadata` | 不能使用云服务器的元数据地址（如 169.254.169.254）。 |
| `internal_host` | 这是平台内部服务的地址（平台本身、代码沙箱、模型网关、Langfuse、数据库或 Redis），不能登记。 |
| `host_not_allowed` | 主机 {host}:{port} 不在允许的范围内，请联系管理员把它加进 AEP_CONNECTOR_HOSTS。 |
| `timeout` | 超时：连接器在 {秒数} 秒内没有响应。请确认连接器已经启动，地址和端口没写错。 |
| `refused` | 连接被拒绝：这个地址和端口上没有服务。请检查端口，或确认连接器已经启动。 |
| `not_mcp` | 不是 MCP 服务：能连上这个地址，但对方不是 MCP 连接器。请检查路径（一般以 /mcp 结尾）；如果确实是连接器，请让管理员在连接器上允许这个主机名。 |
| `other` | 网络不通：找不到这个主机，或者网络不通，也可能是 http 和 https 写反了。请检查地址，必要时请管理员查看平台日志。 |
| `missing_tools` | 缺少 ETL 需要的工具：{工具名，用「、」隔开}。数据源要提供 list_schemas、list_tables、describe_table、table_stats、sample_rows、read_rows 这 6 个工具；如果它只提供别的工具，请把类型选成「工具服务」。 |
| `no_tools` | 连接器没有提供任何工具，工具服务至少要有一个工具。 |

- `host_not_allowed` 里 IPv6 主机带方括号，比如 `[fd00::5]:8001`；没写端口时是默认端口。
- `timeout` 的秒数按 `AEP_CONNECTOR_CHECK_SECONDS` 写，整数不带小数点（「5 秒」）。
- 每条 `message` 都以句号结尾。

页面上的短标签（状态词）：`timeout` 超时，`refused` 连接被拒绝，`not_mcp` 不是 MCP 服务，`other` 网络不通；连通时是「正常」。连接类问题的 `message` 都以状态词开头。

其他错误：

| 情况 | 状态 | `detail` |
| --- | --- | --- |
| id 不存在 | 404 | 没有找到这个数据源或工具服务 |
| 重名（含上传文件） | 409 | 已经有叫「{name}」的数据源或工具服务了 |
| 保存时测试没通过 | 422 | 测试连接没有通过。{各条 message 直接连在一起} |
| 用 `/test` 或 `POST /datasources` 登记 `file` | 422 | 上传文件的数据源请用「上传文件」登记，不用填地址 |
| 改上传文件数据源的地址 | 422 | 上传文件的数据源不能修改地址 |
| PATCH 带 `kind` | 422 | 类型登记后不能修改 |
| 字段为 null | 422 | 请求字段不能为 null |
| 其他字段不合格 | 422 | 「名称」的格式、长度或范围不正确（「类型」「连接器地址」「说明」同理） |
| `/tools` 连不上 | 502 | 上表 `timeout`、`refused`、`not_mcp`、`other` 中对应的那一句 |
| `/tools` 连接器报错 | 502 | 连接器列工具时报错，请让管理员查看连接器的日志。 |

编排应用里选到工具服务时：
- `etl_profile`、`databricks_ingest` 写死的 id 是工具服务，保存时报「「{name}」是工具服务，这里要选数据库或上传文件的数据源」；
- 运行时（比如 `{{start.datasource}}`）选到工具服务，节点以 `input_error` 失败：「「{name}」是工具服务，不能用来探查或抽取数据，请选数据库或上传文件的数据源」；
- `etl` 应用的 `datasource_id` 是工具服务时，运行失败：「「{name}」是工具服务，ETL 要选数据库或上传文件的数据源」。

## Databricks 数据源（第三批）

改造方案第一期新增，落在**连接器**这一层：Databricks 上已有的表（可以跨 catalog）登记成一个数据源，探查、引用和数据检查都经它只读访问，不复制数据。设计见 [projects.md](projects.md) 开头的设计文档。

### 连接器 `connectors/databricks`

独立的只读 MCP 连接器，和 postgres 连接器一样是一个 uv 包，本地编排里叫 `connector-databricks`，端口 8005，地址 `http://connector-databricks:8005/mcp`。

- 提供同样的 6 个工具，参数和返回同 postgres 连接器，只有这些不同：
  - `schema` 一律写成 `catalog.schema` 两段（比如 `icsd-ace-dev.sales`），`list_schemas` 返回允许的 catalog 下的这种两段名字；表名是 `table`。所以平台和节点里 Databricks 的表一律是三段式 `catalog.schema.table`。名称不分大小写（Unity Catalog 的规则），不能有反引号和控制字符。
  - 元数据（schema、表、列、注释、主键外键）取各 catalog 的 `information_schema`，所以 catalog 要在 Unity Catalog 里，`hive_metastore` 不支持。`list_tables` 列出这个身份能看到的表和视图（视图也能读）。
  - `table_stats`、`sample_rows`、`read_rows` 多一个可选参数 `where`：一个 Spark SQL 布尔表达式，只读满足它的行。形状检查同项目引用表的 `filter`（[projects.md](projects.md) 的取值规则），连接器自己再查一遍，而且更严：按 Spark 的写法识别字符串（单引号、双引号，反斜杠转义，`r'…'` 不转义）和反引号名称，再查分号、注释、`SELECT`、`TABLE`、`WITH`（不能有子查询），`FROM` 只能用在 `extract`、`trim`、`substring`、`substr`、`position`、`overlay` 的括号里；不能调用 `reflect`、`try_reflect`、`java_method`、`secret`、`try_secret`、`list_secrets`、`http_request`、`read_files` 和其他 `read_*` 流读取、`remote_query`、`vector_search`、`identifier`，以及 `ai_` 开头的函数（会把数据发给模型），也不能调用带 catalog 或 schema 的函数（Unity Catalog 里的自定义函数）；最多 2000 个字符。不合格时工具报错“读取条件写法不对：…”（后面是原因）。通过后放进 `WHERE (…)`。
  - `describe_table` 的列另有 `comment`（列注释，没有为 null），返回另有表的 `comment`；类型是 Databricks 的写法（`string`、`decimal(18,2)`、`timestamp`）。`primary_key` 和 `foreign_keys` 取 Unity Catalog 里登记的约束（只是信息，Databricks 不强制），没登记时为 false 和 `[]`。
  - `table_stats` 的 `estimated_row_count` 是带 `where` 的精确行数；空值率、不同值个数和 `top_values` 在最多 `AEP_DATABRICKS_READ_STATS_SAMPLE`（默认 1000）行的样本上算，同 postgres 连接器。
  - `read_rows` 按主键排序（没登记主键时按全部列），用 `LIMIT … OFFSET …` 分页。
- 另有 3 个只读的分析工具（`databricks` 类型才有，登记时不要求，`data_check` 节点用；`tool`、`agent` 节点也能用）。表名一律是三段式 `table`（`catalog.schema.table`），`where`、`expected_where`、`expr` 的形状检查同上，最多返回 `limit` 行（1–1000，默认 20）：
  - `aggregate`：`{"table", "expr", "where"?, "group_by"?: [列], "limit"?}`，返回 `{"rows": [{分组列…, "value": 值}], "groups": 组数}`；不分组时 `rows` 只有一行，`groups` 是 1。`expr` 的形状检查同 `where`，报错写“聚合表达式写法不对：…”；分组列不能叫 `value`。`limit` 不分组时不起作用，分组时最多 10000。`groups` 是满足条件的全部组数，可能比 `rows` 多：`rows` 按分组列排序，取前 `limit` 组。
  - `aggregate` 和 `sample_rows` 另有可选参数 `lookup`：`{"table": "catalog.schema.table", "on": {本表的列: 另一张表的列}, "where"?: 另一张表的条件, "found": true | false}`，只读在另一张表里找得到（`found: true`）或找不到（`false`）对应行的那些行，比如“分摊表里这个月不适用的航线”。`on` 1–10 对列，按空值安全的相等对上（两边都是空值算对上）；`where` 只用在另一张表上，形状检查同上，报错写“lookup.where 写法不对：…”；另一张表同样只能在允许读的 catalog 里；`found` 必填，不能用文字代替布尔。语句写成 `FROM 本表 AS __aep_t WHERE (where) AND [NOT] EXISTS (SELECT 1 FROM 另一张表 AS __aep_l WHERE __aep_l.列 <=> __aep_t.列 … AND (lookup.where))`，子查询由连接器自己拼，`where`、`expr` 里仍然不能写子查询。（jadeflow 需求变更轮后新增，给 `rows` 检查的 `lookup` 用）
  - `duplicates`：`{"table", "columns": [列], "where"?, "limit"?}`，返回 `{"groups": 重复的组数, "rows": [{列…, "count": 次数}]}`（按次数从多到少）；列不能叫 `count`。
  - `compare`：`{"table", "expected", "keys": [列], "columns"?: [列], "where"?, "expected_where"?, "tolerance"?, "limit"?}`：先数两边按 `keys` 重复的组数，返回里的 `duplicates` 是 `{"table": 组数, "expected": 组数}`；任何一边有重复时不再比较（`missing`、`extra`、`different` 为 null，`rows` 是重复的键和次数：`{"kind": "duplicate", "side": "table" | "expected", "key": {键列: 值}, "count": 次数}`），因为有重复键时连接会把多出来的行算成一致。都不重复时按 `keys` 全外连接两张表（键用空值安全的相等，两边都是空值的键算同一个），返回 `{"duplicates": {"table": 0, "expected": 0}, "missing": expected 有而 table 没有的行数, "extra": 反过来, "different": 键相同而某列不同的行数, "columns": 比较了哪些列, "rows": [例子]}`；例子是 `{"kind": "missing" | "extra" | "different", "key": {键列: 值}, "table": {列: 值} | null, "expected": {列: 值} | null}`，`different` 另有 `"differs": [不同的列]`，按 `kind`、键排序。`columns` 不写时比较两张表都有的非键列（列名不分大小写）；写了的列、`keys` 两边都要有，否则报“<表名> 没有列 …”；列名用 `table` 那边的写法。数字列（两边都是数字类型）差的绝对值不超过 `tolerance`（默认 0）算相同，两边都是空值算相同；只有一边是空值（比如 `NULL` 和 `0`）算不同。两边的条件分开：`where` 只用在 `table` 上，`expected_where` 只用在 `expected` 上（两张表的列常常不一样，一边的条件放到另一边可能报错或筛错行），各自先筛再数重复、再连接。
- 在哪里执行：设了 `AEP_DATABRICKS_READ_WAREHOUSE_ID` 时用 SQL warehouse（Statement Execution API，结果内联、按块读全），否则用 `AEP_DATABRICKS_READ_CLUSTER_ID` 指定的已有集群（命令执行接口）；两个都没设时工具报错“连接器没有配置在哪里执行查询”。集群没在运行时默认报错“集群 … 没有在运行，请先在 Databricks 上启动它”，连接器不替你启动集群；设了 `AEP_DATABRICKS_READ_START_CLUSTER=true` 时改成替你启动：集群是停止的（`TERMINATED`）就启动一次，正在启动或停止中的就等，在 `AEP_DATABRICKS_READ_TIMEOUT_SECONDS` 内等到 `RUNNING` 再执行语句；同时到来的调用只启动一次，一起等。启动失败报错“集群 … 没能启动：<Databricks 给的原因>”；到时间还没就绪报错“集群 … 正在启动（<状态>），等了 N 秒还没就绪，请过几分钟重试”，集群照样在启动，之后的调用会等到它。启动集群要几分钟，所以开这个设置时把 `AEP_DATABRICKS_READ_TIMEOUT_SECONDS` 和平台的 `AEP_DATABRICKS_TOOL_SECONDS` 一起调大（比如 600 和 620），否则第一次调用会先超时。默认不开：启动集群要花钱，客户的集群也不一定允许平台去启动。`/healthz?probe=1` 执行 `SELECT 1`，开了这个设置时同样会启动集群。集群上用一个复用的 Python 执行环境跑一段固定的程序，语句以 JSON 字符串字面值传进去（不会当代码执行），程序按类型把结果转成 JSON 打印出来；不用 SQL 上下文，是为了两种执行方式的取值写法一致（小数、日期、嵌套类型），也和平台在这个集群上试跑代码用的是同一种接口。输出被集群截断时报错“结果太大，被集群截断了；请减小 limit 或加条件”。
- 取值：整数是数字；小数有小数位时是数字（可能有浮点误差），没有时是整数；布尔是 true/false；日期和时间是 ISO 文本；数组、map、struct 是 JSON；NaN、无穷是文本。语句出错时工具报错“Databricks 报错：…”，带 Databricks 自己的报错（集群上只取异常那一行，不带程序和调用栈），里面可能引用取值，平台按数据外发策略处理（[projects.md](projects.md)）。
- 只读：
  - 连接器只执行自己拼好的 `SELECT`、`DESCRIBE`、`SHOW` 语句，没有执行任意 SQL 的工具；标识符一律校验后加反引号，`limit`、`offset` 校验成整数后再写进语句；`where`、`expr` 这类表达式只在形状检查通过后放进括号里。
  - 只能读 `AEP_DATABRICKS_READ_CATALOGS`（JSON 列表，必填，空列表时所有工具报错）里的 catalog，别的 catalog 一律报“没有允许读 catalog …”。
  - 真正的边界是令牌的权限：`AEP_DATABRICKS_READ_TOKEN` 要用只有 `USE CATALOG`、`USE SCHEMA`、`SELECT` 权限的身份（服务主体或个人令牌），部署说明里写明。令牌只配在连接器上，平台不保存。
- 设置（都在连接器自己的环境里）：`AEP_DATABRICKS_READ_HOST`（工作区地址）、`AEP_DATABRICKS_READ_TOKEN`、`AEP_DATABRICKS_READ_CLUSTER_ID` 或 `AEP_DATABRICKS_READ_WAREHOUSE_ID`、`AEP_DATABRICKS_READ_CATALOGS`、`AEP_DATABRICKS_READ_TIMEOUT_SECONDS`（每条语句，默认 150）、`AEP_DATABRICKS_READ_STATS_SAMPLE`，以及 MCP 的 `AEP_DATABRICKS_MCP_HOST`、`AEP_DATABRICKS_MCP_PORT`、`AEP_DATABRICKS_MCP_ALLOWED_HOSTS`。本地编排默认把平台的 `AEP_DATABRICKS_HOST`、`AEP_DATABRICKS_TOKEN`、`AEP_DATABRICKS_CLUSTER_ID` 传给前三项，方便开发；生产要单独配只读身份。
- `GET /healthz`：不访问 Databricks，总是 200，返回 `{"status": "ok", "configured": 配置齐不齐, "missing": [缺的设置]}`。本地编排每 5 秒查一次，每次都执行查询会让 warehouse 或集群一直停不下来。`GET /healthz?probe=1` 才执行 `SELECT 1`：通了 200，否则 503，`problem` 说明缺哪项配置或连不上（不带令牌和 Databricks 的原始报错）。
- 没配工作区时连接器照样启动，工具调用报“连接器没有配置 Databricks 工作区”；`AEP_DEFAULT_DATASOURCES` 默认不登记它，要用时在“数据源和工具”页面登记（类型选 Databricks）。

### 平台这边

- 登记、测试连接、连通状态和上面一样：类型 `databricks`，要有 6 个工具。
- 平台调 `databricks` 数据源的工具时，超时用 `AEP_DATABRICKS_TOOL_SECONDS`（默认 180，要比连接器的语句超时长），其他类型照旧。
- 能用在 `tool`、`agent`、`etl_profile`、`databricks_ingest`、`data_check` 节点上（`data_check` 只接受这种类型）；`etl` 代码应用不接受（“「…」是 Databricks 数据源，ETL 应用要选数据库或上传文件的数据源，Databricks 的表请用编排应用 etl-lakehouse 第 3 版”）。
- 探查、抽取怎么对待它见 [data-nodes.md](data-nodes.md) 第三批；数据外发策略把它的工具结果算作数据值（[projects.md](projects.md)）。
- 测试和 CI 不连真实的 Databricks：连接器的测试用假的执行接口，平台用 `tests/fake_connector.py` 加一个 `databricks` 版本。

## 存储

表结构不变（`datasources`，`kind` 是 text，没有约束）。新增按 id 修改名称、说明和地址，重名仍由 `UNIQUE (tenant_id, name)` 拦截。业务表都带 `tenant_id`。

平台启动时登记 `AEP_DEFAULT_DATASOURCES`：名称已经有了，或者已经有数据源用着同一个连接器地址（比如「MySQL 示例库」在页面上改了名），就跳过，不会重复登记。`GET /health/demo` 和 `make demo` 找演示数据源、评测按 `AEP_EVAL_DATASOURCE` 找数据源时，只看数据源，不看工具服务；先按名称找，名称对不上再按 `AEP_DEFAULT_DATASOURCES` 里同名那一项的地址找，所以改名不影响它们。`GET /health/demo` 在没配这一项时按本地编排的地址 `http://connector-mysql:8002/mcp` 找；`make demo` 在平台外面运行，读不到平台的设置，总是按这个本地地址找。

## 设置

- `AEP_CONNECTOR_HOSTS`：见上。
- `AEP_CONNECTOR_CHECK_SECONDS`：测试连接、连通状态和 `/tools` 的超时，默认 5，最多 8。前端代理的超时是 10 秒，要比它短。

## 放到第二阶段

试一下工具；删除或归档数据源；按运行记录统计使用情况；平台系统（沙箱、Databricks、模型网关）的卡片；带写操作的工具；运行时也检查地址；解析域名后核对 IP，防 DNS 重绑定。
