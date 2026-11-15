"""A runtime (``AEP_MODE=runtime``): installs packages the platform signed, binds the data
sources they need to this machine's connectors, and checks it has everything they need
(docs/api/runtime.md, 运行包这边的接口).

Installed apps are ordinary flow and agent apps with their versions and skills under the
platform's numbers, so runs, /v1 and /mcp/apps work as on the platform. What this module
adds is the record of which package each version came from and the self-check.
"""

import asyncio
import contextlib
import logging
import os
import shutil
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from openai import AsyncOpenAI
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, ValidationError

from aep import __version__, llm, packages
from aep.config import Settings
from aep.connector_check import ConnectorCheck, connection_problem, failed_detail
from aep.datasources import DataSource, DataSourceStore, DuplicateName, canon_id
from aep.media.store import DuplicateId
from aep.media.store import DuplicateName as LibraryNameTaken
from aep.runtime import agent_app
from aep.runtime.appconfig import ConfigError
from aep.runtime.catalog import AppCatalog
from aep.runtime.flow import files as flowfiles
from aep.runtime.service import RunService
from aep.runtime.store import DEFAULT_TENANT
from aep.skills import SkillContent
from aep.tools.mcp import ConnectorUnavailable, McpTools, ToolError

log = logging.getLogger(__name__)

NOT_PACKAGE = "不是流程包文件"
NO_KEYS = "运行包没有配置平台公钥（AEP_PLATFORM_PUBLIC_KEYS）"
TOO_BIG = "流程包不能超过 5 MB"
UNFINISHED = "这个应用在运行包上没有装完，请重新装一次流程包"
CHECK_EVERY_SECONDS = 24 * 3600
NETWORK_SECONDS = 5.0
MAX_LISTED = 5


class InstallError(Exception):
    """Refused, with the HTTP status and the reason in Chinese."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class NotInstalled(LookupError):
    pass


# The package as a runtime reads it. Its signature covers the raw object, so these only
# check the shape; unknown keys are left alone.


class _Loose(BaseModel):
    model_config = ConfigDict(extra="allow")


class _Issuer(_Loose):
    platform_version: str
    key_id: str


class _Skill(_Loose):
    name: str
    version: StrictInt
    description: str
    body: str
    references: dict[str, str]


class _Platform(_Loose):
    min_version: str


class _Source(_Loose):
    datasource_id: str
    name: str | None
    kind: str | None
    tools: list[str]


class _Library(_Loose):
    library_id: str
    name: str | None
    access: str


class _Requirements(_Loose):
    platform: _Platform
    models: list[dict[str, str]]
    default_model: StrictBool
    datasources: list[_Source]
    sandbox: StrictBool
    skills: list[dict[str, Any]]
    secrets: list[str]
    machine: dict[str, StrictInt] | None
    network: list[str]
    notes: str
    # Packages made before media libraries could be bound have none.
    media_libraries: list[_Library] = []


class _Package(_Loose):
    format: Literal["aep-package/1"]
    package_id: str
    issued_at: str
    issuer: _Issuer
    app: str
    version: StrictInt
    kind: Literal["flow", "agent"]
    title: str
    summary: str
    app_file: str
    skills: list[_Skill]
    requirements: _Requirements
    signature: str


def _version_tuple(text: str) -> tuple[int, ...] | None:
    try:
        return tuple(int(part) for part in text.split("."))
    except ValueError:
        return None


def new_enough(min_version: str, have: str = __version__) -> bool:
    wanted, mine = _version_tuple(min_version), _version_tuple(have)
    return wanted is not None and mine is not None and mine >= wanted


# Which package each installed version came from


@dataclass(frozen=True)
class Installed:
    app: str
    version: int
    package_id: str
    issued_at: str
    requirements: dict[str, Any]
    installed_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def brief(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "package_id": self.package_id,
            "issued_at": self.issued_at,
            "installed_at": self.installed_at.isoformat(),
        }


class InstallStore(Protocol):
    async def setup(self) -> None: ...
    async def add(self, installed: Installed) -> None: ...
    async def versions(self, app: str) -> list[Installed]: ...
    async def find(self, package_id: str) -> Installed | None: ...
    async def apps(self) -> list[str]: ...


class MemoryInstallStore:
    def __init__(self) -> None:
        self._items: list[Installed] = []

    async def setup(self) -> None:
        return None

    async def add(self, installed: Installed) -> None:
        self._items.append(installed)

    async def versions(self, app: str) -> list[Installed]:
        return sorted(
            (i for i in self._items if i.app == app), key=lambda i: i.version, reverse=True
        )

    async def find(self, package_id: str) -> Installed | None:
        return next((i for i in self._items if i.package_id == package_id), None)

    async def apps(self) -> list[str]:
        return sorted({i.app for i in self._items})


SCHEMA = [
    """
CREATE TABLE IF NOT EXISTS runtime_installs (
    tenant_id    text NOT NULL DEFAULT 'default',
    app          text NOT NULL,
    version      integer NOT NULL,
    package_id   text NOT NULL UNIQUE,
    issued_at    text NOT NULL,
    requirements jsonb NOT NULL,
    installed_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, app, version)
)""",
]
COLUMNS = "app, version, package_id, issued_at, requirements, installed_at"


class PostgresInstallStore:
    """Installed package versions in Postgres. The pool must use dict rows and autocommit."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            for statement in SCHEMA:
                await conn.execute(statement)

    async def add(self, installed: Installed) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO runtime_installs"
                " (tenant_id, app, version, package_id, issued_at, requirements, installed_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    DEFAULT_TENANT,
                    installed.app,
                    installed.version,
                    installed.package_id,
                    installed.issued_at,
                    Jsonb(installed.requirements),
                    installed.installed_at,
                ),
            )

    async def versions(self, app: str) -> list[Installed]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {COLUMNS} FROM runtime_installs WHERE tenant_id = %s AND app = %s"
                " ORDER BY version DESC",
                (DEFAULT_TENANT, app),
            )
            return [Installed(**row) for row in await cur.fetchall()]

    async def find(self, package_id: str) -> Installed | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {COLUMNS} FROM runtime_installs WHERE tenant_id = %s AND package_id = %s",
                (DEFAULT_TENANT, package_id),
            )
            row = await cur.fetchone()
            return Installed(**row) if row else None

    async def apps(self) -> list[str]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT DISTINCT app FROM runtime_installs WHERE tenant_id = %s ORDER BY app",
                (DEFAULT_TENANT,),
            )
            return [row["app"] for row in await cur.fetchall()]


# This machine, for the optional checks


def machine_facts(media_dir: Path) -> dict[str, int]:
    """CPU cores, memory and the size of the disk the media library lives on, in GB."""
    pages = os.sysconf("SC_PHYS_PAGES") if hasattr(os, "sysconf") else 0
    size = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 0
    where = media_dir
    while not where.exists() and where != where.parent:
        where = where.parent
    return {
        "cpu_cores": os.cpu_count() or 0,
        "memory_gb": round(pages * size / 1024**3),
        "disk_gb": round(shutil.disk_usage(where).total / 1024**3),
    }


async def reaches(host: str) -> bool:
    """Whether port 443 of ``host`` answers within 5 seconds."""
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, 443), NETWORK_SECONDS)
    except (OSError, TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return True


# A media library's name, as the media API allows (docs/api/media.md).
MAX_LIBRARY_NAME = 50
MACHINE = (("cpu_cores", "CPU", "核"), ("memory_gb", "内存", "GB"), ("disk_gb", "硬盘", "GB"))


def _item(kind: str, name: str, ok: bool, message: str | None, required: bool = True, **more: Any):
    return {"kind": kind, "name": name, **more, "required": required, "ok": ok, "message": message}


def _is_uuid(value: str) -> bool:
    try:
        uuid.UUID(value)
    except ValueError:
        return False
    return True


def _missing(item: dict[str, Any]) -> str:
    """How a failed required item reads in the 503 of /v1."""
    kind, name = item["kind"], item["name"]
    if kind == "model":
        return f"{name} 模型别名"
    if kind == "datasource":
        return f"{name}连接器"
    if kind == "skill":
        return f"Skill {name} 第 {item['version']} 版"
    if kind == "secret":
        return f"密钥 {name}"
    if kind == "sandbox":
        return "代码沙箱"
    if kind == "media_library":
        return f"媒体库「{name}」"
    return f"平台 {name} 以上的镜像"


class Runtime:
    """Installs, bindings and the self-check of a runtime."""

    def __init__(
        self,
        runs: RunService,
        catalog: AppCatalog,
        datasources: DataSourceStore,
        tools: McpTools,
        check: ConnectorCheck,
        settings: Settings,
        client: AsyncOpenAI | None,
        store: InstallStore | None = None,
        machine: Callable[[], dict[str, int]] | None = None,
        reach: Callable[[str], Awaitable[bool]] = reaches,
    ) -> None:
        self._runs = runs
        self._catalog = catalog
        self._sources = datasources
        self._tools = tools
        self._check = check
        self._settings = settings
        self._client = client
        self.store: InstallStore = store or MemoryInstallStore()
        self._machine = machine or (lambda: machine_facts(settings.media_dir))
        self._reach = reach
        # The last self-check of each app, with the version it checked.
        self._checks: dict[str, tuple[int, dict[str, Any]]] = {}
        # Model aliases whose last real call failed: probed again on every check until one
        # goes through, so a check without fresh=true cannot hide a broken provider.
        self._failed_probes: dict[str, str] = {}
        self._lock = asyncio.Lock()

    # Installing

    async def install(self, raw: Any, actor: str | None) -> tuple[dict[str, Any], bool]:
        """Install a package (the parsed JSON); with whether anything was added."""
        if not isinstance(raw, dict):
            raise InstallError(422, NOT_PACKAGE)
        try:
            package = _Package.model_validate(raw)
        except ValidationError as exc:
            raise InstallError(422, NOT_PACKAGE) from exc
        if not self._settings.platform_public_keys:
            raise InstallError(503, NO_KEYS)
        try:
            packages.verify(raw, self._settings.platform_public_keys)
        except packages.BadSignature as exc:
            raise InstallError(422, str(exc)) from exc
        wanted = package.requirements.platform.min_version
        if not new_enough(wanted):
            raise InstallError(
                422, f"这个包要求平台 {wanted} 以上，这台运行包是 {__version__}，请先升级镜像"
            )
        async with self._lock:
            if await self.store.find(package.package_id) is not None:
                return await self.installed(package.app), False
            config = await self._config(package)
            resume = await self._half_installed(package, config)
            await self._refuse_conflicts(package, resume)
            for skill in package.skills:
                content = SkillContent(
                    skill.description,
                    skill.body,
                    dict(skill.references),
                    version=skill.version,
                    note=f"随流程包 {package.app} v{package.version} 装上",
                    created_by=actor,
                )
                await self._runs.skills.install(skill.name, content)
            for library in package.requirements.media_libraries:
                await self._add_library(package.app, library)
            await self._runs.ensure_flows()
            if await self._runs.flows.get(package.app) is None:
                await self._runs.flows.create(
                    package.app, package.title, package.summary, actor, package.kind
                )
            else:
                await self._runs.flows.update(package.app, package.title, package.summary)
            if resume:
                await self._runs.versions.activate(package.app, package.version)
            else:
                note = f"流程包 {package.package_id[:8]}（{package.issued_at[:10]}）"
                await self._runs.versions.install(package.app, package.version, config, note, actor)
            requirements = raw["requirements"]
            await self.store.add(
                Installed(
                    package.app,
                    package.version,
                    package.package_id,
                    package.issued_at,
                    requirements,
                )
            )
        await self.check(package.app)
        return await self.installed(package.app), True

    async def _add_library(self, app: str, library: _Library) -> None:
        """Make a media library the package binds, with the same id, when this machine has
        none (docs/api/media.md, 运行要求清单); its name gets the app's when taken."""
        store = self._runs.media
        if not _is_uuid(library.library_id):
            raise InstallError(422, NOT_PACKAGE)
        if await store.library(library.library_id) is not None:
            return
        name = (library.name or f"媒体库 {library.library_id[:8]}")[:MAX_LIBRARY_NAME]
        for suffix in (
            "",
            f"（{app}）",
            f"（{app} {library.library_id[:8]}）",
            f"（{library.library_id}）",
        ):
            # The base gives way so the part that tells the libraries apart is kept.
            candidate = name[: max(1, MAX_LIBRARY_NAME - len(suffix))] + suffix
            try:
                await store.create_library(
                    candidate[:MAX_LIBRARY_NAME], library_id=library.library_id
                )
                return
            except LibraryNameTaken:
                continue
            except DuplicateId:
                return  # made meanwhile
        raise InstallError(409, f"这台机器上已经有名为「{name}」的媒体库")

    async def _config(self, package: _Package) -> dict[str, Any]:
        """The configuration in the package's app file, checked as an imported file is."""
        try:
            data = flowfiles.load(package.app_file)
        except flowfiles.BadFile as exc:
            raise InstallError(422, NOT_PACKAGE) from exc
        if data["kind"] != package.kind:
            raise InstallError(422, NOT_PACKAGE)
        try:
            if package.kind == agent_app.KIND:
                return await self._catalog.check_agent(data["config"], strict=False)
            return await self._catalog.check_flow(data["config"], strict=False)
        except ConfigError as exc:
            first = exc.problems[0][1] if exc.problems else ""
            raise InstallError(422, f"这台运行包读不了包里的应用配置：{first}") from exc

    async def _half_installed(self, package: _Package, config: dict[str, Any]) -> bool:
        """Whether an earlier install of this package stopped after adding the app version
        but before recording the install: the same version number with the same
        configuration and no install record for it. Installing again finishes it."""
        have = await self._runs.versions.version(package.app, package.version)
        if have is None or have.config != config:
            return False
        recorded = await self.store.versions(package.app)
        return all(i.version != package.version for i in recorded)

    async def _refuse_conflicts(self, package: _Package, resume: bool = False) -> None:
        for skill in package.skills:
            have = await self._runs.skills.version(skill.name, skill.version)
            if have is not None and (have.description, have.body, have.references) != (
                skill.description,
                skill.body,
                skill.references,
            ):
                raise InstallError(
                    409, f"Skill「{skill.name}」第 {skill.version} 版和这台机器上已有的不一样"
                )
        await self._runs.ensure_flows()
        found = await self._runs.flows.get(package.app)
        if found is not None and found.kind != package.kind:
            other = "智能体应用" if found.kind == agent_app.KIND else "编排应用"
            raise InstallError(409, f"这台机器上已经有一个同名的{other}")
        if not resume and await self._runs.versions.version(package.app, package.version):
            raise InstallError(409, f"第 {package.version} 版已经装过了，平台重新打的包要用新版本")

    # Reading

    async def _versions(self, app: str) -> list[Installed]:
        found = await self.store.versions(app)
        if not found:
            raise NotInstalled(app)
        return found

    async def _current(self, app: str) -> Installed:
        found = await self._versions(app)
        number = await self._runs.versions.current(app)
        return next((i for i in found if i.version == number), found[0])

    async def installed(self, app: str) -> dict[str, Any]:
        found = await self._versions(app)
        current = await self._current(app)
        flow = await self._runs.flows.get(app)
        assert flow is not None
        return {
            "app": app,
            "title": flow.title,
            "kind": flow.kind,
            "version": current.version,
            "versions": [i.brief() for i in found],
            "requirements": current.requirements,
            "check": await self.latest(app),
        }

    async def all(self) -> list[dict[str, Any]]:
        return [await self.installed(app) for app in await self.store.apps()]

    async def activate(self, app: str, version: int) -> dict[str, Any]:
        found = await self._versions(app)
        if all(i.version != version for i in found):
            raise InstallError(404, f"没有装过第 {version} 版")
        await self._runs.versions.activate(app, version)
        await self.check(app)
        return await self.installed(app)

    # Binding

    async def bind(self, app: str, datasource_id: str, url: str) -> tuple[DataSource, bool]:
        """Register (or move) this machine's connector for one of the app's data sources."""
        current = await self._current(app)
        key = canon_id(datasource_id)
        wanted = next(
            (d for d in current.requirements.get("datasources") or [] if d["datasource_id"] == key),
            None,
        )
        if wanted is None:
            raise InstallError(404, "这个应用不需要这个数据源")
        name = wanted.get("name") or key
        kind = wanted.get("kind") or "mcp"
        async with self._lock:
            existing = await self._sources.get(key)
            if existing is None and any(d.name == name for d in await self._sources.all()):
                raise InstallError(409, f"这台机器上已经有名为「{name}」的数据源")
            result = await self._check.test(self._tools, kind, url, self._settings)
            if not result.ok or result.url is None:
                raise InstallError(422, failed_detail(result))
            names = {t.name for t in result.tools}
            missing = [t for t in wanted.get("tools") or [] if t not in names]
            if missing:
                raise InstallError(422, "连接器缺少工具：" + "、".join(missing))
            if existing is None:
                try:
                    source = await self._sources.create(
                        name,
                        kind,
                        result.url,
                        datasource_id=key,  # type: ignore[arg-type]
                    )
                except DuplicateName as exc:
                    raise InstallError(409, f"这台机器上已经有名为「{name}」的数据源") from exc
            else:
                moved = await self._sources.update(key, connector_url=result.url)
                assert moved is not None
                source = moved
            self._check.remember(source, result)
        for other in await self.store.apps():
            uses = (await self._current(other)).requirements.get("datasources") or []
            if any(d["datasource_id"] == key for d in uses):
                await self.check(other)
        return source, existing is None

    # The self-check

    async def check(self, app: str, fresh: bool = False) -> dict[str, Any]:
        """Check every item of the current version's requirements and keep the result."""
        current = await self._current(app)
        needs = current.requirements
        items: list[dict[str, Any]] = []
        wanted = needs["platform"]["min_version"]
        items.append(
            _item(
                "platform",
                wanted,
                new_enough(wanted),
                None if new_enough(wanted) else f"这台运行包是 {__version__}，请先升级镜像",
            )
        )
        for ref in needs.get("skills") or []:
            have = await self._runs.skills.version(ref["name"], ref["version"])
            items.append(
                _item(
                    "skill",
                    ref["name"],
                    have is not None,
                    None if have else "没有装上这个 Skill 版本",
                    version=ref["version"],
                )
            )
        items += await self._model_items(needs, fresh)
        for source in needs.get("datasources") or []:
            items.append(await self._source_item(source))
        if needs.get("sandbox"):
            items.append(await self._sandbox_item())
        for name in needs.get("secrets") or []:
            have = await self._runs.secrets.get(app, name)
            items.append(
                _item("secret", name, have is not None, None if have else "还没有填这个密钥")
            )
        for wanted in needs.get("media_libraries") or []:
            library = await self._runs.media.library(str(wanted.get("library_id")))
            items.append(
                _item(
                    "media_library",
                    library.name if library else str(wanted.get("name") or wanted["library_id"]),
                    library is not None,
                    None if library else "没有这个媒体库，请重新装一次流程包",
                    library_id=wanted.get("library_id"),
                )
            )
        items += self._machine_items(needs.get("machine"))
        for host in needs.get("network") or []:
            ok = await self._reach(host)
            message = None if ok else f"{NETWORK_SECONDS:g} 秒内连不上 443 端口"
            items.append(_item("network", host, ok, message, required=False))
        result = {
            "ok": all(i["ok"] for i in items if i["required"]),
            "checked_at": datetime.now(UTC).isoformat(),
            "items": items,
        }
        self._checks[app] = (current.version, result)
        return result

    async def _model_items(self, needs: dict[str, Any], fresh: bool) -> list[dict[str, Any]]:
        aliases = await self._catalog.models()
        wanted = [m["alias"] for m in needs.get("models") or []]
        default = self._runs.default_model
        if needs.get("default_model") and default not in wanted:
            wanted.append(default)
        items = []
        for alias in wanted:
            if aliases is None:
                items.append(_item("model", alias, False, "连不上模型网关"))
                continue
            if alias not in aliases:
                message = "模型网关上没有这个别名，请在 LiteLLM 配置里加上"
                if alias == default and alias not in [m["alias"] for m in needs["models"]]:
                    message = "默认对话模型不在模型网关上，请在 LiteLLM 配置里加上"
                items.append(_item("model", alias, False, message))
                continue
            problem = None
            if fresh or alias in self._failed_probes:
                problem = await self._probe(alias)
                if problem is None:
                    self._failed_probes.pop(alias, None)
                else:
                    self._failed_probes[alias] = problem
            items.append(_item("model", alias, problem is None, problem))
        return items

    async def _probe(self, alias: str) -> str | None:
        if self._client is None:
            return None
        try:
            await llm.chat(
                self._client.with_options(timeout=30, max_retries=0),
                [{"role": "user", "content": "OK"}],
                alias,
                max_tokens=1,
            )
        except Exception as exc:  # noqa: BLE001 - any failure is what the check reports
            return f"别名在，但调用失败：{str(exc)[:200]}"
        return None

    async def _source_item(self, wanted: dict[str, Any]) -> dict[str, Any]:
        key, name = wanted["datasource_id"], wanted.get("name") or wanted["datasource_id"]
        source = await self._sources.get(key)
        if source is None:
            return _item("datasource", name, False, "还没有绑定连接器", datasource_id=key)
        try:
            listed = await self._check.tools(self._tools, source, self._settings)
        except ConnectorUnavailable as exc:
            problem = connection_problem(exc.reason, self._settings.connector_check_seconds)
            return _item("datasource", name, False, problem.message, datasource_id=key)
        except ToolError:
            problem = connection_problem("not_mcp", self._settings.connector_check_seconds)
            return _item("datasource", name, False, problem.message, datasource_id=key)
        names = {t.name for t in listed}
        missing = [t for t in wanted.get("tools") or [] if t not in names]
        message = "连接器缺少工具：" + "、".join(missing) if missing else None
        return _item("datasource", name, not missing, message, datasource_id=key)

    async def _sandbox_item(self) -> dict[str, Any]:
        url = self._settings.sandbox_url
        try:
            listed = await self._tools.list_tools(
                url, timeout_seconds=self._settings.connector_check_seconds
            )
        except (ConnectorUnavailable, ToolError):
            return _item("sandbox", "代码沙箱", False, "连不上代码沙箱（AEP_SANDBOX_URL）")
        if all(t.name != "run_python" for t in listed):
            return _item("sandbox", "代码沙箱", False, "代码沙箱没有 run_python 工具")
        return _item("sandbox", "代码沙箱", True, None)

    def _machine_items(self, machine: dict[str, int] | None) -> list[dict[str, Any]]:
        if not machine:
            return []
        facts = self._machine()
        items = []
        for key, label, unit in MACHINE:
            if key not in machine:
                continue
            have, wanted = facts.get(key, 0), machine[key]
            message = None if have >= wanted else f"要求 {wanted} {unit}，这台机器 {have} {unit}"
            items.append(_item("machine", label, have >= wanted, message, required=False))
        return items

    async def latest(self, app: str) -> dict[str, Any]:
        """The last check of ``app``'s current version, checking it now when it has none."""
        current = await self._current(app)
        cached = self._checks.get(app)
        if cached is not None and cached[0] == current.version:
            return cached[1]
        return await self.check(app)

    async def recheck(self, app: str) -> None:
        """Check ``app`` again if it is installed (after one of its secrets changed)."""
        if await self.store.versions(app):
            await self.check(app)

    async def check_all(self) -> None:
        for app in await self.store.apps():
            try:
                await self.check(app)
            except Exception:  # noqa: BLE001 - one app must not stop the others' checks
                log.exception("could not check installed app %s", app)

    async def ready(self, app: str) -> str | None:
        """None when /v1 may start a run of ``app``; else why not (docs/api/runtime.md). The
        version that runs must be installed and its own last check must have passed."""
        try:
            number = await self._runs.versions.current(app)
        except LookupError:  # no version at all: the memory store's KeyError, VersionNotFound
            return UNFINISHED
        if all(i.version != number for i in await self.store.versions(app)):
            return UNFINISHED
        cached = self._checks.get(app)
        if cached is not None and cached[0] == number and cached[1]["ok"]:
            return None
        result = await self.check(app)
        if result["ok"]:
            return None
        missing = [_missing(i) for i in result["items"] if i["required"] and not i["ok"]]
        return f"这个应用还不能运行：缺 {'、'.join(missing[:MAX_LISTED])}（运行包自检没通过）"

    async def keep_checking(self) -> None:
        """Check every installed app once a day."""
        while True:
            await asyncio.sleep(CHECK_EVERY_SECONDS)
            await self.check_all()
