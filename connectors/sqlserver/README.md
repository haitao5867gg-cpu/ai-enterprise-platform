# SQL Server 只读 MCP 连接器

独立的 Python 3.12 / uv 包，结构和 `connectors/mysql` 相同。官方 MCP SDK 提供 Streamable HTTP：
`http://127.0.0.1:8007/mcp`。平台早已支持 `kind: "sqlserver"`，不用改平台代码。

驱动用 [pymssql](https://pypi.org/project/pymssql/)（轮子里自带 FreeTDS，不用装系统的 ODBC 驱动）。
pymssql 是同步库，每次工具调用在工作线程里开一个连接，用完关掉。

## 启动与验收

SQL Server 的镜像约 1.5 GB，而且只有 x86-64 版（Apple Silicon 的 Mac 上用 Rosetta 跑），所以不在
`make up` 里，用 compose 的 `sqlserver` profile 单独启动：

```bash
make up-sqlserver      # 示例库 + 导入种子数据 + 连接器
make test-sqlserver    # 集成测试（连真实 SQL Server，不可用时失败，不跳过）
```

启动后在「数据源与工具」页登记：类型选 SQL Server，地址 `http://connector-sqlserver:8007/mcp`。
示例库 `source` 里有 `sales.customers`、`sales.products`、`sales.orders` 三张表，和 MySQL、PostgreSQL
示例库的数据一样（含脏数据）。

只跑连接器自己的检查：

```bash
uv sync --frozen --project connectors/sqlserver
uv run --frozen --project connectors/sqlserver ruff check connectors/sqlserver
uv run --frozen --project connectors/sqlserver pytest connectors/sqlserver/tests -q
uv run --frozen --project connectors/sqlserver aep-sqlserver-smoke
```

测试用管理员账号（`sa`）建、删测试用的 schema 和表、临时授权，连接器始终用 `source_reader`。
测试会写入合成数据，不能指向真实的客户数据库。

## 配置

环境变量都带 `AEP_` 前缀：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `AEP_SQLSERVER_HOST` / `AEP_SQLSERVER_PORT` | `127.0.0.1` / `1433` | |
| `AEP_SQLSERVER_DATABASE` | `source` | 连接器只看这一个数据库 |
| `AEP_SQLSERVER_USER` / `AEP_SQLSERVER_PASSWORD` | `source_reader` / 本地演示密码 | SQL 登录名；客户环境放 `.env` 或 Key Vault |
| `AEP_SQLSERVER_CONNECT_TIMEOUT_SECONDS` | `5` | |
| `AEP_SQLSERVER_QUERY_TIMEOUT_SECONDS` | `5` | 每条语句的超时，也用作锁等待超时 |
| `AEP_SQLSERVER_STATS_SAMPLE_SIZE` | `1000` | `table_stats` 的样本行数 |
| `AEP_SQLSERVER_MCP_HOST` / `AEP_SQLSERVER_MCP_PORT` | `127.0.0.1` / `8007` | |

## 工具接口

工具名、参数和 JSON 字段和 PostgreSQL、MySQL 连接器一致，都带 `read_only_hint=true`。

| 工具 | 参数 | 返回字段 |
| --- | --- | --- |
| `list_schemas` | 无 | `schemas`：配置的数据库里有可读表的 schema（不含 `sys`、`INFORMATION_SCHEMA`、`guest`） |
| `list_tables` | `schema` | `schema`、`tables`：有整表 SELECT 权限的用户表，不含视图，按数据库排序规则排序 |
| `describe_table` | `schema`, `table` | `schema`、`table`、`columns`（`name`、`data_type` 如 `nvarchar(50)`、`decimal(12,2)`、`nullable`、`primary_key`）、`foreign_keys` |
| `sample_rows` | `schema`, `table`, `limit=10` | `schema`、`table`、`rows`，最多 100 行，不保证顺序 |
| `read_rows` | `schema`, `table`, `offset=0`, `limit=1000` | `schema`、`table`、`columns`、`rows`、`next_offset`；按主键顺序分页，没主键时按全部可排序的列 |
| `table_stats` | `schema`, `table` | `schema`、`table`、`estimated_row_count`（`sys.partitions` 的行数）、`sample_limit`、`sample_row_count`、`columns`（`name`、`null_fraction`、`distinct_count`，低基数列另有 `top_values`） |

值的编码：`decimal`、`money` 转成 JSON 数值；日期时间转 ISO 字符串；`uniqueidentifier` 转字符串；`bit`
转布尔；二进制转 `\x` 加十六进制；`geography`、`geometry`、`hierarchyid` 转文本（WKT 等）；`sql_variant`
转成字符串；`xml` 原样返回文本。`text`、`ntext`、`image`、`xml`、`geography`、`geometry` 不能用来排序，
没主键的表分页时跳过它们；如果所有列都不能排序，分页顺序不稳定。

统计的不同值在 Python 里按完整值计算，不受排序规则影响（大小写、首尾空格不同的值分开算）。
`top_values` 的规则和 MySQL 连接器一样（1–20 个不同值才返回，展示值超过 80 字符截断并标 `truncated`）。

## 只读保护

- 种子里的 `source_reader` 是 SQL 登录名，对应的数据库用户只有 `GRANT SELECT ON SCHEMA::sales`。
- 每次连接都检查权限，有多余权限就拒绝连接；启动时检查不通过，连接器启动失败：
  - 服务器级（`fn_my_permissions(NULL, 'SERVER')`）只允许 `CONNECT SQL`、`VIEW ANY DATABASE`。
    `sysadmin` 等服务器角色、`VIEW SERVER STATE` 都会被拒绝。
  - 数据库级只允许 `CONNECT`、`SELECT`、`VIEW DEFINITION`、`SHOWPLAN`、`VIEW DATABASE STATE`、
    `VIEW DATABASE PERFORMANCE STATE`，以及 public 角色自带的两项列加密密钥查看权限。所以
    `db_owner`、`db_datawriter`、`db_ddladmin` 都会被拒绝，`db_datareader` 可以。
  - 授给这个用户、或它所在的任何角色的 schema 级和对象级权限，只允许 `SELECT`、`VIEW DEFINITION`、
    `REFERENCES`、`VIEW CHANGE TRACKING`。`INSERT`、`UPDATE`（含列级）、`EXECUTE`、`ALTER`、
    `IMPERSONATE` 等都会被拒绝。
  - 拥有任何 schema（拥有就等于 CONTROL）也会被拒绝。
  - 用白名单而不是黑名单，所以以后版本新增的权限也会被拒绝。
- SQL Server 没有只读会话，所以每次工具调用都在一个事务里执行，结束时一律回滚。就算检查之后有人
  临时加了写权限，这次调用也写不进去。
- 只暴露六个固定工具，不接受任意 SQL。schema、表名用方括号引用，内嵌的 `]` 加倍转义；行数和偏移量
  先校验成整数再写进语句，不绑定参数（pymssql 的占位符解析会误读名字里的 `%`）。
- 驱动的错误信息可能带主机名、SQL 和数据，所以不原样返回，只说“请求失败，请检查访问和配置”或超时。

Azure SQL Database 没测过：它不支持服务器级的 `fn_my_permissions`，现在会被当成连接失败。
