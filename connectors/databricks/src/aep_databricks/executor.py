"""Where statements run: a SQL warehouse (Statement Execution API) or an existing,
running cluster (Command Execution API). Both give the same ``Result``: column names and
types, and rows of JSON values (numbers, booleans, text; dates and timestamps as ISO
text; arrays, maps and structs as JSON)."""

import asyncio
import json
import math
import time
from dataclasses import dataclass
from typing import Any, Protocol

import httpx2 as httpx

from aep_databricks.config import Settings
from aep_databricks.sql import ConnectorError

POLL_SECONDS = 1.0
# The Statement Execution API waits this long before answering with a pending statement.
WAIT = "30s"
MAX_ERROR = 500


@dataclass
class Result:
    # (name, type) with types as Spark writes them, lower case: string, decimal(18,2).
    columns: list[tuple[str, str]]
    rows: list[list[Any]]

    def dicts(self) -> list[dict[str, Any]]:
        names = [name for name, _ in self.columns]
        return [dict(zip(names, row, strict=True)) for row in self.rows]


class Executor(Protocol):
    async def query(self, statement: str) -> Result: ...

    async def close(self) -> None: ...


class StatementError(ConnectorError):
    """Databricks refused or failed a statement; the message is Databricks' own."""


def _error(message: object) -> str:
    text = " ".join(str(message or "").split())
    return text[:MAX_ERROR] + ("…" if len(text) > MAX_ERROR else "")


class _Rest:
    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None) -> None:
        self.timeout = settings.databricks_read_timeout_seconds
        self._client = httpx.AsyncClient(
            base_url=settings.databricks_read_host.rstrip("/"),
            headers={
                "Authorization": f"Bearer {settings.databricks_read_token.get_secret_value()}"
            },
            timeout=httpx.Timeout(60.0),
            transport=transport,
        )

    async def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError:
            # Transport errors can carry the workspace address: say only what happened.
            raise ConnectorError("连不上 Databricks 工作区，请检查地址和网络") from None
        if response.status_code in (401, 403):
            raise ConnectorError("Databricks 拒绝了令牌（没有权限或已过期）")
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400:
            raise StatementError(f"Databricks 报错：{_error(body.get('message') or response.text)}")
        return body if isinstance(body, dict) else {}

    async def close(self) -> None:
        await self._client.aclose()


# SQL warehouse

_INTEGERS = {"BYTE", "SHORT", "INT", "LONG"}
_COMPLEX = {"ARRAY", "MAP", "STRUCT"}


def _number(text: str) -> Any:
    value = float(text)
    return value if math.isfinite(value) else text


def _decimal(text: str) -> Any:
    if "." not in text and "e" not in text.lower():
        return int(text)
    return _number(text)


def _value(type_name: str, text: Any) -> Any:
    """A value of the JSON_ARRAY format (always text) as JSON."""
    if text is None:
        return None
    if type_name in _INTEGERS:
        return int(text)
    if type_name in ("FLOAT", "DOUBLE"):
        return _number(text)
    if type_name == "DECIMAL":
        return _decimal(text)
    if type_name == "BOOLEAN":
        return text == "true"
    if type_name in _COMPLEX:
        try:
            return json.loads(text)
        except ValueError:
            return text
    return text


class WarehouseExecutor:
    """Statements on a SQL warehouse, results inline as JSON arrays (all chunks)."""

    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._rest = _Rest(settings, transport)
        self._warehouse = settings.databricks_read_warehouse_id

    async def query(self, statement: str) -> Result:
        rest = self._rest
        deadline = time.monotonic() + rest.timeout
        body = await rest.request(
            "POST",
            "/api/2.0/sql/statements",
            json={
                "statement": statement,
                "warehouse_id": self._warehouse,
                "wait_timeout": WAIT,
                "on_wait_timeout": "CONTINUE",
                "disposition": "INLINE",
                "format": "JSON_ARRAY",
            },
        )
        statement_id = body.get("statement_id")
        while (body.get("status") or {}).get("state") in ("PENDING", "RUNNING"):
            if time.monotonic() > deadline:
                await rest.request("POST", f"/api/2.0/sql/statements/{statement_id}/cancel")
                raise ConnectorError(f"查询超过 {rest.timeout} 秒没有结束，已取消")
            await asyncio.sleep(POLL_SECONDS)
            body = await rest.request("GET", f"/api/2.0/sql/statements/{statement_id}")
        status = body.get("status") or {}
        if status.get("state") != "SUCCEEDED":
            error = status.get("error") or {}
            raise StatementError(
                f"Databricks 报错：{_error(error.get('message') or status.get('state'))}"
            )
        schema = ((body.get("manifest") or {}).get("schema") or {}).get("columns") or []
        columns = [
            (c["name"], str(c.get("type_text") or c.get("type_name")).lower()) for c in schema
        ]
        types = [str(c.get("type_name") or "").upper() for c in schema]
        result = body.get("result") or {}
        data = list(result.get("data_array") or [])
        while result.get("next_chunk_internal_link"):
            result = await rest.request("GET", result["next_chunk_internal_link"])
            data += result.get("data_array") or []
        rows = [[_value(t, v) for t, v in zip(types, row, strict=True)] for row in data]
        return Result(columns, rows)

    async def close(self) -> None:
        await self._rest.close()


# Cluster

MARK = "AEP-RESULT"
# Runs one statement on the cluster and prints its columns and rows as JSON. The
# statement goes in as a JSON string literal (also a valid Python literal), never as code.
PROGRAM = """
import json as _aep_json, datetime as _aep_dt, decimal as _aep_dec
import base64 as _aep_b64, math as _aep_math
def _aep_value(v):
    if v is None or isinstance(v, (bool, int, str)):
        return v
    if isinstance(v, float):
        return v if _aep_math.isfinite(v) else str(v)
    if isinstance(v, _aep_dec.Decimal):
        if not v.is_finite():
            return str(v)
        return int(v) if v.as_tuple().exponent >= 0 else float(v)
    if isinstance(v, (_aep_dt.datetime, _aep_dt.date, _aep_dt.time)):
        return v.isoformat()
    if isinstance(v, (bytes, bytearray)):
        return _aep_b64.b64encode(bytes(v)).decode()
    if hasattr(v, "asDict"):
        return {k: _aep_value(x) for k, x in v.asDict(recursive=False).items()}
    if isinstance(v, dict):
        return {str(k): _aep_value(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_aep_value(x) for x in v]
    return str(v)
_aep_df = spark.sql(__STATEMENT__)
_aep_out = _aep_json.dumps({
    "columns": [[f.name, f.dataType.simpleString()] for f in _aep_df.schema.fields],
    "rows": [[_aep_value(x) for x in r] for r in _aep_df.collect()],
}, ensure_ascii=True, allow_nan=False)
print("__MARK__ %d %s" % (len(_aep_out), _aep_out))
"""


def program(statement: str) -> str:
    return PROGRAM.replace("__STATEMENT__", json.dumps(statement)).replace("__MARK__", MARK)


def parse_output(text: str) -> Result:
    start = text.rfind(MARK + " ")
    if start < 0:
        raise ConnectorError("集群没有返回结果")
    size, _, rest = text[start + len(MARK) + 1 :].partition(" ")
    if not size.isdigit() or len(rest.rstrip("\n")) < int(size):
        raise ConnectorError("结果太大，被集群截断了；请减小 limit 或加条件")
    try:
        data = json.loads(rest[: int(size)])
    except ValueError:
        raise ConnectorError("结果太大，被集群截断了；请减小 limit 或加条件") from None
    return Result([(name, kind) for name, kind in data["columns"]], data["rows"])


class ClusterExecutor:
    """Statements on an existing cluster, in one Python context reused across calls.
    The connector never starts the cluster."""

    def __init__(
        self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._rest = _Rest(settings, transport)
        self._cluster = settings.databricks_read_cluster_id
        self._context: str | None = None
        self._lock = asyncio.Lock()

    async def _running(self) -> None:
        cluster = await self._rest.request(
            "GET", "/api/2.0/clusters/get", params={"cluster_id": self._cluster}
        )
        if cluster.get("state") != "RUNNING":
            raise ConnectorError(f"集群 {self._cluster} 没有在运行，请先在 Databricks 上启动它")

    async def _context_id(self, deadline: float) -> str:
        async with self._lock:
            if self._context is None:
                made = await self._rest.request(
                    "POST",
                    "/api/1.2/contexts/create",
                    json={"clusterId": self._cluster, "language": "python"},
                )
                ids = {"clusterId": self._cluster, "contextId": made["id"]}
                while True:
                    status = await self._rest.request("GET", "/api/1.2/contexts/status", params=ids)
                    if status.get("status") == "Running":
                        break
                    if status.get("status") == "Error" or time.monotonic() > deadline:
                        raise ConnectorError("集群上的执行环境没有就绪")
                    await asyncio.sleep(POLL_SECONDS)
                self._context = made["id"]
            return self._context

    async def query(self, statement: str) -> Result:
        await self._running()
        deadline = time.monotonic() + self._rest.timeout
        try:
            return await self._execute(statement, deadline)
        except _ContextGone:
            # The cluster restarted or dropped the context: make a new one, once.
            self._context = None
            return await self._execute(statement, deadline)

    async def _execute(self, statement: str, deadline: float) -> Result:
        context = await self._context_id(deadline)
        ids = {"clusterId": self._cluster, "contextId": context}
        try:
            command = await self._rest.request(
                "POST",
                "/api/1.2/commands/execute",
                json=ids | {"language": "python", "command": program(statement)},
            )
        except StatementError as exc:
            if "context" in str(exc).lower():
                raise _ContextGone from exc
            raise
        while True:
            status = await self._rest.request(
                "GET", "/api/1.2/commands/status", params=ids | {"commandId": command["id"]}
            )
            if status.get("status") in ("Finished", "Error", "Cancelled"):
                break
            if time.monotonic() > deadline:
                await self._rest.request(
                    "POST", "/api/1.2/commands/cancel", json=ids | {"commandId": command["id"]}
                )
                raise ConnectorError(f"查询超过 {self._rest.timeout} 秒没有结束，已取消")
            await asyncio.sleep(POLL_SECONDS)
        results = status.get("results") or {}
        if status.get("status") != "Finished" or results.get("resultType") == "error":
            cause = results.get("summary") or results.get("cause") or status.get("status")
            raise StatementError(f"Databricks 报错：{_error(_last_line(cause))}")
        return parse_output(str(results.get("data") or ""))

    async def close(self) -> None:
        if self._context is not None:
            try:
                await self._rest.request(
                    "POST",
                    "/api/1.2/contexts/destroy",
                    json={"clusterId": self._cluster, "contextId": self._context},
                )
            except ConnectorError:
                pass
            self._context = None
        await self._rest.close()


class _ContextGone(Exception):
    pass


def _last_line(cause: object) -> str:
    """The error itself, not the Python traceback around it (no program, no values)."""
    lines = [line for line in str(cause or "").splitlines() if line.strip()]
    for line in reversed(lines):
        if "Exception" in line or "Error" in line:
            return line.strip()
    return lines[-1] if lines else ""


def make_executor(
    settings: Settings, transport: httpx.AsyncBaseTransport | None = None
) -> Executor:
    if settings.databricks_read_warehouse_id:
        return WarehouseExecutor(settings, transport)
    if settings.databricks_read_cluster_id:
        return ClusterExecutor(settings, transport)
    raise ConnectorError("连接器没有配置在哪里执行查询")
