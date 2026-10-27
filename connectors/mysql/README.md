# MySQL 只读 MCP 连接器

独立的 Python 3.12 / uv 包，结构与 `connectors/postgres` 相同。官方 MCP SDK 提供
Streamable HTTP：`http://127.0.0.1:8002/mcp`。平台已经支持 `kind: "mysql"`，无需改平台代码。

## 启动与验收

在仓库根目录运行：

```bash
make up
make smoke-connectors
make test-connectors
make lint
```

只启动 MySQL 相关服务：

```bash
docker compose up --build -d --wait source-mysql connector-mysql
uv sync --frozen --project connectors/mysql
uv run --frozen --project connectors/mysql ruff check connectors/mysql
uv run --frozen --project connectors/mysql ruff format --check connectors/mysql
uv run --frozen --project connectors/mysql pytest connectors/mysql/tests -q
uv run --frozen --project connectors/mysql aep-mysql-smoke
```

测试连接真实 MySQL 8 示例库；不可用时失败，不会跳过。管理员账号只用于创建/清理测试表
和临时授权，连接器始终使用 `source_reader`。测试会写入合成数据，不能指向真实客户数据库。
HTTP 测试完成 MCP 握手、调用全部五个工具、检查参数、Host 校验及 readiness。

`make up` 通过现有 `AEP_DEFAULT_DATASOURCES` 自动登记第二个数据源「MySQL 示例库」。
工作台选择 `etl` →「MySQL 示例库」→ 输入需求，即可探查 `sales` 的三张表并生成方案。
与 PostgreSQL 一样，方案步骤需要配置真实模型；默认 `mock` 只用于测试，不能生成 ETL 方案。

## 工具接口

工具名、参数和 JSON 字段与 PostgreSQL 连接器一致，均带 `read_only_hint=true`。
MySQL 的数据库就是 schema，`list_schemas` 列出账号可见的非系统数据库。

| 工具 | 参数 | 返回字段 |
| --- | --- | --- |
| `list_schemas` | 无 | `schemas` |
| `list_tables` | `schema` | `schema`、`tables`（有整表 SELECT 权限的普通表，排除视图） |
| `describe_table` | `schema`, `table` | `schema`、`table`、`columns`（`name`、MySQL `data_type`、`nullable`、`primary_key`）、`foreign_keys`（名称、源列、目标 schema/表/列） |
| `sample_rows` | `schema`, `table`, `limit=10` | `schema`、`table`、`rows`，最多 100 行 |
| `table_stats` | `schema`, `table` | `schema`、`table`、`estimated_row_count`、`sample_limit`、`sample_row_count`、`columns`（`name`、`null_fraction`、`distinct_count`，低基数列另有 `top_values`） |

`limit` 只接受 1–100 的整数，拒绝布尔、字符串、浮点和越界值。日期时间为 ISO 字符串，
DECIMAL 为 JSON 数值，JSON 列解析为 JSON 值，二进制列显示为 `\x` 加十六进制。
复合外键的源列和目标列按约束中的顺序一一对应。系统库、视图和只有列级 SELECT 的表不暴露。

`estimated_row_count` 来自 `information_schema.TABLES.TABLE_ROWS`，InnoDB 为近似值，
可能陈旧；不可用时为 `null`。其余统计使用**同一份**最多 1,000 行的样本，不保证顺序或随机性。
SQL NULL 不计入不同值；空字符串计入。空表 `null_fraction=null`、`distinct_count=0`。

样本中不同值为 1–20 个的列返回 `top_values`，按次数降序、取值升序排列。
计数使用完整值，保留大小写与尾部空格差异，不受 MySQL 排序规则或 `GROUP_CONCAT` 长度限制影响。
JSON 使用稳定的键排序。展示值超过 80 个字符时，只返回前 80 字符并加 `truncated: true`；
共同前缀不会合并计数。超过 20 个不同值或全空的列省略 `top_values`，与 PostgreSQL 一致。

## 只读保护

- 种子只授予 `source_reader` 对 `sales.*` 的 SELECT，无写权限、管理权限或角色。
  `002-reader.sh` 用十六进制传入配置密码，不把密码拼接成 SQL 语法。
- 启动时、每次连接时和 `/healthz` 都检查权限；有写权限时启动失败。
  `SHOW GRANTS` 只允许直接的 USAGE / SELECT / SHOW VIEW，拒绝 GRANT OPTION、FILE、EXECUTE、
  临时表权限、动态管理权限、PROXY，以及任何可切换的角色（包括未激活角色）。
  对配置了 `mandatory_roles` 的服务器也拒绝连接，避免遗漏隐含角色权限。
  因此当前要求直接授权的只读账号，不支持使用角色授予读取权限。
- 在发出业务查询前设 `SESSION TRANSACTION READ ONLY`，检查无角色授权，并开启只读事务。
  即使后来误授写权限，当前事务仍拒写；下一次连接会重新检查授权并拒绝。
- 只暴露五个固定工具，不接受任意 SQL。库/表标识符使用反引号引用，内嵌反引号加倍转义；
  条件和行数使用绑定参数。列名来自元数据，不作为未引用的 SQL 片段拼接。
- 默认连接超时 5 秒；每个查询同时有 MySQL `max_execution_time=3000` 和 3 秒客户端期限，
  元数据锁等待也设上限。超时连接关闭，驱动错误中的数据、SQL、密码和连接信息不会返回客户端。
  禁用 `LOCAL INFILE`。

## 示例库与配置

`source-mysql` 使用 MySQL 8.0，端口 `127.0.0.1:3307`，数据卷 `source-mysql-data`。
`sales.customers` 1,000 行、`sales.products` 200 行、`sales.orders` 5,000 行，字段、复合主外键、
确定性数据和脏数据规则与 Postgres 示例库一致，均有 `tenant_id`。日期时间统一按 UTC 读取。
种子只在空卷首次启动时运行；修改环境变量中的密码不会修改已有账号。

所有配置由 `aep_mysql.config.Settings` 加载，沿用平台 Settings 模式和 `AEP_` 前缀。
独立命名空间避免依赖后端。凭据放 `.env` 或 Key Vault，不存入平台的数据源登记信息。

| 环境变量 | 默认值 |
| --- | --- |
| `AEP_MYSQL_HOST` | `127.0.0.1`（Compose 内 `source-mysql`） |
| `AEP_MYSQL_PORT` | `3307`（Compose 内 `3306`） |
| `AEP_MYSQL_DATABASE` | `sales` |
| `AEP_MYSQL_USER` | `source_reader` |
| `AEP_MYSQL_PASSWORD` | `local-source-reader`，仅本地示例 |
| `AEP_MYSQL_CONNECT_TIMEOUT_SECONDS` | `5`，范围 1–60 |
| `AEP_MYSQL_QUERY_TIMEOUT_MS` | `3000`，范围 1–60000 |
| `AEP_MYSQL_STATS_SAMPLE_SIZE` | `1000`，范围 1–10000 |
| `AEP_MYSQL_MCP_HOST` | `127.0.0.1`（Compose 内 `0.0.0.0`） |
| `AEP_MYSQL_MCP_PORT` | `8002` |
| `AEP_MYSQL_MCP_ALLOWED_HOSTS` | JSON 数组，允许 localhost、127.0.0.1、[::1]、connector-mysql 的任意端口 |
| `AEP_MYSQL_MCP_ALLOWED_ORIGINS` | JSON 数组，允许本地三个回环地址的 HTTP origin 的任意端口 |
| `AEP_SOURCE_MYSQL_ADMIN_PASSWORD` | `local-source-admin`，仅种子库和测试使用 |

连接器只绑定回环地址，并校验 Host / Origin；反向代理白名单可配置。
其他 MySQL 8 数据源须先准备直接授予 SELECT 的账号；账号可见的其他数据库也不能有写权限。

## 依赖与 CI

- `aiomysql`：异步 MySQL 访问、超时取消与字典游标。
- `pymysql[rsa]`：底层 MySQL 协议及 MySQL 8 `caching_sha2_password` / `sha256_password`
  的 RSA 认证（通过 `cryptography`）；无需系统 MySQL 客户端库。
- `mcp`、`pydantic`、`pydantic-settings`、`starlette`：沿用 Postgres 的 MCP、校验、配置和 HTTP 层。
- 开发依赖 `pytest`、`pytest-asyncio`、`ruff`、`httpx2`、`uvicorn`：真实数据库和 HTTP 测试及代码检查。

`uv.lock` 固定依赖；CI `connector-mysql` job 使用 MySQL 8 service 容器执行 lint、格式检查和全部测试。
整栈 CI 还检查 `kind: "mysql"` 自动登记与五个工具的发现。
