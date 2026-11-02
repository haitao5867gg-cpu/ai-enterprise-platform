# Databricks 只读 MCP 连接器

独立的 Python 3.12 / uv 包，用官方 MCP Python SDK 提供 Streamable HTTP：`http://127.0.0.1:8005/mcp`（本地编排里是 `http://connector-databricks:8005/mcp`）。不依赖平台后端或模型网关。接口契约见 [docs/api/datasources.md](../../docs/api/datasources.md)“Databricks 数据源”。

## 能做什么

- 6 个和其他数据源一样的工具：`list_schemas`、`list_tables`、`describe_table`、`sample_rows`、`read_rows`、`table_stats`。schema 写成 `catalog.schema`，`sample_rows`、`read_rows`、`table_stats` 可以带 `where`。
- 3 个分析工具：`aggregate`、`duplicates`、`compare`，表名写成 `catalog.schema.table`。`aggregate` 和 `sample_rows` 可以带 `lookup`，只读在另一张表里找得到（或找不到）对应行的那些行，子查询由连接器自己拼。
- 只读：没有执行任意 SQL 的工具，语句都由连接器自己拼。名称校验后加反引号，`where`、`expr` 先做形状检查（不能有分号、注释、子查询；不能调反射、密钥、网络和文件读取、`ai_*`、`IDENTIFIER` 这些函数，也不能调带 catalog 或 schema 的自定义函数）。
- 元数据来自各 catalog 的 `information_schema`，所以 catalog 要在 Unity Catalog 里（不支持 `hive_metastore`）。
- 不复制数据，也不启动集群。

真正的边界是令牌的权限：`AEP_DATABRICKS_READ_TOKEN` 要用只有 `USE CATALOG`、`USE SCHEMA`、`SELECT` 权限的身份（服务主体或个人令牌）。用集群执行时，这个身份还要能挂到集群上（Can Attach To）。

## 在哪里执行

- 设了 `AEP_DATABRICKS_READ_WAREHOUSE_ID`：用这个 SQL warehouse（Statement Execution API，结果内联、分块读全）。warehouse 停着时 Databricks 会自动启动它，这要花钱，也要等一会儿。
- 否则用 `AEP_DATABRICKS_READ_CLUSTER_ID` 指定的已有集群（Command Execution API）。集群要已经在运行，连接器不替你启动。连接器在一个 Python 执行环境里跑一段固定的程序，语句以 JSON 字符串字面值传进去（不会当代码执行），结果按类型转成 JSON。

## 设置

| 变量 | 说明 |
| --- | --- |
| `AEP_DATABRICKS_READ_HOST` | 工作区地址 |
| `AEP_DATABRICKS_READ_TOKEN` | 只读身份的令牌，只配在连接器上 |
| `AEP_DATABRICKS_READ_WAREHOUSE_ID` / `AEP_DATABRICKS_READ_CLUSTER_ID` | 在哪里执行，二选一（都设时用 warehouse） |
| `AEP_DATABRICKS_READ_CATALOGS` | 允许读的 catalog，JSON 列表，必填，比如 `["icsd-ace-dev"]` |
| `AEP_DATABRICKS_READ_TIMEOUT_SECONDS` | 每条语句的超时，默认 150 |
| `AEP_DATABRICKS_READ_STATS_SAMPLE` | `table_stats` 的样本行数，默认 1000 |
| `AEP_DATABRICKS_MCP_HOST`、`AEP_DATABRICKS_MCP_PORT`、`AEP_DATABRICKS_MCP_ALLOWED_HOSTS` | MCP 服务 |

本地编排没设前三项时沿用平台的 `AEP_DATABRICKS_HOST`、`AEP_DATABRICKS_TOKEN`、`AEP_DATABRICKS_CLUSTER_ID`，方便开发；生产要单独配只读身份。

## 健康检查

- `GET /healthz`：不访问 Databricks，总是 200，说明配置齐不齐（`configured`、`missing`）。编排每 5 秒查一次，如果每次都执行查询，warehouse 或集群就永远停不下来。
- `GET /healthz?probe=1`：执行 `SELECT 1`，通了返回 200，否则返回 503 并说明原因（不带令牌，也不带 Databricks 的原始报错）。

## 测试

```bash
cd connectors/databricks
uv sync --frozen
uv run ruff check . && uv run ruff format --check .
uv run pytest -q
```

测试用假的执行接口和假的 REST 端点，不连真实的 Databricks。集群上跑的那段程序也在测试里用假的 `spark` 执行过一遍。

对真实工作区验收（不在 CI 里跑；只打印行数这类统计，不打印数据行）：

```bash
docker compose up --build -d --wait connector-databricks
uv run --frozen aep-databricks-smoke --table <catalog>.<schema>.<table> --key <主键列>
```
