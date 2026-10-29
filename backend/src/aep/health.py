"""Liveness and readiness checks for the platform's backing services, and the ETL
demo's checks (``GET /health/demo``)."""

from typing import Any
from urllib.parse import urlparse

import httpx
import psycopg
import redis.asyncio as redis

from aep.config import Settings
from aep.databricks import DatabricksError, DevTarget
from aep.datasources import DataSourceStore
from aep.tools.mcp import McpTools


async def check_postgres(settings: Settings) -> str | None:
    """Return None when Postgres answers, otherwise the error message."""
    try:
        async with await psycopg.AsyncConnection.connect(
            settings.database_url, connect_timeout=2
        ) as conn:
            await conn.execute("SELECT 1")
    except Exception as exc:  # noqa: BLE001 - any failure means "not ready"
        return str(exc)
    return None


async def check_redis(settings: Settings) -> str | None:
    """Return None when Redis answers, otherwise the error message."""
    client = redis.from_url(settings.redis_url, socket_connect_timeout=2)
    try:
        await client.ping()
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    finally:
        await client.aclose()
    return None


async def check_llm_gateway(settings: Settings) -> str | None:
    """Return None when the model gateway is up, otherwise the error message."""
    url = f"{settings.llm_gateway_url.rstrip('/')}/health/liveliness"
    try:
        async with httpx.AsyncClient(timeout=2) as client:
            resp = await client.get(url)
            resp.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        return str(exc)
    return None


# The ETL demo (``make demo``, docs/api/data-nodes.md): what has to be in place before
# the demo app can run from the request to the dev run.
DEMO_DATASOURCE = "MySQL 示例库"
DEMO_SCHEMA = "retail"
CONNECTOR_SECONDS = 15.0


def _item(
    key: str, ok: bool, detail: str, required: bool = True, value: str | None = None
) -> dict[str, Any]:
    return {"key": key, "ok": ok, "detail": detail, "required": required, "value": value}


async def demo_checks(
    settings: Settings,
    models: list[str] | None,
    sources: DataSourceStore,
    tools: McpTools,
    databricks: DevTarget | None,
) -> list[dict[str, Any]]:
    """The demo's checks, each ``{"key", "ok", "detail", "required", "value"}``
    (``value``: the alias, for ``model`` and ``embedding``); no secret's value appears. Without
    Databricks the app still gets made (``required`` false), and the run fails when
    it reaches the ingest."""
    items = [_item("api", True, "平台接口正常")]
    alias = settings.demo_model
    if models is None:
        detail = f"模型网关没有应答，不知道有没有 {alias}"
        items.append(_item("model", False, detail, value=alias))
    elif alias in models:
        items.append(_item("model", True, f"模型网关上有 {alias}", value=alias))
    else:
        items.append(
            _item(
                "model",
                False,
                f"模型网关上没有 {alias}（现有：{'、'.join(models)}）；"
                "在 infra/litellm/config.yaml 里加上，或用 AEP_DEMO_MODEL 换一个",
                value=alias,
            )
        )
    items.append(_demo_embedding(settings, models))
    items.append(await _demo_source(sources, tools))

    s = settings
    if not (s.databricks_host and s.databricks_token):
        items.append(
            _item(
                "databricks",
                False,
                "没有配置 AEP_DATABRICKS_HOST、AEP_DATABRICKS_TOKEN：应用照样建好，"
                "运行到「抽取到湖仓」时会失败",
                required=False,
            )
        )
        return items
    host = urlparse(s.databricks_host).hostname or s.databricks_host
    items.append(_item("databricks", True, f"工作区 {host}", required=False))
    items.append(await _demo_cluster(s, databricks))
    if s.databricks_source_catalog:
        detail = f"源表抽取到 {s.databricks_source_catalog}.{DEMO_SCHEMA}"
        items.append(_item("databricks_source_catalog", True, detail, required=False))
    else:
        detail = "没有配置 AEP_DATABRICKS_SOURCE_CATALOG：不知道把源表抽取到哪个 catalog"
        items.append(_item("databricks_source_catalog", False, detail, required=False))
    if s.databricks_catalog and s.databricks_schema:
        detail = f"结果表写到 {s.databricks_catalog}.{s.databricks_schema}"
        items.append(_item("databricks_target", True, detail, required=False))
    else:
        detail = (
            "没有配置 AEP_DATABRICKS_CATALOG、AEP_DATABRICKS_SCHEMA：结果表写到 bundle 默认的"
            "位置，平台读不到结果预览"
        )
        items.append(_item("databricks_target", False, detail, required=False))
    return items


def _demo_embedding(settings: Settings, models: list[str] | None) -> dict[str, Any]:
    alias = settings.demo_embedding_model
    other = "；或用 AEP_DEMO_EMBEDDING_MODEL 换一个（mock-embed 不需要模型网关）"
    if alias not in settings.embedding_models:
        detail = f"向量模型 {alias} 不在 AEP_EMBEDDING_MODELS 里{other}"
        return _item("embedding", False, detail, value=alias)
    if alias == "mock-embed" or (models is not None and alias in models):
        return _item("embedding", True, f"知识库用向量模型 {alias}", value=alias)
    if models is None:
        detail = f"模型网关没有应答，不知道有没有向量模型 {alias}"
    else:
        detail = f"模型网关上没有向量模型 {alias}{other}"
    return _item("embedding", False, detail, value=alias)


async def _demo_source(sources: DataSourceStore, tools: McpTools) -> dict[str, Any]:
    found = next((d for d in await sources.all() if d.name == DEMO_DATASOURCE), None)
    if found is None:
        return _item(
            "datasource",
            False,
            f"没有登记数据源「{DEMO_DATASOURCE}」（docker-compose.yml 的 AEP_DEFAULT_DATASOURCES）",
        )
    try:
        listed = await tools.call(found.connector_url, "list_schemas", {}, CONNECTOR_SECONDS)
    except Exception as exc:  # noqa: BLE001 - any failure means "cannot read it"
        return _item("datasource", False, f"读不了数据源「{DEMO_DATASOURCE}」：{exc}")
    if DEMO_SCHEMA not in (listed or {}).get("schemas", []):
        return _item(
            "datasource",
            False,
            f"数据源「{DEMO_DATASOURCE}」里没有 {DEMO_SCHEMA} 库：先导入 "
            "connectors/mysql/seed/003-retail.sql（make demo 会做）",
        )
    return _item("datasource", True, f"数据源「{DEMO_DATASOURCE}」能读到 {DEMO_SCHEMA} 库")


async def _demo_cluster(s: Settings, databricks: DevTarget | None) -> dict[str, Any]:
    cluster_id = s.databricks_cluster_id
    if not cluster_id or databricks is None:
        return _item(
            "databricks_cluster",
            False,
            "没有配置 AEP_DATABRICKS_CLUSTER_ID：抽取和 dev 运行都要在已有集群上跑",
            required=False,
        )
    try:
        state = await databricks.cluster_state()
    except DatabricksError as exc:
        return _item(
            "databricks_cluster",
            False,
            f"查不到集群 {cluster_id}（令牌过期或没有权限？）：{str(exc)[:300]}",
            required=False,
        )
    if state == "RUNNING":
        detail = f"集群 {cluster_id} 正在运行"
    elif state == "TERMINATED":
        detail = (
            f"集群 {cluster_id} 已停止：运行到抽取时平台会自动启动，要等几分钟；"
            "演示前可以先在 Databricks 里启动它"
        )
    else:
        detail = f"集群 {cluster_id} 的状态是 {state}"
    return _item("databricks_cluster", True, detail, required=False)
