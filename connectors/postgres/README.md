# PostgreSQL 只读 MCP 连接器

独立的 Python 3.12 / uv 包，使用[官方 MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
提供 Streamable HTTP：`http://127.0.0.1:8001/mcp`。不依赖平台后端或模型网关。

## 启动与验收

在仓库根目录运行：

```bash
make up
make smoke-connectors
make test
make lint
```

只启动本 Issue 的服务：

```bash
docker compose up --build -d --wait source-postgres connector-postgres
uv sync --frozen --project connectors/postgres
uv run --frozen --project connectors/postgres ruff check connectors/postgres
uv run --frozen --project connectors/postgres ruff format --check connectors/postgres
uv run --frozen --project connectors/postgres pytest connectors/postgres/tests -q
uv run --frozen --project connectors/postgres aep-postgres-smoke
```

测试必须连接真实的合成示例库；数据库不可用时会失败，不会静默跳过。
测试用管理员账号仅用于临时测试表的创建和清理，连接器始终使用 `source_reader`。
测试过程中的写入只用于这个本地示例库。不要将测试指向客户或生产数据库。

`aep-postgres-smoke` 通过真实 HTTP 连接完成 MCP 握手、发现并调用全部五个工具，
核对三张表、订单主外键、100 行采样、5,000 行估计以及非零空值率，同时验证 `limit=101` 被拒绝。
成功后打印一条 `status: ok` 的 JSON 摘要。可用 `--url http://127.0.0.1:8001/mcp` 指定地址。
也可以在 MCP Inspector 中选择 **Streamable HTTP** 并连接上述 `/mcp` 地址。

`GET http://127.0.0.1:8001/healthz` 会实际连接数据库并检查只读账号权限。
数据库连接或权限配置失败时返回 503。

## 工具接口

工具均返回 JSON 对象，并标记 MCP `read_only_hint=true`。

| 工具 | 参数 | 返回字段 |
| --- | --- | --- |
| `list_schemas` | 无 | `schemas`：可访问的非系统 schema 名称 |
| `list_tables` | `schema` | `schema`、`tables`：有 SELECT 权限的普通表和分区表名称 |
| `describe_table` | `schema`, `table` | `columns`：名称、PostgreSQL 类型、`nullable`、`primary_key`；`foreign_keys`：名称、源列、目标 schema/表/列 |
| `sample_rows` | `schema`, `table`, `limit=10` | `rows`：最多 100 行 JSON；日期时间为字符串，numeric 为 JSON 数值 |
| `table_stats` | `schema`, `table` | `estimated_row_count`、`sample_limit`、`sample_row_count`、每列的 `null_fraction` / `distinct_count`；样本里不同值不超过 20 个的列另有 `top_values`（取值和次数，按次数降序） |

`limit` 必须是 1–100 的整数，布尔值、字符串、浮点数和越界值都会被拒绝。
主键通过各列的 `primary_key` 标识；复合外键的源列和目标列按约束顺序一一对应。

`estimated_row_count` 使用 PostgreSQL `pg_class.reltuples`，可能陈旧；未有估计时为 `null`。
空值率和不同值数量在同一份最多 1,000 行的物化样本上计算，并返回实际样本行数。
样本不保证顺序或随机性，不代表全表精确分布。不同值数量不包含 SQL NULL，空字符串算一个值；
空表的空值率为 `null`、不同值数量为 0。JSON 列也支持不同值统计。
不暴露系统表、视图、物化视图、外部表或任意 SQL 执行入口。

## 只读保护

- 种子脚本创建 `source_reader`：非 owner、非超级用户，无建库、建角色、复制或绕过 RLS 权限。
  撤销 PUBLIC 的数据库权限和 public schema 权限，只授予示例库 CONNECT、sales USAGE 和表 SELECT。
  不授予 CREATE、TEMP、序列写权限或任何表写权限。
- 角色级设置 `default_transaction_read_only=on`；连接器还在**每次建立连接时**通过 libpq options
  设置此参数，并将当前连接事务设为只读。
- 每次连接检查有效权限、所有权和可切换角色；误用管理员账号、继承/可切换到写角色，
  或后续获得表/列写权限、维护权限、服务端文件/程序执行权限时会拒绝请求。
- 数据库名和用户名先校验，再作为独立 libpq 参数传入。schema/表/列名验证 UTF-8 字节长度和控制字符，
  所有动态 SQL 标识符使用 `psycopg.sql.Identifier` 引用；查询条件和行数使用绑定参数。
  合法的空格、Unicode、引号名称仍可使用。
- 连接超时默认 5 秒，每条语句及锁等待默认 3 秒，事务空闲超时 10 秒。错误响应不返回驱动错误中的源数据或连接信息。
- 测试分别验证事务拒写（SQLSTATE `25006`）和关闭只读事务后账号仍拒写（`42501`），
  覆盖 INSERT、UPDATE、DELETE、TRUNCATE、DDL、临时表、COPY FROM 和数据修改 CTE；另外实测查询超时和标识符注入。

实现依据：[PostgreSQL 只读事务](https://www.postgresql.org/docs/17/sql-set-transaction.html)、
[连接默认参数](https://www.postgresql.org/docs/17/runtime-config-client.html)、
[Psycopg 标识符组合](https://www.psycopg.org/psycopg3/docs/api/sql.html)。

## 合成示例库与配置

`source-postgres` 是独立的 PostgreSQL 17 服务，端口 `127.0.0.1:5433`。
种子为确定性的 `sales.customers` 1,000 行、`sales.products` 200 行、`sales.orders` 5,000 行；
三张业务表均有 `tenant_id`，包含复合主键、外键、缺失值、空格、大小写不一致、重复邮箱和负数数量。
所有姓名、邮箱和交易均为合成数据。

种子只在 `source-postgres-data` 空卷首次启动时执行。已有卷会保留数据；修改初始化密码不会自动改变已有角色密码。
如需重建示例库，请先确认卷中只有可丢弃的示例数据，再单独删除该服务及对应卷。

配置统一由 `aep_postgres.config.Settings` 读取，保持 API `aep.config.Settings` 的 Settings 模式和 `AEP_` 前缀。
独立命名空间避免连接器依赖/覆盖 backend 的 `aep` 包。`.env` 从当前工作目录读取，Compose 也会加载根目录 `.env`。
默认密码只用于本地合成示例；实际凭据放 `.env`（Azure 使用 Key Vault），不要提交。

| 环境变量 | 本地默认值 |
| --- | --- |
| `AEP_POSTGRES_HOST` | `127.0.0.1`（Compose 内为 `source-postgres`） |
| `AEP_POSTGRES_PORT` | `5433`（Compose 内为 `5432`） |
| `AEP_POSTGRES_DATABASE` | `aep_source` |
| `AEP_POSTGRES_USER` | `source_reader` |
| `AEP_POSTGRES_PASSWORD` | `local-source-reader`，仅用于示例 |
| `AEP_POSTGRES_CONNECT_TIMEOUT_SECONDS` | `5`，范围 1–60 |
| `AEP_POSTGRES_QUERY_TIMEOUT_MS` | `3000`，范围 1–60000 |
| `AEP_POSTGRES_STATS_SAMPLE_SIZE` | `1000`，范围 1–10000 |
| `AEP_POSTGRES_MCP_HOST` | `127.0.0.1`（Compose 内为 `0.0.0.0`） |
| `AEP_POSTGRES_MCP_PORT` | `8001` |
| `AEP_POSTGRES_MCP_ALLOWED_HOSTS` | JSON 数组：localhost / 127.0.0.1 / [::1] / connector-postgres，允许任意端口 |
| `AEP_POSTGRES_MCP_ALLOWED_ORIGINS` | JSON 数组：本地三个回环地址的 HTTP origin，允许任意端口 |
| `AEP_SOURCE_POSTGRES_ADMIN_PASSWORD` | `local-source-admin`，只供种子库和测试使用 |

连接其他数据库时，需要由数据库管理员预先准备只有 CONNECT / schema USAGE / 表 SELECT 的非 owner 账号，
并撤销 PUBLIC 或成员角色带来的 CREATE、TEMP、写表和写序列权限。
单独运行 `uv run aep-postgres`，或给 Compose 的连接器服务覆盖这些连接参数；不要使用示例库测试命令验证真实数据源。
当前 HTTP 服务供本地开发，Compose 只绑定回环地址，并开启 Host / Origin 的 DNS rebinding 校验；
反向代理的 Host / Origin 白名单可通过上述 JSON 数组配置，保持相同镜像；外部部署的认证和接入策略另行配置。

## 依赖

- `mcp`：官方 MCP Server / Client、Streamable HTTP 和工具参数校验。
- `psycopg[binary]`：异步 PostgreSQL 访问、绑定参数及安全标识符组合，无需系统 libpq。
- `pydantic` / `pydantic-settings`：参数约束、统一 Settings 和 `AEP_` 配置加载，密码使用 `SecretStr`。
- `starlette`：MCP HTTP 的自定义 readiness 路由。
- 开发依赖 `pytest`、`pytest-asyncio`、`ruff`：真实数据库/HTTP 测试及仓库统一代码检查；
  `httpx2` / `uvicorn`：实测 HTTP 请求、就绪路由及 Host 校验。

`uv.lock` 固定完整依赖树；CI 的 `connectors` job 启动示例库和连接器后运行全部检查及 HTTP 验收脚本。
