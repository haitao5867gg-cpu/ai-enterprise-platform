"""Packages: one published app version, signed by the platform, to run on another machine
(docs/api/runtime.md).

The platform derives most of a package's requirements list from the version's
configuration; the publisher fills in the rest on the app (machine, network, notes),
kept here. Packages are signed with Ed25519 over a canonical JSON form; a runtime
installs only packages that verify with a public key it was given.
"""

import base64
import binascii
import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from aep import __version__
from aep.datasources import DataSourceStore
from aep.runtime import agent_app
from aep.runtime.catalog import AppCatalog, AppNotFound
from aep.runtime.flow import files as flowfiles
from aep.runtime.flow import graph as flowgraph
from aep.runtime.service import RunService
from aep.runtime.store import DEFAULT_TENANT
from aep.runtime.versions import VersionNotFound

FORMAT = "aep-package/1"
MAX_BYTES = 5 * 1024 * 1024
MAX_HOSTS = 20
MAX_NOTES = 2000
_HOST = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)*$")
BAD_HOST = "「网络」里要写主机名，比如 api.pexels.com"
NO_SIGNING_KEY = "平台没有配置流程包签名密钥（AEP_PACKAGE_SIGNING_KEY）"

# Why a version cannot be packaged in step 1, checked in this order (docs/api/runtime.md).
CODE_APP = "代码应用暂时不能打成流程包"
APPROVAL = "有要审批者确认的步骤，运行包上没有审批的地方，暂时不能打成流程包"
CALLS_APPS = "调用其他应用的应用暂时不能打成流程包"
KNOWLEDGE = "用到知识库的应用暂时不能打成流程包"
FILE_SOURCE = "用到上传文件数据源的应用暂时不能打成流程包"
DATA_NODES = "有数据工程节点的应用暂时不能打成流程包"
NEEDS_PROJECT = "要选择项目的应用暂时不能打成流程包"
# Nodes that need the platform's Databricks workspace or projects.
DATA_NODE_TYPES = (
    "etl_profile",
    "etl_plan_check",
    "etl_code_check",
    "databricks_ingest",
    "databricks_dev_run",
    "databricks_release",
    "data_check",
    "git_pr",
)


def unsupported(
    nodes: list[dict[str, Any]], knowledge: list[str], source_kinds: list[str | None]
) -> str | None:
    """Why a flow-shaped configuration cannot be packaged yet, or None. ``nodes`` are its
    nodes, ``knowledge`` the bases it names and ``source_kinds`` the kinds of the data
    sources it uses. Code apps are refused before this is asked."""
    if any(n["type"] == "human" and n.get("mode", "approval") == "approval" for n in nodes):
        return APPROVAL
    if any(n["type"] == "app" or (n["type"] == "agent" and n.get("apps")) for n in nodes):
        return CALLS_APPS
    if knowledge:
        return KNOWLEDGE
    if "file" in source_kinds:
        return FILE_SOURCE
    if any(n["type"] in DATA_NODE_TYPES for n in nodes):
        return DATA_NODES
    # A runtime has no projects; a flow that may run without one is fine.
    if any(n["type"] == "start" and n.get("project") == "required" for n in nodes):
        return NEEDS_PROJECT
    return None


# Signing


def canonical(obj: dict[str, Any]) -> bytes:
    """The bytes a package's signature covers: the package without ``signature``."""
    body = {k: v for k, v in obj.items() if k != "signature"}
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def decode_key(text: str) -> bytes:
    """A key's 32 raw bytes from base64; ValueError when it is not that."""
    try:
        raw = base64.b64decode(text.strip(), validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("not base64") from exc
    if len(raw) != 32:
        raise ValueError("not 32 bytes")
    return raw


def key_id(public_raw: bytes) -> str:
    """The first 16 hex characters of the SHA-256 of a raw public key."""
    return hashlib.sha256(public_raw).hexdigest()[:16]


class Signer:
    """Signs packages with the platform's Ed25519 key (a 32-byte seed)."""

    def __init__(self, seed: bytes) -> None:
        self._key = Ed25519PrivateKey.from_private_bytes(seed)
        self.public_raw = self._key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
        self.key_id = key_id(self.public_raw)

    @classmethod
    def from_text(cls, text: str) -> "Signer | None":
        """The signer for ``AEP_PACKAGE_SIGNING_KEY``; None when it is not set."""
        return cls(decode_key(text)) if text.strip() else None

    @property
    def public_key(self) -> str:
        return base64.b64encode(self.public_raw).decode()

    def sign(self, unsigned: dict[str, Any]) -> dict[str, Any]:
        signature = self._key.sign(canonical(unsigned))
        return {**unsigned, "signature": base64.b64encode(signature).decode()}


class BadSignature(Exception):
    """The package was not signed by a key this machine knows, or was changed."""


def verify(package: dict[str, Any], public_keys: list[str]) -> str:
    """The key id that signed ``package``; BadSignature (with the reason in Chinese)
    when none of ``public_keys`` did, or the package was changed after signing."""
    issuer = package.get("issuer")
    wanted = issuer.get("key_id") if isinstance(issuer, dict) else None
    known = {key_id(raw): raw for raw in (decode_key(k) for k in public_keys)}
    if wanted not in known:
        raise BadSignature("这个包不是用这台运行包认识的平台密钥签的")
    try:
        signature = base64.b64decode(str(package.get("signature") or ""), validate=True)
        Ed25519PublicKey.from_public_bytes(known[wanted]).verify(signature, canonical(package))
    except (binascii.Error, ValueError, InvalidSignature) as exc:
        raise BadSignature("流程包的签名不对，可能被改过") from exc
    return wanted


# What the publisher fills in on the app


@dataclass(frozen=True)
class RuntimeNeeds:
    """The machine, network and notes a package of the app asks for. They belong to the
    app, not to a version, and are copied into each package."""

    machine: dict[str, int] | None = None
    network: list[str] = field(default_factory=list)
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"machine": self.machine, "network": list(self.network), "notes": self.notes}


def hosts(values: list[str]) -> list[str]:
    """Host names as kept: trimmed, lowercase, in order, without repeats; ValueError
    (``BAD_HOST``) for anything that is not a bare host name."""
    out: list[str] = []
    for value in values:
        host = value.strip().lower()
        if not 1 <= len(host) <= 253 or not _HOST.match(host):
            raise ValueError(BAD_HOST)
        if host not in out:
            out.append(host)
    return out


class RuntimeNeedsStore(Protocol):
    async def setup(self) -> None: ...
    async def get(self, app: str) -> RuntimeNeeds: ...
    async def set(self, app: str, needs: RuntimeNeeds) -> None: ...


class MemoryRuntimeNeedsStore:
    def __init__(self) -> None:
        self._needs: dict[str, RuntimeNeeds] = {}

    async def setup(self) -> None:
        return None

    async def get(self, app: str) -> RuntimeNeeds:
        return self._needs.get(app, RuntimeNeeds())

    async def set(self, app: str, needs: RuntimeNeeds) -> None:
        self._needs[app] = needs


SCHEMA = [
    """
CREATE TABLE IF NOT EXISTS app_runtime_needs (
    tenant_id  text NOT NULL DEFAULT 'default',
    app        text NOT NULL,
    machine    jsonb,
    network    jsonb NOT NULL DEFAULT '[]',
    notes      text NOT NULL DEFAULT '',
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, app)
)""",
]


class PostgresRuntimeNeedsStore:
    """What publishers filled in, in Postgres. The pool must use dict rows and autocommit."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            for statement in SCHEMA:
                await conn.execute(statement)

    async def get(self, app: str) -> RuntimeNeeds:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT machine, network, notes FROM app_runtime_needs"
                " WHERE tenant_id = %s AND app = %s",
                (DEFAULT_TENANT, app),
            )
            row = await cur.fetchone()
        if row is None:
            return RuntimeNeeds()
        return RuntimeNeeds(row["machine"], list(row["network"]), row["notes"])

    async def set(self, app: str, needs: RuntimeNeeds) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO app_runtime_needs (tenant_id, app, machine, network, notes)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (tenant_id, app) DO UPDATE SET machine = excluded.machine,"
                " network = excluded.network, notes = excluded.notes, updated_at = now()",
                (
                    DEFAULT_TENANT,
                    app,
                    Jsonb(needs.machine) if needs.machine is not None else None,
                    Jsonb(list(needs.network)),
                    needs.notes,
                ),
            )


# Requirements lists and packages


class Unsupported(Exception):
    """The version cannot be packaged in step 1; the message says why."""


class Packages:
    """Requirements lists and signed packages of published app versions."""

    def __init__(
        self,
        runs: RunService,
        catalog: AppCatalog,
        datasources: DataSourceStore,
        store: RuntimeNeedsStore | None = None,
    ) -> None:
        self._runs = runs
        self._catalog = catalog
        self._datasources = datasources
        self.store: RuntimeNeedsStore = store or MemoryRuntimeNeedsStore()

    async def _kind(self, app: str) -> str:
        kind = await self._runs.app_kind(app)
        if kind is None or await self._runs.kind(app) is None:
            raise AppNotFound(app)
        return kind

    async def needs(self, app: str) -> RuntimeNeeds:
        await self._kind(app)
        return await self.store.get(app)

    async def set_needs(self, app: str, needs: RuntimeNeeds) -> RuntimeNeeds:
        await self._kind(app)
        await self.store.set(app, needs)
        return needs

    async def requirements(self, app: str, version: int) -> dict[str, Any]:
        """The version's requirements list with ``unsupported`` (docs/api/runtime.md)."""
        kind = await self._kind(app)
        await self._runs.ensure_versions(app)
        found = await self._runs.versions.version(app, version)
        if found is None:
            raise VersionNotFound(f"{app} v{version}")
        derived: dict[str, Any] = {
            "models": [],
            "default_model": False,
            "datasources": [],
            "sandbox": False,
            "skills": [],
            "secrets": [],
        }
        reason: str | None = CODE_APP
        if kind != "code":
            shape = self._runs.flow_config(app, found.config)
            config = found.config if kind == agent_app.KIND else shape
            sources = {
                d.datasource_id: {"datasource_id": d.datasource_id, "name": d.name, "kind": d.kind}
                for d in await self._datasources.all()
            }
            needed = flowfiles.requires(config, sources, {}, kind)
            nodes = flowgraph.nodes(shape)
            _, default = flowfiles.model_refs(shape)
            derived = {
                "models": needed["models"],
                "default_model": default,
                "datasources": needed["datasources"],
                "sandbox": any(n["type"] == "code" for n in nodes),
                "skills": needed["skills"],
                "secrets": needed["secrets"],
            }
            reason = unsupported(
                nodes,
                flowfiles.knowledge_ids(shape),
                [d["kind"] for d in needed["datasources"]],
            )
        return {
            "platform": {"min_version": __version__},
            **derived,
            **(await self.store.get(app)).as_dict(),
            "unsupported": reason,
        }

    async def build(self, app: str, version: int, signer: Signer) -> dict[str, Any]:
        """The version as a signed package; Unsupported when step 1 cannot package it."""
        requirements = await self.requirements(app, version)
        reason = requirements.pop("unsupported")
        if reason:
            raise Unsupported(reason)
        _, app_file = await self._catalog.export(app, version)
        skills = []
        for ref in requirements["skills"]:
            content = await self._runs.skills.version(ref["name"], ref["version"])
            if content is None:
                raise Unsupported(f"Skill「{ref['name']}」第 {ref['version']} 版不在平台上")
            skills.append(
                {
                    "name": ref["name"],
                    "version": ref["version"],
                    "description": content.description,
                    "body": content.body,
                    "references": dict(content.references),
                }
            )
        flow = await self._runs.flows.get(app)
        assert flow is not None
        return signer.sign(
            {
                "format": FORMAT,
                "package_id": str(uuid.uuid4()),
                "issued_at": datetime.now(UTC).isoformat(),
                "issuer": {"platform_version": __version__, "key_id": signer.key_id},
                "app": app,
                "version": version,
                "kind": flow.kind,
                "title": flow.title,
                "summary": flow.summary,
                "app_file": app_file,
                "skills": skills,
                "requirements": requirements,
            }
        )
