# 上传文件连接器

只读 MCP Streamable HTTP，默认端口 **8003**。端点是
`/datasets/<UUID>/mcp`，`GET /healthz` 检查进程存活。
后端负责上传 CSV / XLSX，连接器只读取该 UUID 目录里的 manifest 与 Parquet。

与 PostgreSQL 连接器相同的五个工具及返回结构：`list_schemas`、`list_tables`、
`describe_table`、`sample_rows`（1～100 行）、`table_stats`。统计最多读取
`AEP_FILES_STATS_SAMPLE_SIZE` 行（默认 1000，最大 10000）；非空不同值数量为
1～20 时返回 `top_values`，按频次降序、值升序排列，按完整值计数后截至 80 字符并标记
`truncated`。不提供任意 SQL 工具。

每次查询使用 DuckDB `read_only=True` 连接。DuckDB 不支持只读内存数据库，所以先在
系统临时目录创建空目录库，再以只读模式打开；这里不存放上传数据。查询禁用外部访问、
扩展自动安装和自动加载，仅放行当前 manifest 中的 Parquet 绝对路径，随后锁定配置。
数据目录、manifest、Parquet 均拒绝符号链接，文件路径不能跨目录。标识符必须来自
manifest 且始终加引号。Compose 额外把 uploads 卷以 `:ro` 挂载，容器以非 root 用户运行。

配置集中于 `aep_files.config.Settings`（独立连接器包，沿用其他连接器的约定），环境变量
统一使用 `AEP_` 前缀：`AEP_UPLOADS_DIR`（默认 `/data/uploads`）、
`AEP_FILES_MCP_HOST`、`AEP_FILES_MCP_PORT`、`AEP_FILES_STATS_SAMPLE_SIZE`，以及
`AEP_FILES_MCP_ALLOWED_HOSTS` / `AEP_FILES_MCP_ALLOWED_ORIGINS`（JSON 数组）。
API 配置通过 `aep.config.Settings` 读取。

```bash
cd connectors/files
uv sync --frozen
uv run pytest -q
uv run aep-files
uv run aep-files-smoke --url http://localhost:8003/datasets/<UUID>/mcp
```

从仓库根目录对真实 Compose 栈验证上传登记、五个工具与只读挂载：

```bash
docker compose up --build -d --wait
scripts/smoke-files.sh
```
