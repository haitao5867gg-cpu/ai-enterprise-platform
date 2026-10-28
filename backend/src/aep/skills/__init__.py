"""Skills: reusable know-how for a model node, kept and versioned as company assets.

A skill follows the open Agent Skills format: a SKILL.md body (Markdown) and
optional reference files (``references/*.md``); text only for now, no scripts.
A model node's prompt is its skills' text followed by the platform's fixed part
(the output format the platform parses, see ``aep.runtime.appconfig``), so
changing a skill changes how the work is done, never what the platform reads back.

A skill is edited as a draft; publishing the draft makes it the next version, and
a published version never changes, so an app version that names ``etl-plan``
version 2 always gets the same text. The platform's own skills live in
``builtin/<name>/SKILL.md`` (with YAML front matter: ``name``, ``description``)
and are registered as version 1 on first start.
"""

import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import cache
from pathlib import Path
from typing import Any, Protocol

import yaml
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from aep.runtime.store import DEFAULT_TENANT

NAME = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
REFERENCE_NAME = re.compile(r"^[\w.-]{1,64}\.md$", re.ASCII)
MAX_TEXT = 20000
MAX_DESCRIPTION = 500
MAX_REFERENCES = 10
BUILTIN_NOTE = "平台内置"
BUILTIN_DIR = Path(__file__).parent / "builtin"


class SkillError(ValueError):
    """The content is not a valid skill; ``problems`` are (field, message) pairs."""

    def __init__(self, problems: list[tuple[str, str]]) -> None:
        super().__init__("；".join(message for _, message in problems))
        self.problems = problems


class SkillNotFound(LookupError):
    pass


class SkillExists(Exception):
    pass


class NoDraft(Exception):
    pass


@dataclass(frozen=True)
class SkillContent:
    """A published version of a skill, or its draft (``version`` None)."""

    description: str
    body: str
    references: dict[str, str] = field(default_factory=dict)
    version: int | None = None
    note: str | None = None
    created_by: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def text(self) -> str:
        """What goes into a prompt: the body, then each reference file."""
        parts = [self.body.strip()]
        for name, content in sorted(self.references.items()):
            parts.append(f"参考资料 {name}：\n{content.strip()}")
        return "\n\n".join(parts)


@dataclass(frozen=True)
class Skill:
    name: str
    versions: list[SkillContent]  # newest first
    draft: SkillContent | None = None

    @property
    def current(self) -> SkillContent | None:
        return self.versions[0] if self.versions else None

    @property
    def published_version(self) -> int | None:
        return self.current.version if self.current else None

    @property
    def description(self) -> str:
        latest = self.draft or self.current
        return latest.description if latest else ""

    @property
    def updated_at(self) -> datetime:
        return max(c.created_at for c in [*self.versions, *([self.draft] if self.draft else [])])


def check_name(name: str) -> None:
    if not NAME.match(name):
        raise SkillError([("name", "名称只能用小写字母、数字和连字符，以字母开头，2 到 64 个字符")])


def check_content(content: SkillContent) -> None:
    problems: list[tuple[str, str]] = []
    if not content.description.strip():
        problems.append(("description", "说明不能为空"))
    elif len(content.description) > MAX_DESCRIPTION:
        problems.append(("description", f"说明最多 {MAX_DESCRIPTION} 个字符"))
    if not content.body.strip():
        problems.append(("body", "正文不能为空"))
    elif len(content.body) > MAX_TEXT:
        problems.append(("body", f"正文最多 {MAX_TEXT} 个字符"))
    if len(content.references) > MAX_REFERENCES:
        problems.append(("references", f"参考资料最多 {MAX_REFERENCES} 个"))
    for name, text in content.references.items():
        if not REFERENCE_NAME.match(name):
            message = f"参考资料文件名 {name} 不合法：用字母、数字、._-，以 .md 结尾"
            problems.append((f"references.{name}", message))
        elif len(text) > MAX_TEXT:
            problems.append((f"references.{name}", f"参考资料 {name} 最多 {MAX_TEXT} 个字符"))
    if problems:
        raise SkillError(problems)


def parse_skill_md(text: str) -> tuple[dict[str, Any], str]:
    """A SKILL.md's front matter and body."""
    match = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.S)
    if not match:
        return {}, text
    meta = yaml.safe_load(match.group(1)) or {}
    return (meta if isinstance(meta, dict) else {}), match.group(2)


@cache
def builtin_skills() -> dict[str, SkillContent]:
    """The platform's own skills, as their version 1."""
    found: dict[str, SkillContent] = {}
    for path in sorted(BUILTIN_DIR.glob("*/SKILL.md")):
        meta, body = parse_skill_md(path.read_text(encoding="utf-8"))
        references = {
            ref.name: ref.read_text(encoding="utf-8")
            for ref in sorted((path.parent / "references").glob("*.md"))
        }
        name = meta.get("name") or path.parent.name
        content = SkillContent(
            str(meta.get("description", "")).strip(),
            body.strip() + "\n",
            references,
            version=1,
            note=BUILTIN_NOTE,
        )
        check_name(name)
        check_content(content)
        found[name] = content
    return found


class SkillStore(Protocol):
    async def setup(self) -> None: ...
    async def all(self) -> list[Skill]: ...
    async def get(self, name: str) -> Skill | None: ...
    async def version(self, name: str, version: int) -> SkillContent | None: ...
    async def create(self, name: str, content: SkillContent) -> Skill: ...
    async def save_draft(self, name: str, content: SkillContent) -> Skill: ...
    async def discard_draft(self, name: str) -> None: ...
    async def publish(self, name: str, note: str, actor: str | None) -> Skill: ...
    async def register_builtins(self) -> int: ...


class MemorySkillStore:
    """In-process store for tests; starts with the built-in skills registered."""

    def __init__(self, builtins: bool = True) -> None:
        self._versions: dict[str, list[SkillContent]] = {}
        self._drafts: dict[str, SkillContent] = {}
        if builtins:
            for name, content in builtin_skills().items():
                self._versions[name] = [content]

    async def setup(self) -> None:
        return None

    async def register_builtins(self) -> int:
        added = 0
        for name, content in builtin_skills().items():
            if name not in self._versions:
                self._versions[name] = [content]
                added += 1
        return added

    def _skill(self, name: str) -> Skill | None:
        if name not in self._versions and name not in self._drafts:
            return None
        return Skill(name, list(self._versions.get(name, [])), self._drafts.get(name))

    async def all(self) -> list[Skill]:
        names = sorted({*self._versions, *self._drafts})
        return [s for n in names if (s := self._skill(n))]

    async def get(self, name: str) -> Skill | None:
        return self._skill(name)

    async def version(self, name: str, version: int) -> SkillContent | None:
        return next((c for c in self._versions.get(name, []) if c.version == version), None)

    async def create(self, name: str, content: SkillContent) -> Skill:
        check_name(name)
        check_content(content)
        if self._skill(name):
            raise SkillExists(name)
        self._drafts[name] = replace(content, version=None, note=None)
        return self._skill(name)  # type: ignore[return-value]

    async def save_draft(self, name: str, content: SkillContent) -> Skill:
        check_content(content)
        if not self._skill(name):
            raise SkillNotFound(name)
        self._drafts[name] = replace(content, version=None, note=None)
        return self._skill(name)  # type: ignore[return-value]

    async def discard_draft(self, name: str) -> None:
        if name not in self._drafts:
            raise NoDraft(name)
        del self._drafts[name]

    async def publish(self, name: str, note: str, actor: str | None) -> Skill:
        draft = self._drafts.pop(name, None)
        if draft is None:
            raise NoDraft(name)
        versions = self._versions.setdefault(name, [])
        number = (versions[0].version or 0) + 1 if versions else 1
        content = replace(
            draft, version=number, note=note, created_by=actor, created_at=datetime.now(UTC)
        )
        versions.insert(0, content)
        return self._skill(name)  # type: ignore[return-value]


SCHEMA = [
    """
CREATE TABLE IF NOT EXISTS skill_versions (
    tenant_id   text NOT NULL DEFAULT 'default',
    name        text NOT NULL,
    version     integer NOT NULL,
    description text NOT NULL,
    body        text NOT NULL,
    refs        jsonb NOT NULL DEFAULT '{}',
    note        text,
    created_by  text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, name, version)
)""",
    """
CREATE TABLE IF NOT EXISTS skill_drafts (
    tenant_id   text NOT NULL DEFAULT 'default',
    name        text NOT NULL,
    description text NOT NULL,
    body        text NOT NULL,
    refs        jsonb NOT NULL DEFAULT '{}',
    created_by  text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, name)
)""",
]

VERSION_COLUMNS = "name, version, description, body, refs, note, created_by, created_at"
DRAFT_COLUMNS = "name, description, body, refs, created_by, created_at"


def _content(row: dict[str, Any]) -> SkillContent:
    return SkillContent(
        row["description"],
        row["body"],
        dict(row["refs"] or {}),
        version=row.get("version"),
        note=row.get("note"),
        created_by=row["created_by"],
        created_at=row["created_at"],
    )


class PostgresSkillStore:
    """Skills in Postgres. The pool must use dict rows and autocommit."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            for statement in SCHEMA:
                await conn.execute(statement)

    async def register_builtins(self) -> int:
        added = 0
        async with self._pool.connection() as conn:
            for name, c in builtin_skills().items():
                cur = await conn.execute(
                    "INSERT INTO skill_versions"
                    " (tenant_id, name, version, description, body, refs, note)"
                    " VALUES (%s, %s, 1, %s, %s, %s, %s) ON CONFLICT DO NOTHING",
                    (DEFAULT_TENANT, name, c.description, c.body, Jsonb(c.references), c.note),
                )
                added += cur.rowcount
        return added

    async def _load(self, name: str | None) -> list[Skill]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {VERSION_COLUMNS} FROM skill_versions WHERE tenant_id = %s"
                " AND (%s::text IS NULL OR name = %s) ORDER BY name, version DESC",
                (DEFAULT_TENANT, name, name),
            )
            versions = await cur.fetchall()
            cur = await conn.execute(
                f"SELECT {DRAFT_COLUMNS} FROM skill_drafts WHERE tenant_id = %s"
                " AND (%s::text IS NULL OR name = %s)",
                (DEFAULT_TENANT, name, name),
            )
            drafts = {row["name"]: _content(row) for row in await cur.fetchall()}
        grouped: dict[str, list[SkillContent]] = {}
        for row in versions:
            grouped.setdefault(row["name"], []).append(_content(row))
        names = sorted({*grouped, *drafts})
        return [Skill(n, grouped.get(n, []), drafts.get(n)) for n in names]

    async def all(self) -> list[Skill]:
        return await self._load(None)

    async def get(self, name: str) -> Skill | None:
        found = await self._load(name)
        return found[0] if found else None

    async def version(self, name: str, version: int) -> SkillContent | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {VERSION_COLUMNS} FROM skill_versions"
                " WHERE tenant_id = %s AND name = %s AND version = %s",
                (DEFAULT_TENANT, name, version),
            )
            row = await cur.fetchone()
            return _content(row) if row else None

    async def _lock(self, conn: Any, name: str) -> None:
        await conn.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"skills:{DEFAULT_TENANT}:{name}",),
        )

    async def _exists(self, conn: Any, name: str) -> bool:
        cur = await conn.execute(
            "SELECT 1 FROM skill_versions WHERE tenant_id = %s AND name = %s"
            " UNION ALL SELECT 1 FROM skill_drafts WHERE tenant_id = %s AND name = %s LIMIT 1",
            (DEFAULT_TENANT, name, DEFAULT_TENANT, name),
        )
        return await cur.fetchone() is not None

    async def _write_draft(self, conn: Any, name: str, c: SkillContent) -> None:
        await conn.execute(
            "INSERT INTO skill_drafts (tenant_id, name, description, body, refs, created_by)"
            " VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (tenant_id, name) DO UPDATE SET"
            " description = excluded.description, body = excluded.body, refs = excluded.refs,"
            " created_by = excluded.created_by, created_at = now()",
            (DEFAULT_TENANT, name, c.description, c.body, Jsonb(c.references), c.created_by),
        )

    async def create(self, name: str, content: SkillContent) -> Skill:
        check_name(name)
        check_content(content)
        async with self._pool.connection() as conn, conn.transaction():
            await self._lock(conn, name)
            if await self._exists(conn, name):
                raise SkillExists(name)
            await self._write_draft(conn, name, content)
        return await self.get(name)  # type: ignore[return-value]

    async def save_draft(self, name: str, content: SkillContent) -> Skill:
        check_content(content)
        async with self._pool.connection() as conn, conn.transaction():
            await self._lock(conn, name)
            if not await self._exists(conn, name):
                raise SkillNotFound(name)
            await self._write_draft(conn, name, content)
        return await self.get(name)  # type: ignore[return-value]

    async def discard_draft(self, name: str) -> None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM skill_drafts WHERE tenant_id = %s AND name = %s",
                (DEFAULT_TENANT, name),
            )
            if cur.rowcount == 0:
                raise NoDraft(name)

    async def publish(self, name: str, note: str, actor: str | None) -> Skill:
        async with self._pool.connection() as conn, conn.transaction():
            await self._lock(conn, name)
            cur = await conn.execute(
                "DELETE FROM skill_drafts WHERE tenant_id = %s AND name = %s"
                " RETURNING description, body, refs",
                (DEFAULT_TENANT, name),
            )
            draft = await cur.fetchone()
            if draft is None:
                raise NoDraft(name)
            await conn.execute(
                "INSERT INTO skill_versions"
                " (tenant_id, name, version, description, body, refs, note, created_by)"
                " SELECT %s, %s, coalesce(max(version), 0) + 1, %s, %s, %s, %s, %s"
                " FROM skill_versions WHERE tenant_id = %s AND name = %s",
                (
                    DEFAULT_TENANT,
                    name,
                    draft["description"],
                    draft["body"],
                    Jsonb(draft["refs"]),
                    note,
                    actor,
                    DEFAULT_TENANT,
                    name,
                ),
            )
        return await self.get(name)  # type: ignore[return-value]
