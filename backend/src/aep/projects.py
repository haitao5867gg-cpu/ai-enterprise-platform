"""Projects: a platform resource any app's runs may bind (docs/api/projects.md).

A project says where its runs write in Databricks (environments: catalog, schema,
cluster), which prefix its tables carry, which tables it only reads (``references``,
each with an optional read filter) and which existing tables are its own
(``adopted``), the run parameters its runs fill in, its acceptance checks (``checks``,
in the format of the ``data_check`` node) and its data outbound policy.

A run that binds a project keeps a snapshot of it (``snapshot``): changing or deleting
the project later does not change the run. Nodes read the snapshot through
``{{param.<key>}}`` and ``{{project.<field>}}`` (``project_value``).
"""

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol

from psycopg import errors
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from aep.cluster_harness import PARAM
from aep.policy import LEVELS, DataPolicy
from aep.runtime.store import DEFAULT_TENANT

# Keys of environments and parameters follow flow node ids (aep.runtime.flow.nodes.ID).
KEY = r"^[a-z][a-z0-9_]{0,31}$"
PROJECT_ID = r"^[a-z][a-z0-9-]{1,39}$"
PREFIX = r"^[a-z][a-z0-9_]{1,29}$"
TABLE = re.compile(r"^[a-z0-9_]{1,255}$")
_PART = r"[^\s`.]{1,255}"
THREE_PART = re.compile(rf"^{_PART}\.{_PART}\.{_PART}$")
# Postgres text cannot hold NUL.
NO_NUL = r"^[^\x00]*$"

PARAM_TYPES = ("text", "number", "boolean", "date", "month", "select")
# What ``{{project.<field>}}`` may name.
FIELDS = (
    "project_id",
    "name",
    "description",
    "table_prefix",
    "environment",
    "catalog",
    "schema",
    "references",
    "params",
    "checks",
    "brief",
)
MAX_CHECKS = 50


def _text(low: int, high: int) -> Any:
    return Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=low, max_length=high, pattern=NO_NUL),
    ]


# Read filters


_LITERAL = re.compile(r"'(?:[^']|'')*'")
_SELECT = re.compile(r"\bselect\b", re.IGNORECASE)


def filter_problem(text: str) -> str | None:
    """Why ``text`` cannot be a read filter, or None. Only its shape is checked (it goes
    as it is into ``WHERE (…)``; Databricks judges the rest): no semicolons, comments
    or SELECT, and balanced parentheses, outside string literals."""
    if text.count("'") % 2:
        return "读取筛选的引号要配对"
    bare = _LITERAL.sub("''", text)
    if ";" in bare:
        return "读取筛选不能有分号"
    if "--" in bare or "/*" in bare or "*/" in bare:
        return "读取筛选不能有注释"
    if _SELECT.search(bare):
        return "读取筛选不能有 SELECT"
    depth = 0
    for char in bare:
        depth += {"(": 1, ")": -1}.get(char, 0)
        if depth < 0:
            break
    if depth:
        return "读取筛选的括号要配对"
    return None


# Parameters


def _parse(value: str, form: str) -> bool:
    try:
        datetime.strptime(value, form)
    except ValueError:
        return False
    return True


def param_problem(param: dict[str, Any], value: Any) -> str | None:
    """Why ``value`` does not fit the parameter, or None."""
    label, kind = param["label"], param["type"]
    if kind == "text" and not (isinstance(value, str) and 1 <= len(value) <= 500):
        return f"参数「{label}」要写 1–500 个字"
    if kind == "number" and (isinstance(value, bool) or not isinstance(value, int | float)):
        return f"参数「{label}」要是数字"
    if kind == "boolean" and not isinstance(value, bool):
        return f"参数「{label}」要是 true 或 false"
    if kind == "date" and not (
        isinstance(value, str)
        and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)
        and _parse(value, "%Y-%m-%d")
    ):
        return f"参数「{label}」要写成 YYYY-MM-DD"
    if kind == "month" and not (
        isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}", value) and _parse(value, "%Y-%m")
    ):
        return f"参数「{label}」要写成 YYYY-MM"
    if kind == "select":
        values = [c["value"] for c in param.get("choices") or []]
        if value not in values:
            return f"参数「{label}」要是这些之一：{'、'.join(map(str, values))}"
    return None


# What the API takes


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Environment(_Model):
    key: Annotated[str, StringConstraints(pattern=KEY)]
    label: _text(1, 50)
    catalog: _text(1, 255)
    schema_: _text(1, 255) = Field(alias="schema")
    cluster_id: _text(1, 200) | None = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Reference(_Model):
    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=800)]
    label: _text(1, 50) | None = None
    filter: _text(1, 2000) | None = None
    description: Annotated[str, StringConstraints(max_length=2000, pattern=NO_NUL)] = ""

    @model_validator(mode="after")
    def valid(self) -> "Reference":
        if not THREE_PART.match(self.name):
            raise ValueError(
                f"引用表 {self.name} 要写成 catalog.schema.table 三段，每段不含空白和反引号"
            )
        if self.filter is not None and (problem := filter_problem(self.filter)):
            raise ValueError(f"引用表 {self.name}：{problem}")
        if self.label is None:
            self.label = self.name.rsplit(".", 1)[1][:50]
        return self


class Choice(_Model):
    value: _text(1, 200)
    label: _text(1, 50) | None = None


class Param(_Model):
    key: Annotated[str, StringConstraints(pattern=KEY)]
    label: _text(1, 50)
    type: Literal["text", "number", "boolean", "date", "month", "select"]
    required: bool = False
    default: Any = None
    choices: list[Choice] | None = None
    description: Annotated[str, StringConstraints(max_length=500, pattern=NO_NUL)] = ""

    @model_validator(mode="after")
    def valid(self) -> "Param":
        if self.type == "select":
            if not self.choices or not 1 <= len(self.choices) <= 50:
                raise ValueError(f"参数「{self.label}」是选项，要列出 1–50 个选项")
            values = [c.value for c in self.choices]
            if len(set(values)) != len(values):
                raise ValueError(f"参数「{self.label}」的选项值重复")
        elif self.choices is not None:
            raise ValueError(f"参数「{self.label}」不是选项，不能写 choices")
        if self.default is not None:
            problem = param_problem(self.model_dump(), self.default)
            if problem:
                raise ValueError(f"默认值不对：{problem}")
        return self


class Policy(_Model):
    samples: Literal["full", "masked", "stats", "schema"] | None = None
    models: list[_text(1, 100)] | None = Field(None, min_length=1, max_length=20)


class ProjectBody(_Model):
    project_id: Annotated[str, StringConstraints(pattern=PROJECT_ID)]
    name: _text(1, 100)
    description: Annotated[str, StringConstraints(max_length=2000, pattern=NO_NUL)] = ""
    table_prefix: Annotated[str, StringConstraints(pattern=PREFIX)]
    environments: list[Environment] = Field(min_length=1, max_length=10)
    references: list[Reference] = Field(default_factory=list, max_length=100)
    adopted: list[Annotated[str, StringConstraints(strip_whitespace=True)]] = Field(
        default_factory=list, max_length=200
    )
    params: list[Param] = Field(default_factory=list, max_length=20)
    # The project's acceptance checks: Check of the data_check node.
    checks: list[Any] = Field(default_factory=list, max_length=MAX_CHECKS)
    policy: Policy = Field(default_factory=Policy)

    @model_validator(mode="after")
    def valid(self) -> "ProjectBody":
        if self.project_id == "new":
            raise ValueError("项目标识不能是 new，页面地址 /projects/new 用了它")
        for what, keys in (
            ("环境", [e.key for e in self.environments]),
            ("引用表", [r.name for r in self.references]),
            ("参数", [p.key for p in self.params]),
            ("归本项目的表", self.adopted),
        ):
            repeated = sorted({k for k in keys if keys.count(k) > 1})
            if repeated:
                raise ValueError(f"{what} {'、'.join(repeated)} 重复")
        for table in self.adopted:
            if not TABLE.match(table):
                raise ValueError(f"表名 {table} 只能用小写字母、数字和下划线，不含 catalog、schema")
            if not table.startswith(self.table_prefix):
                raise ValueError(f"归本项目的表 {table} 要以表名前缀 {self.table_prefix} 开头")
        if problem := checks_problem(self.checks, self.table_prefix, {p.key for p in self.params}):
            raise ValueError(problem)
        return self

    def body(self) -> dict[str, Any]:
        """As stored and returned (environments with ``schema``)."""
        return self.model_dump(by_alias=True)


def checks_problem(found: list[Any], prefix: str, params: set[str]) -> str | None:
    """The first thing wrong with a project's acceptance checks: one the data_check node
    would refuse, a repeated id or ``golden``, a run parameter the project lacks, or a
    result table (a name without dots) without the table prefix."""
    # aep.etl.checks reads filter_problem from this module.
    from aep.etl import checks

    seen: set[str] = set()
    for n, check in enumerate(found, start=1):
        cid = check.get("id") if isinstance(check, dict) else None
        named = f"验收检查 {cid}" if isinstance(cid, str) and cid else f"第 {n} 条验收检查"
        if wrong := checks.problems(check):
            loc, message = wrong[0]
            where = ".".join(str(x) for x in loc)
            return f"{named}{f'（{where}）' if where else ''}：{message}"
        if cid == checks.GOLDEN:
            return "验收检查的 id 不能用 golden，它留给数据检查节点和老系统的对比"
        if cid in seen:
            return f"验收检查 id {cid} 重复"
        seen.add(cid)  # type: ignore[arg-type]
        for where, text in checks.texts(check):
            for key in PARAM.findall(text) if isinstance(text, str) else []:
                if key not in params:
                    return f"{named}（{where}）用的运行参数 ${{param.{key}}} 不是本项目定义的参数"
        for where, table in checks.tables(check):
            if isinstance(table, str) and "." not in table and not table.startswith(prefix):
                return (
                    f"{named}（{where}）：不带 catalog、schema 的 {table} 是本项目在运行环境里的"
                    f"表，要以表名前缀 {prefix} 开头"
                )
    return None


# Stored projects


class ProjectExists(Exception):
    pass


class DuplicateName(Exception):
    pass


@dataclass(frozen=True)
class Project:
    project_id: str
    name: str
    # Everything ``ProjectBody`` takes, as validated.
    body: dict[str, Any]
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def as_dict(self) -> dict[str, Any]:
        return self.body | {
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    def summary(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "name": self.name,
            "description": self.body["description"],
            "environments": [
                {"key": e["key"], "label": e["label"]} for e in self.body["environments"]
            ],
            "updated_at": self.updated_at.isoformat(),
        }


class ProjectStore(Protocol):
    async def setup(self) -> None: ...
    async def all(self) -> list[Project]: ...
    async def get(self, project_id: str) -> Project | None: ...
    async def create(self, body: dict[str, Any]) -> Project: ...
    async def replace(self, body: dict[str, Any]) -> Project | None: ...
    async def delete(self, project_id: str) -> bool: ...


class MemoryProjectStore:
    """In-process store for tests."""

    def __init__(self) -> None:
        self._items: dict[str, Project] = {}

    async def setup(self) -> None:
        return None

    async def all(self) -> list[Project]:
        return sorted(self._items.values(), key=lambda p: p.name)

    async def get(self, project_id: str) -> Project | None:
        return self._items.get(project_id)

    def _check_name(self, body: dict[str, Any]) -> None:
        if any(
            p.name == body["name"] and p.project_id != body["project_id"]
            for p in self._items.values()
        ):
            raise DuplicateName(body["name"])

    async def create(self, body: dict[str, Any]) -> Project:
        if body["project_id"] in self._items:
            raise ProjectExists(body["project_id"])
        self._check_name(body)
        project = Project(body["project_id"], body["name"], body)
        self._items[project.project_id] = project
        return project

    async def replace(self, body: dict[str, Any]) -> Project | None:
        found = self._items.get(body["project_id"])
        if found is None:
            return None
        self._check_name(body)
        project = Project(found.project_id, body["name"], body, found.created_at)
        self._items[project.project_id] = project
        return project

    async def delete(self, project_id: str) -> bool:
        return self._items.pop(project_id, None) is not None


SCHEMA = [
    """
CREATE TABLE IF NOT EXISTS projects (
    project_id text NOT NULL,
    tenant_id  text NOT NULL DEFAULT 'default',
    name       text NOT NULL,
    body       jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, project_id),
    UNIQUE (tenant_id, name)
)""",
]

COLUMNS = "project_id, name, body, created_at, updated_at"


def _record(row: dict[str, Any]) -> Project:
    return Project(**row)


class PostgresProjectStore:
    """Projects in Postgres. The pool must use dict rows and autocommit."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            for statement in SCHEMA:
                await conn.execute(statement)

    async def all(self) -> list[Project]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {COLUMNS} FROM projects WHERE tenant_id = %s ORDER BY name",
                (DEFAULT_TENANT,),
            )
            return [_record(row) for row in await cur.fetchall()]

    async def get(self, project_id: str) -> Project | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {COLUMNS} FROM projects WHERE tenant_id = %s AND project_id = %s",
                (DEFAULT_TENANT, project_id),
            )
            row = await cur.fetchone()
            return _record(row) if row else None

    async def create(self, body: dict[str, Any]) -> Project:
        async with self._pool.connection() as conn:
            try:
                cur = await conn.execute(
                    "INSERT INTO projects (project_id, tenant_id, name, body)"
                    f" VALUES (%s, %s, %s, %s) RETURNING {COLUMNS}",
                    (body["project_id"], DEFAULT_TENANT, body["name"], Jsonb(body)),
                )
            except errors.UniqueViolation as exc:
                if "projects_pkey" in str(exc):
                    raise ProjectExists(body["project_id"]) from exc
                raise DuplicateName(body["name"]) from exc
            return _record(await cur.fetchone())

    async def replace(self, body: dict[str, Any]) -> Project | None:
        async with self._pool.connection() as conn:
            try:
                cur = await conn.execute(
                    "UPDATE projects SET name = %s, body = %s, updated_at = now()"
                    f" WHERE tenant_id = %s AND project_id = %s RETURNING {COLUMNS}",
                    (body["name"], Jsonb(body), DEFAULT_TENANT, body["project_id"]),
                )
            except errors.UniqueViolation as exc:
                raise DuplicateName(body["name"]) from exc
            row = await cur.fetchone()
            return _record(row) if row else None

    async def delete(self, project_id: str) -> bool:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM projects WHERE tenant_id = %s AND project_id = %s",
                (DEFAULT_TENANT, project_id),
            )
            return cur.rowcount > 0


# Binding a run


class BindError(Exception):
    """A run cannot bind the project as asked; the message is for people."""


def snapshot(project: Project, environment: str | None, given: Any) -> dict[str, Any]:
    """What a run keeps of the project: the chosen environment and the parameter values
    (defaults filled in), checked; raises ``BindError``."""
    body = project.body
    envs = {e["key"]: e for e in body["environments"]}
    key = environment or body["environments"][0]["key"]
    if key not in envs:
        raise BindError(f"项目「{project.name}」没有环境 {key}")
    if given is None:
        given = {}
    if not isinstance(given, dict):
        raise BindError("运行参数要是一个对象")
    params = {p["key"]: p for p in body["params"]}
    for name in given:
        if name not in params:
            raise BindError(f"项目「{project.name}」没有参数 {name}")
    values: dict[str, Any] = {}
    for p in body["params"]:
        value = given.get(p["key"])
        if value is None:
            value = p.get("default")
        if value is None:
            if p.get("required"):
                raise BindError(f"请填写参数「{p['label']}」")
            values[p["key"]] = None
            continue
        if problem := param_problem(p, value):
            raise BindError(problem)
        values[p["key"]] = value
    return {
        "project_id": project.project_id,
        "name": project.name,
        "description": body["description"],
        "table_prefix": body["table_prefix"],
        "environment": envs[key],
        "references": body["references"],
        "adopted": body["adopted"],
        "params": body["params"],
        "values": values,
        "checks": body.get("checks") or [],
        "policy": body["policy"],
    }


def effective_policy(project: dict[str, Any] | None, platform: DataPolicy) -> DataPolicy:
    """The policy a run follows: the project's where it sets one, else the platform's."""
    own = (project or {}).get("policy") or {}
    samples = own.get("samples")
    models = own.get("models")
    return DataPolicy(
        samples if samples in LEVELS else platform.samples,
        tuple(models) if models else platform.models,
    )


def run_summary(project: dict[str, Any] | None) -> dict[str, Any] | None:
    """The project as a run record shows it."""
    if not project:
        return None
    env = project["environment"]
    return {
        "project_id": project["project_id"],
        "name": project["name"],
        "environment": env["key"],
        "environment_label": env["label"],
        "params": project["values"],
        "param_labels": {p["key"]: p["label"] for p in project["params"]},
        # The project's own setting; the page says which part of the policy it set.
        "policy": project.get("policy") or {"samples": None, "models": None},
    }


def project_value(project: dict[str, Any] | None, name: str) -> Any:
    """``{{project.<name>}}`` in a run bound to ``project`` (a snapshot); None unbound
    (``brief`` is "")."""
    if not project:
        return "" if name == "brief" else None
    env = project["environment"]
    if name == "environment":
        return env["key"]
    if name in ("catalog", "schema"):
        return env[name]
    if name == "params":
        return [
            {
                "key": p["key"],
                "label": p["label"],
                "type": p["type"],
                "value": project["values"].get(p["key"]),
                "description": p.get("description", ""),
            }
            for p in project["params"]
        ]
    if name == "brief":
        return brief(project)
    if name == "checks":
        # Snapshots taken before projects had checks have none.
        return project.get("checks") or []
    if name in ("project_id", "name", "description", "table_prefix", "references"):
        return project[name]
    return None


def _cell(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).replace("|", "\\|").replace("\n", " ")


def brief(project: dict[str, Any]) -> str:
    """What a model is told about the project the run is bound to (Markdown)."""
    env = project["environment"]
    lines = [f"## 项目：{project['name']}"]
    if project["description"]:
        lines += ["", project["description"]]
    lines += [
        "",
        f"- 写到 `{env['catalog']}.{env['schema']}`（环境 {env['label']}），"
        f"表名前缀 `{project['table_prefix']}`",
        f"- 只能写以 `{project['table_prefix']}` 开头、属于本项目的表",
    ]
    if project["references"]:
        lines += [
            "",
            "### 引用表（只读）",
            "",
            "| 表 | 名称 | 读取筛选 | 说明 |",
            "| --- | --- | --- | --- |",
        ]
        lines += [
            f"| `{r['name']}` | {_cell(r['label'])} | {_cell(r.get('filter'))} "
            f"| {_cell(r.get('description') or None)} |"
            for r in project["references"]
        ]
    if project["params"]:
        lines += [
            "",
            "### 运行参数",
            "",
            "| 标识 | 名称 | 类型 | 这次的值 | 说明 |",
            "| --- | --- | --- | --- | --- |",
        ]
        lines += [
            f"| `{p['key']}` | {_cell(p['label'])} | {p['type']} "
            f"| {_cell(project['values'].get(p['key']))} | {_cell(p.get('description') or None)} |"
            for p in project["params"]
        ]
        lines += [
            "",
            '生成的代码用 `param("标识")` 读参数；方案和检查里的条件用 `${param.标识}` 写。',
        ]
    if project.get("checks"):
        from aep.etl import checks

        lines += [
            "",
            "### 验收检查（项目已经写好，每次数据检查都会跑）",
            "",
            "| id | 名称 | 级别 | 检查什么 |",
            "| --- | --- | --- | --- |",
        ]
        lines += [
            f"| `{c['id']}` | {_cell(c['label'])} | {c.get('severity') or 'error'} "
            f"| {_cell(checks.describe(c))} |"
            for c in project["checks"]
        ]
        lines += [
            "",
            "方案里建议的数据检查不要重复这些，id 也不能和它们相同。",
        ]
    return "\n".join(lines)
