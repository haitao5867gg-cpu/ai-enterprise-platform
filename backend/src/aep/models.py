"""Models: the aliases the LiteLLM gateway offers, managed from the model page.

The gateway is the only list of models. Those in ``infra/litellm/config.yaml`` are read
only here; models added on the page are stored by the gateway in its own database
(``store_model_in_db``), keys included and encrypted there. The platform never stores or
returns a key: it only notes in the model's ``model_info`` whether the key was pasted on
the page or names a server environment variable. Every model call the platform makes is
recorded in ``model_calls`` (``aep.llm.set_recorder``). Contract: docs/api/models.md.
"""

import asyncio
import json
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal, Protocol
from zoneinfo import ZoneInfo

import httpx
from openai import AsyncOpenAI
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from aep import llm
from aep.config import Settings
from aep.connector_urls import (
    Rejected,
    _internal,
    _is_loopback,
    _is_metadata,
    _parse,
    internal_targets,
)
from aep.connector_urls import _credentials as url_credentials
from aep.policy import platform_default
from aep.runtime.store import DEFAULT_TENANT

log = logging.getLogger(__name__)

Purpose = Literal["chat", "embedding", "rerank"]
PURPOSES: tuple[Purpose, ...] = ("chat", "embedding", "rerank")
PURPOSE_LABELS = {"chat": "对话", "embedding": "向量", "rerank": "重排"}
GatewayState = Literal["ok", "no_db", "down"]

ALIAS = re.compile(r"^[a-z][a-z0-9_-]{1,39}$")
KEY_ENV = re.compile(r"^[A-Z][A-Z0-9_]*_API_KEY$")
ID_PREFIX = "aep-"
USAGE_DAYS = 30
KEEP_DAYS = 180
TEST_SECONDS = 30.0
GATEWAY_SECONDS = 10.0
CACHE_SECONDS = 10.0
REPLY_CHARS = 200
REASON_CHARS = 200

PROBLEMS = {
    "auth": "认证失败：key 不对或已失效。",
    "quota": "额度不足或被限流：请检查这个账号的余额和限额，稍后再试。",
    "not_found": "模型不存在：供应商那边没有这个型号，或者这个 key 没有权限用它。",
    "bad_request": "请求被拒绝：{reason}。",
    "unreachable": (
        "连不上模型服务：网关所在的服务器访问不到这个地址（有的供应商不对部分地区开放）。"
    ),
    "timeout": "超时：模型在 30 秒内没有回复。",
    "dimensions": "向量维度不对：平台需要 1024 维，这个模型给的是 {n} 维，或者它不支持指定维度。",
    "unsupported": "重排模型暂不支持在页面上测试。",
    "gateway": "模型网关报错：{reason}。",
}

NOT_FOUND = "没有找到这个模型"
NO_DB = (
    "模型网关没有接数据库，只能在配置文件里加模型。"
    "请按部署说明给网关配置 DATABASE_URL 和 LITELLM_SALT_KEY"
)
DOWN = "连不上模型网关"
BAD_KEY_ENV = (
    "环境变量名要以 _API_KEY 结尾，只能有大写字母、数字和下划线，不能以 LITELLM_ 或 AEP_ 开头"
)
LOOPBACK = "不能用本机地址，网关在容器里访问不到它"
BASE_TEXT = {
    "bad_url": (
        "地址格式不对。请填完整的地址，例如 https://api.example.com/v1："
        "以 http:// 或 https:// 开头，不含空格、中文和 # 号，端口在 1～65535 之间。"
    ),
    "credentials": (
        "地址里不能带账号密码，也不能带 password、token、secret、key 这类参数。"
        "key 请填在 key 一栏。"
    ),
}


@dataclass(frozen=True)
class Provider:
    label: str
    prefix: str
    # Fields a new model of this provider must have, besides its model id.
    needs: tuple[str, ...] = ()
    # Whether a key is required (Bedrock uses the gateway's AWS credentials).
    key: bool = True
    # Fields it may have.
    allows: tuple[str, ...] = ()


PROVIDERS: dict[str, Provider] = {
    "anthropic": Provider("Anthropic（Claude）", "anthropic/"),
    "openai": Provider("OpenAI", "openai/", allows=("api_base",)),
    "azure": Provider("Azure OpenAI", "azure/", needs=("api_base", "api_version")),
    "bedrock": Provider("AWS Bedrock", "bedrock/", needs=("aws_region",), key=False),
    "dashscope": Provider("通义千问（DashScope）", "dashscope/"),
    "deepseek": Provider("DeepSeek", "deepseek/"),
    "openai_compatible": Provider("OpenAI 兼容网关", "openai/", needs=("api_base",)),
}
# What a config model's LiteLLM prefix says about its provider.
PREFIXES = {
    "anthropic": "anthropic",
    "openai": "openai",
    "azure": "azure",
    "bedrock": "bedrock",
    "dashscope": "dashscope",
    "deepseek": "deepseek",
}
FIELD_LABELS = {
    "model": "型号",
    "api_base": "地址",
    "api_version": "API 版本",
    "aws_region": "区域",
    "key": "key",
}


class ModelError(Exception):
    """A request the model API refuses: ``status`` and a Chinese ``detail``."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class GatewayDown(Exception):
    pass


class GatewayFailed(Exception):
    pass


def problem(code: str, **values: Any) -> dict[str, str]:
    text = PROBLEMS[code]
    values.setdefault("reason", "")
    values.setdefault("n", "?")
    return {"code": code, "message": text.format(**values)}


# The gateway's management API


class GatewayAdmin:
    """LiteLLM's model management endpoints, called with the master key."""

    def __init__(self, base_url: str, key: str, client: httpx.AsyncClient | None = None):
        self._client = client or httpx.AsyncClient(timeout=GATEWAY_SECONDS)
        self._base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {key}"}

    @classmethod
    def from_settings(
        cls, settings: Settings, client: httpx.AsyncClient | None = None
    ) -> "GatewayAdmin":
        return cls(settings.llm_gateway_url, settings.llm_gateway_key, client)

    async def close(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, path: str, body: Any = None) -> Any:
        try:
            resp = await self._client.request(
                method, f"{self._base}{path}", headers=self._headers, json=body
            )
        except httpx.HTTPError as exc:
            raise GatewayDown(str(exc)) from exc
        if resp.status_code >= 400:
            raise GatewayFailed(_gateway_reason(resp))
        return resp.json() if resp.content else None

    async def state(self) -> GatewayState:
        try:
            ready = await self._call("GET", "/health/readiness")
        except (GatewayDown, GatewayFailed):
            return "down"
        db = str((ready or {}).get("db", "")).lower()
        return "ok" if db == "connected" else "no_db"

    async def deployments(self) -> list[dict[str, Any]]:
        found = await self._call("GET", "/model/info")
        return list((found or {}).get("data") or [])

    async def add(self, alias: str, params: dict[str, Any], info: dict[str, Any]) -> None:
        await self._call(
            "POST",
            "/model/new",
            {"model_name": alias, "litellm_params": params, "model_info": info},
        )

    async def update(
        self, model_id: str, params: dict[str, Any] | None, info: dict[str, Any] | None
    ) -> None:
        body: dict[str, Any] = {}
        if params:
            body["litellm_params"] = params
        if info:
            body["model_info"] = info
        await self._call("PATCH", f"/model/{model_id}/update", body)

    async def delete(self, model_id: str) -> None:
        await self._call("POST", "/model/delete", {"id": model_id})


def _gateway_reason(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        error = body.get("error") if isinstance(body, dict) else None
        message = error.get("message") if isinstance(error, dict) else body.get("detail")
    except (ValueError, AttributeError):
        message = None
    return str(message or resp.text or resp.status_code)[:REASON_CHARS]


# Call records and platform settings


@dataclass(frozen=True)
class UsageSummary:
    calls: int = 0
    errors: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float | None = None
    last_call_at: datetime | None = None
    last_error: str | None = None  # the code of the last call when it failed


@dataclass(frozen=True)
class CallRollup:
    """Calls made from one run (None: outside runs) on one day of the platform timezone,
    for the operations dashboard (docs/api/ops.md)."""

    run_id: str | None
    day: date
    calls: int = 0
    unpriced: int = 0  # calls the gateway reported no cost for
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float | None = None


class CallStore(Protocol):
    async def setup(self) -> None: ...
    async def record(self, call: llm.CallRecord) -> None: ...
    async def summary(self, since: datetime) -> dict[str, UsageSummary]: ...
    async def rollup(self, since: datetime, timezone: str) -> list[CallRollup]: ...
    async def prune(self, before: datetime) -> int: ...
    async def for_run(self, run_id: str) -> list[tuple[datetime, llm.CallRecord]]: ...


class SettingStore(Protocol):
    async def setup(self) -> None: ...
    async def get(self, key: str) -> Any: ...
    async def set(self, key: str, value: Any) -> None: ...


@dataclass
class MemoryCallStore:
    calls: list[tuple[datetime, llm.CallRecord]] = field(default_factory=list)

    async def setup(self) -> None:
        return None

    async def record(self, call: llm.CallRecord) -> None:
        self.calls.append((datetime.now(UTC), call))

    async def summary(self, since: datetime) -> dict[str, UsageSummary]:
        found: dict[str, UsageSummary] = {}
        for at, call in sorted(self.calls, key=lambda item: item[0]):
            if at < since:
                continue
            found[call.model] = _add(found.get(call.model, UsageSummary()), at, call)
        return found

    async def for_run(self, run_id: str) -> list[tuple[datetime, llm.CallRecord]]:
        return [(at, c) for at, c in self.calls if c.run_id == run_id]

    async def rollup(self, since: datetime, timezone: str) -> list[CallRollup]:
        zone = ZoneInfo(timezone)
        found: dict[tuple[str | None, date], CallRollup] = {}
        for at, call in self.calls:
            if at < since:
                continue
            key = (call.run_id, at.astimezone(zone).date())
            old = found.get(key, CallRollup(*key))
            cost = old.cost_usd
            if call.cost_usd is not None:
                cost = (cost or 0.0) + call.cost_usd
            found[key] = CallRollup(
                *key,
                calls=old.calls + 1,
                unpriced=old.unpriced + (call.cost_usd is None),
                prompt_tokens=old.prompt_tokens + call.prompt_tokens,
                completion_tokens=old.completion_tokens + call.completion_tokens,
                cost_usd=cost,
            )
        return list(found.values())

    async def prune(self, before: datetime) -> int:
        kept = [(at, c) for at, c in self.calls if at >= before]
        pruned = len(self.calls) - len(kept)
        self.calls = kept
        return pruned


def _add(summary: UsageSummary, at: datetime, call: llm.CallRecord) -> UsageSummary:
    cost = summary.cost_usd
    if call.cost_usd is not None:
        cost = (cost or 0.0) + call.cost_usd
    return UsageSummary(
        calls=summary.calls + 1,
        errors=summary.errors + (0 if call.ok else 1),
        prompt_tokens=summary.prompt_tokens + call.prompt_tokens,
        completion_tokens=summary.completion_tokens + call.completion_tokens,
        cost_usd=cost,
        last_call_at=at,
        last_error=None if call.ok else call.error,
    )


@dataclass
class MemorySettingStore:
    values: dict[str, Any] = field(default_factory=dict)

    async def setup(self) -> None:
        return None

    async def get(self, key: str) -> Any:
        return self.values.get(key)

    async def set(self, key: str, value: Any) -> None:
        self.values[key] = value


SCHEMA = [
    """
CREATE TABLE IF NOT EXISTS model_calls (
    id                bigserial PRIMARY KEY,
    tenant_id         text NOT NULL DEFAULT 'default',
    at                timestamptz NOT NULL DEFAULT now(),
    model             text NOT NULL,
    kind              text NOT NULL,
    run_id            text,
    node              text,
    prompt_tokens     integer NOT NULL DEFAULT 0,
    completion_tokens integer NOT NULL DEFAULT 0,
    cost_usd          double precision,
    ok                boolean NOT NULL,
    error             text,
    duration_ms       integer NOT NULL DEFAULT 0
)""",
    "CREATE INDEX IF NOT EXISTS model_calls_at ON model_calls (tenant_id, at)",
    "CREATE INDEX IF NOT EXISTS model_calls_run ON model_calls (run_id) WHERE run_id IS NOT NULL",
    """
CREATE TABLE IF NOT EXISTS platform_settings (
    tenant_id  text NOT NULL DEFAULT 'default',
    key        text NOT NULL,
    value      jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, key)
)""",
]


class PostgresCallStore:
    """Call records and platform settings in Postgres (dict rows, autocommit)."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            for statement in SCHEMA:
                await conn.execute(statement)

    async def record(self, call: llm.CallRecord) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO model_calls (model, kind, run_id, node, prompt_tokens,"
                " completion_tokens, cost_usd, ok, error, duration_ms)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    call.model,
                    call.kind,
                    call.run_id,
                    call.node,
                    call.prompt_tokens,
                    call.completion_tokens,
                    call.cost_usd,
                    call.ok,
                    call.error,
                    call.duration_ms,
                ),
            )

    async def summary(self, since: datetime) -> dict[str, UsageSummary]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT model, count(*) AS calls, count(*) FILTER (WHERE NOT ok) AS errors,"
                " coalesce(sum(prompt_tokens), 0) AS prompt_tokens,"
                " coalesce(sum(completion_tokens), 0) AS completion_tokens,"
                " sum(cost_usd) AS cost_usd, max(at) AS last_call_at,"
                " (array_agg(CASE WHEN ok THEN NULL ELSE coalesce(error, 'gateway') END"
                "   ORDER BY at DESC, id DESC))[1] AS last_error"
                " FROM model_calls WHERE tenant_id = %s AND at >= %s GROUP BY model",
                (DEFAULT_TENANT, since),
            )
            return {
                row["model"]: UsageSummary(
                    calls=row["calls"],
                    errors=row["errors"],
                    prompt_tokens=int(row["prompt_tokens"]),
                    completion_tokens=int(row["completion_tokens"]),
                    cost_usd=row["cost_usd"],
                    last_call_at=row["last_call_at"],
                    last_error=row["last_error"],
                )
                for row in await cur.fetchall()
            }

    async def rollup(self, since: datetime, timezone: str) -> list[CallRollup]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT run_id, (at AT TIME ZONE %s)::date AS day, count(*)::integer AS calls,"
                " count(*) FILTER (WHERE cost_usd IS NULL)::integer AS unpriced,"
                " coalesce(sum(prompt_tokens), 0)::bigint AS prompt_tokens,"
                " coalesce(sum(completion_tokens), 0)::bigint AS completion_tokens,"
                " sum(cost_usd) AS cost_usd FROM model_calls"
                " WHERE tenant_id = %s AND at >= %s GROUP BY run_id, day",
                (timezone, DEFAULT_TENANT, since),
            )
            return [CallRollup(**row) for row in await cur.fetchall()]

    async def for_run(self, run_id: str) -> list[tuple[datetime, llm.CallRecord]]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT at, model, kind, run_id, node, prompt_tokens, completion_tokens,"
                " cost_usd, ok, error, duration_ms FROM model_calls"
                " WHERE tenant_id = %s AND run_id = %s ORDER BY at",
                (DEFAULT_TENANT, run_id),
            )
            rows = await cur.fetchall()
        return [(row.pop("at"), llm.CallRecord(**row)) for row in rows]

    async def prune(self, before: datetime) -> int:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM model_calls WHERE tenant_id = %s AND at < %s",
                (DEFAULT_TENANT, before),
            )
            return cur.rowcount

    async def get(self, key: str) -> Any:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT value FROM platform_settings WHERE tenant_id = %s AND key = %s",
                (DEFAULT_TENANT, key),
            )
            row = await cur.fetchone()
            return row["value"] if row else None

    async def set(self, key: str, value: Any) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO platform_settings (tenant_id, key, value) VALUES (%s, %s, %s)"
                " ON CONFLICT (tenant_id, key)"
                " DO UPDATE SET value = excluded.value, updated_at = now()",
                (DEFAULT_TENANT, key, Jsonb(value)),
            )


# Checking what a new or changed model may be


def check_api_base(url: str, settings: Settings) -> str:
    """The address, stripped, if a model may be called there; ModelError otherwise. Rules 1-4
    of the connector address check (docs/api/datasources.md), no loopback, no allow list."""
    text = url.strip()
    try:
        address = _parse(text)
        url_credentials(text)
        if address.ip is not None and _is_metadata(address.ip):
            raise Rejected("metadata")
        if _is_loopback(address.host):
            raise ModelError(422, LOOPBACK)
        if _internal(address, internal_targets(settings)):
            raise Rejected("internal_host")
    except Rejected as exc:
        raise ModelError(422, BASE_TEXT.get(exc.code, exc.message)) from exc
    return text


def check_key(key: dict[str, Any] | None, provider: Provider) -> dict[str, Any] | None:
    """The key as given, checked: ``{"source": "page", "value"}`` or ``{"source": "env",
    "env"}``; None when no key is given."""
    if key is None:
        return None
    if not provider.key:
        raise ModelError(422, f"{provider.label}不用填 key，网关用它所在环境里的凭据")
    if key.get("source") == "page":
        value = key.get("value")
        if not isinstance(value, str) or not re.fullmatch(r"[\x21-\x7e]{1,500}", value.strip()):
            raise ModelError(422, "「key」的格式、长度或范围不正确")
        return {"source": "page", "value": value.strip()}
    if key.get("source") == "env":
        name = key.get("env")
        if (
            not isinstance(name, str)
            or not KEY_ENV.fullmatch(name)
            or name.startswith(("LITELLM_", "AEP_"))
        ):
            raise ModelError(422, BAD_KEY_ENV)
        return {"source": "env", "env": name}
    raise ModelError(422, "「key」的格式、长度或范围不正确")


def _key_param(key: dict[str, Any]) -> str:
    return key["value"] if key["source"] == "page" else f"os.environ/{key['env']}"


def _key_note(key: dict[str, Any] | None) -> dict[str, Any]:
    if key is None:
        return {"source": "none", "env": None}
    return {"source": key["source"], "env": key.get("env")}


# The service behind /models


ChoiceListener = Callable[[str], None]


@dataclass
class ModelService:
    admin: GatewayAdmin
    calls: CallStore
    store: SettingStore
    settings: Settings
    client: AsyncOpenAI
    # Per alias, the apps whose current version uses it (aep.runtime.catalog.model_usage).
    usage_of: Callable[[], Awaitable[dict[str, list[dict[str, Any]]]]] | None = None
    # Told the new default chat model when it changes (the run service's default).
    on_default_chat: ChoiceListener | None = None
    _defaults: dict[str, str] = field(default_factory=dict)
    _cached: tuple[float, list[dict[str, Any]]] | None = None
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def setup(self) -> None:
        """Load the defaults set on the page and drop call records older than KEEP_DAYS."""
        await self.calls.setup()
        await self.store.setup()
        saved = await self.store.get("default_models") or {}
        self._defaults = {
            "chat": saved.get("chat") or self.settings.default_model,
            "embedding": saved.get("embedding") or self.settings.default_embedding_model,
        }
        if self.on_default_chat:
            self.on_default_chat(self._defaults["chat"])
        try:
            await self.calls.prune(datetime.now(UTC) - timedelta(days=KEEP_DAYS))
        except Exception:  # noqa: BLE001
            log.warning("could not prune old model call records", exc_info=True)

    def defaults(self) -> dict[str, str]:
        return dict(self._defaults)

    # Reading

    async def _deployments(self, fresh: bool = False) -> list[dict[str, Any]]:
        if not fresh and self._cached and time.monotonic() - self._cached[0] < CACHE_SECONDS:
            return self._cached[1]
        found = await self.admin.deployments()
        seen: set[str] = set()
        unique = []
        for dep in found:
            alias = dep.get("model_name")
            if isinstance(alias, str) and alias not in seen:
                seen.add(alias)
                unique.append(dep)
        self._cached = (time.monotonic(), unique)
        return unique

    def _forget(self) -> None:
        self._cached = None

    async def catalog(self) -> list[dict[str, Any]]:
        """Every gateway model without usage, for dropdowns; [] when the gateway is down."""
        try:
            deployments = await self._deployments()
        except (GatewayDown, GatewayFailed):
            return []
        return [self._model(dep, None) for dep in deployments]

    async def listing(self) -> dict[str, Any]:
        state = await self.admin.state()
        models: list[dict[str, Any]] = []
        if state != "down":
            try:
                deployments = await self._deployments(fresh=True)
            except GatewayDown:
                state, deployments = "down", []
            except GatewayFailed:
                deployments = []
            usage = await self._usage()
            models = [self._model(dep, usage) for dep in deployments]
        order = {p: i for i, p in enumerate(PURPOSES)}
        models.sort(key=lambda m: (order[m["purpose"]], not m["is_default"], m["alias"]))
        return {"gateway": state, "models": models, "defaults": self.defaults()}

    async def get(self, alias: str) -> dict[str, Any]:
        dep = await self._find(alias)
        return self._model(dep, await self._usage())

    async def _find(self, alias: str, fresh: bool = True) -> dict[str, Any]:
        try:
            deployments = await self._deployments(fresh=fresh)
        except GatewayDown as exc:
            raise ModelError(503, DOWN) from exc
        except GatewayFailed as exc:
            raise ModelError(502, problem("gateway", reason=str(exc))["message"]) from exc
        for dep in deployments:
            if dep.get("model_name") == alias:
                return dep
        raise ModelError(404, NOT_FOUND)

    async def _usage(self) -> dict[str, UsageSummary]:
        try:
            return await self.calls.summary(datetime.now(UTC) - timedelta(days=USAGE_DAYS))
        except Exception:  # noqa: BLE001 - the list is still useful without usage
            log.warning("could not read model call records", exc_info=True)
            return {}

    def _model(self, dep: dict[str, Any], usage: dict[str, UsageSummary] | None) -> dict[str, Any]:
        alias = dep["model_name"]
        info = dep.get("model_info") or {}
        params = dep.get("litellm_params") or {}
        source = "page" if info.get("db_model") else "config"
        full = str(params.get("model") or "")
        prefix, _, rest = full.partition("/")
        provider = info.get("aep_provider")
        if provider not in (*PROVIDERS, "mock", "other"):
            if "mock_response" in params:
                provider = "mock"
            elif prefix == "openai" and params.get("api_base"):
                provider = "openai_compatible"
            else:
                provider = PREFIXES.get(prefix, "other")
        purpose = info.get("aep_purpose")
        if purpose not in PURPOSES:
            mode = info.get("mode")
            purpose = mode if mode in ("embedding", "rerank") else "chat"
        key = info.get("aep_key")
        if not (isinstance(key, dict) and key.get("source") in ("page", "env", "none")):
            key = {"source": "unknown" if source == "config" else "none", "env": None}
        price = None
        in_cost, out_cost = info.get("input_cost_per_token"), info.get("output_cost_per_token")
        if isinstance(in_cost, int | float) and isinstance(out_cost, int | float):
            price = {"input": round(in_cost * 1e6, 6), "output": round(out_cost * 1e6, 6)}
        window = info.get("max_input_tokens")
        policy = platform_default(self.settings)
        model: dict[str, Any] = {
            "alias": alias,
            "display_name": str(info.get("aep_display_name") or alias),
            "purpose": purpose,
            "provider": provider,
            "model": rest if prefix and rest else (full or None),
            "api_base": params.get("api_base") or None,
            "key": {"source": key["source"], "env": key.get("env")},
            "description": str(info.get("aep_description") or ""),
            "source": source,
            "is_default": self._defaults.get(purpose) == alias,
            "allowed": policy.allows(alias),
            "context_window": window if isinstance(window, int) else None,
            "price": price,
        }
        if usage is not None:
            found = usage.get(alias, UsageSummary())
            summary = asdict(found)
            if found.last_call_at is not None:
                summary["last_call_at"] = found.last_call_at.isoformat()
            if found.last_error is not None:
                summary["last_error"] = problem(found.last_error)["message"]
            model["usage"] = summary
        return model

    async def choices(self) -> list[dict[str, Any]]:
        """Dropdown entries for app pages (``options.model_choices``)."""
        return [
            {
                "alias": m["alias"],
                "label": m["display_name"],
                "purpose": m["purpose"],
                "allowed": m["allowed"],
            }
            for m in await self.catalog()
        ]

    async def embedding_aliases(self) -> list[str]:
        """Embedding models for knowledge bases: the configured ones and those on the page."""
        page = [
            m["alias"]
            for m in await self.catalog()
            if m["purpose"] == "embedding" and m["source"] == "page"
        ]
        return list(dict.fromkeys([*self.settings.embedding_models, *page]))

    # Changing

    async def _writable(self) -> None:
        state = await self.admin.state()
        if state == "down":
            raise ModelError(503, DOWN)
        if state == "no_db":
            raise ModelError(503, NO_DB)

    async def create(self, body: dict[str, Any]) -> dict[str, Any]:
        alias = body["alias"]
        if not ALIAS.fullmatch(alias):
            raise ModelError(422, "「别名」的格式、长度或范围不正确")
        provider = PROVIDERS.get(body["provider"])
        if provider is None:
            raise ModelError(422, "「供应商」的格式、长度或范围不正确")
        if body["purpose"] not in ("chat", "embedding"):
            raise ModelError(422, "页面上只能新增对话模型和向量模型，重排模型请在配置文件里加")
        async with self._lock:
            await self._writable()
            try:
                await self._find(alias)
            except ModelError as exc:
                if exc.status != 404:
                    raise
            else:
                raise ModelError(409, f"已经有叫「{alias}」的模型了")
            params = self._params(provider, body, body.get("key"), new=True)
            info = {
                "id": ID_PREFIX + alias,
                "aep_display_name": body["display_name"],
                "aep_purpose": body["purpose"],
                "aep_provider": body["provider"],
                "aep_description": body.get("description") or "",
                "aep_key": _key_note(check_key(body.get("key"), provider)),
            }
            if body["purpose"] == "embedding":
                info["mode"] = "embedding"
            await self._gateway(self.admin.add(alias, params, info))
            self._forget()
            result = await self._test(alias, body["purpose"])
            if not result["ok"]:
                await self._gateway(self.admin.delete(ID_PREFIX + alias), quiet=True)
                self._forget()
                raise ModelError(422, f"测试没有通过：{result['problem']['message']}")
        return await self.get(alias)

    def _params(
        self, provider: Provider, body: dict[str, Any], key: Any, *, new: bool
    ) -> dict[str, Any]:
        """The LiteLLM params a create (all of them) or an update (those given) sets."""
        params: dict[str, Any] = {}
        for name in ("model", *provider.needs):
            if new and not str(body.get(name) or "").strip():
                raise ModelError(422, f"{provider.label}要填{FIELD_LABELS[name]}")
        extra = set(body) & {"api_base", "api_version", "aws_region"}
        for name in extra - set(provider.needs) - set(provider.allows):
            if body.get(name):
                raise ModelError(422, f"{provider.label}不用填{FIELD_LABELS[name]}")
        if body.get("model") is not None:
            model = str(body["model"]).strip()
            if not re.fullmatch(r"[\x21-\x7e]{1,200}", model):
                raise ModelError(422, "「型号」的格式、长度或范围不正确")
            params["model"] = provider.prefix + model
        if body.get("api_base"):
            params["api_base"] = check_api_base(body["api_base"], self.settings)
        if body.get("api_version"):
            params["api_version"] = str(body["api_version"]).strip()[:40]
        if body.get("aws_region"):
            params["aws_region_name"] = str(body["aws_region"]).strip()[:40]
        checked = check_key(key, provider)
        if checked is not None:
            params["api_key"] = _key_param(checked)
        elif new and provider.key:
            raise ModelError(422, f"{provider.label}要填key")
        return params

    async def update(self, alias: str, body: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            await self._writable()
            dep = await self._find(alias)
            current = self._model(dep, None)
            if current["source"] != "page":
                raise ModelError(409, f"「{alias}」写在网关的配置文件里，只能在配置文件里修改")
            for fixed, label in (("alias", "别名"), ("purpose", "用途"), ("provider", "供应商")):
                if fixed in body:
                    raise ModelError(422, f"{label}登记后不能修改")
            provider = PROVIDERS[current["provider"]]
            params = self._params(provider, body, body.get("key"), new=False)
            info: dict[str, Any] = {}
            if body.get("display_name") is not None:
                info["aep_display_name"] = body["display_name"]
            if body.get("description") is not None:
                info["aep_description"] = body["description"]
            if body.get("key") is not None:
                info["aep_key"] = _key_note(check_key(body["key"], provider))
            model_id = str(dep["model_info"]["id"])
            if not params:
                if info:
                    await self._gateway(self.admin.update(model_id, None, info))
                    self._forget()
                return await self.get(alias)
            if "api_key" in params:
                # A new key cannot be undone (the old one cannot be read back), so the new
                # settings are tried on a temporary model first.
                await self._try(current, dep, params)
                await self._gateway(self.admin.update(model_id, params, info or None))
                self._forget()
            else:
                old = dep.get("litellm_params") or {}
                undo = {k: old.get(k) for k in params if k in old}
                await self._gateway(self.admin.update(model_id, params, None))
                self._forget()
                result = await self._test(alias, current["purpose"])
                if not result["ok"]:
                    if undo:
                        await self._gateway(self.admin.update(model_id, undo, None), quiet=True)
                    self._forget()
                    raise ModelError(422, f"测试没有通过：{result['problem']['message']}")
                if info:
                    await self._gateway(self.admin.update(model_id, None, info))
                    self._forget()
        return await self.get(alias)

    async def _try(self, current: dict[str, Any], dep: dict[str, Any], params: dict[str, Any]):
        old = {
            k: v
            for k, v in (dep.get("litellm_params") or {}).items()
            if k in ("model", "api_base", "api_version", "aws_region_name")
        }
        trial = f"{ID_PREFIX}trial-{int(time.time() * 1000)}"
        info = {"id": trial, "aep_purpose": current["purpose"]}
        if current["purpose"] == "embedding":
            info["mode"] = "embedding"
        await self._gateway(self.admin.add(trial, {**old, **params}, info))
        try:
            result = await self._test(trial, current["purpose"])
        finally:
            await self._gateway(self.admin.delete(trial), quiet=True)
        if not result["ok"]:
            raise ModelError(422, f"测试没有通过：{result['problem']['message']}")

    async def delete(self, alias: str) -> None:
        async with self._lock:
            await self._writable()
            dep = await self._find(alias)
            current = self._model(dep, None)
            if current["source"] != "page":
                raise ModelError(409, f"「{alias}」写在网关的配置文件里，只能在配置文件里修改")
            if current["is_default"]:
                label = PURPOSE_LABELS[current["purpose"]]
                raise ModelError(409, f"「{alias}」是平台默认的{label}模型，请先换一个默认模型")
            users = [use for use in (await self.usage(alias)) if use["via"] == "literal"]
            if users:
                titles = "、".join(use["title"] for use in users)
                raise ModelError(409, f"这些应用正在用「{alias}」：{titles}。请先在应用里换掉它")
            await self._gateway(self.admin.delete(str(dep["model_info"]["id"])))
            self._forget()

    async def usage(self, alias: str) -> list[dict[str, Any]]:
        if self.usage_of is None:
            return []
        return (await self.usage_of()).get(alias, [])

    async def set_defaults(self, chat: str | None, embedding: str | None) -> dict[str, str]:
        async with self._lock:
            catalog = {m["alias"]: m for m in await self.catalog()}
            embedding_ok = set(self.settings.embedding_models)
            chosen = dict(self._defaults)
            for purpose, alias in (("chat", chat), ("embedding", embedding)):
                if alias is None:
                    continue
                found = catalog.get(alias)
                if found is None and not (purpose == "embedding" and alias in embedding_ok):
                    raise ModelError(422, f"没有找到模型「{alias}」")
                if found is not None and found["purpose"] != purpose:
                    raise ModelError(422, f"「{alias}」不是{PURPOSE_LABELS[purpose]}模型")
                chosen[purpose] = alias
            await self.store.set("default_models", chosen)
            self._defaults = chosen
            if self.on_default_chat:
                self.on_default_chat(chosen["chat"])
            return self.defaults()

    # Testing

    async def test(self, alias: str) -> dict[str, Any]:
        dep = await self._find(alias)
        return await self._test(alias, self._model(dep, None)["purpose"])

    async def _test(self, alias: str, purpose: str) -> dict[str, Any]:
        if purpose == "rerank":
            unsupported = problem("unsupported")
            return {"ok": False, "latency_ms": None, "reply": None, "problem": unsupported}
        client = self.client.with_options(timeout=TEST_SECONDS, max_retries=0)
        started = time.monotonic()
        reply = None
        try:
            if purpose == "embedding":
                await llm.embed(client, ["测试"], alias)
            else:
                result = await llm.chat(
                    client, [{"role": "user", "content": "请只回复：OK"}], alias, max_tokens=16
                )
                reply = result.text[:REPLY_CHARS]
        except Exception as exc:  # noqa: BLE001 - every failure is a problem to show
            return {
                "ok": False,
                "latency_ms": None,
                "reply": None,
                "problem": _problem_of(exc, purpose),
            }
        return {
            "ok": True,
            "latency_ms": int((time.monotonic() - started) * 1000),
            "reply": reply,
            "problem": None,
        }

    async def _gateway(self, call: Awaitable[None], quiet: bool = False) -> None:
        try:
            await call
        except GatewayDown as exc:
            if quiet:
                log.warning("model gateway unreachable while cleaning up: %s", exc)
                return
            raise ModelError(503, DOWN) from exc
        except GatewayFailed as exc:
            if quiet:
                log.warning("model gateway refused a clean-up: %s", exc)
                return
            raise ModelError(502, problem("gateway", reason=str(exc))["message"]) from exc


def _problem_of(exc: BaseException, purpose: str) -> dict[str, str]:
    code = llm.error_code(exc)
    reason = str(getattr(exc, "message", None) or exc)
    log.info("model test failed (%s): %s", code, reason[:500])
    if purpose == "embedding":
        found = re.search(r"收到 (\d+)", str(exc))
        if found:
            return problem("dimensions", n=found.group(1))
        if code == "bad_request" and "dimension" in reason.lower():
            return problem("dimensions")
    return problem(code, reason=_short(reason))


def _short(reason: str) -> str:
    """A provider's reason without LiteLLM's wrapping, at most REASON_CHARS characters."""
    text = reason.split("\n\nLiteLLM:")[0]
    text = re.sub(r"^litellm\.\w+:\s*", "", text)
    try:
        start = text.index("{")
        inner = json.loads(text[start:])
        message = inner.get("error", {}).get("message") if isinstance(inner, dict) else None
        if message:
            text = str(message)
    except (ValueError, AttributeError):
        pass
    return text.strip().rstrip("。.")[:REASON_CHARS]
