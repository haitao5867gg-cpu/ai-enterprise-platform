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
from functools import lru_cache
from typing import Annotated, Any, Literal, Protocol

import re2
from psycopg import errors
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from aep.cluster_harness import DATE_DIRECTIVES, FIXED_FORMATS, PARAM, rule_value
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
    "can_release",
    "release_ready",
    "release_brief",
    "conventions",
    "plan_rules",
    "code_rules",
)
# The environment a run releases to (the template's databricks_release node).
RELEASE_ENVIRONMENT = "prod"
MAX_CHECKS = 50
MAX_CONVENTIONS = 30
# When a convention applies (``Convention.applies_to``) and how the brief says it.
STAGES = ("plan", "code", "both")
STAGE_TEXT = {"plan": "写方案时", "code": "写代码时", "both": "写方案和写代码时"}
MAX_PATTERN = 200
# A text parameter's format is RE2: matching takes linear time, so whatever a project
# writes there cannot stall the API on a value (Python's re can backtrack for minutes).
PATTERN_OPTIONS = re2.Options()
PATTERN_OPTIONS.log_errors = False


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


@lru_cache(maxsize=256)
def _pattern(pattern: str) -> Any:
    return re2.compile(pattern, PATTERN_OPTIONS)


def pattern_problem(pattern: str) -> str | None:
    """Why ``pattern`` cannot be a text parameter's format, or None."""
    try:
        _pattern(pattern)
    except re2.error as exc:
        said = exc.args[0] if exc.args else ""
        said = said.decode(errors="replace") if isinstance(said, bytes) else str(said)
        return f"不是有效的正则表达式（{said}）"
    return None


def param_problem(param: dict[str, Any], value: Any) -> str | None:
    """Why ``value`` does not fit the parameter, or None. A text parameter's ``pattern``
    must match the whole value."""
    label, kind = param["label"], param["type"]
    if kind == "text" and not (isinstance(value, str) and value.strip() and 1 <= len(value) <= 500):
        return f"参数「{label}」要写 1–500 个字，不能只有空格"
    pattern = param.get("pattern")
    if kind == "text" and pattern and _pattern(pattern).fullmatch(value) is None:
        # People read the parameter's description, not the regular expression (jadeflow
        # retest S-19).
        if described := (param.get("description") or "").strip():
            return f"参数「{label}」的格式不对：{described}"
        return f"参数「{label}」的格式不对，要整段符合正则表达式 {pattern}"
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


class Scheduled(_Model):
    """How a released job's scheduled runs fill in a parameter (docs/api/projects.md,
    "定时运行时参数怎么取"): ``this_run`` keeps the value of the run that released it,
    ``run_id`` takes the Databricks job run's id, ``date`` works out a date ``months``
    from the day it runs (its ``first`` or ``last`` day, or the same day), written in
    ``format`` for a text parameter."""

    use: Literal["this_run", "run_id", "date"]
    format: _text(1, 50) | None = None
    months: int = Field(0, ge=-24, le=24)
    day: Literal["first", "last"] | None = None


def format_problem(form: str) -> str | None:
    """Why ``form`` cannot be a text parameter's date format, or None: only %Y, %y, %m
    and %d (and %% for a percent sign), at least one of them."""
    directives = re.findall(r"%.?", form)
    if any(d not in DATE_DIRECTIVES for d in directives):
        return "日期格式只能用 %Y（四位年份）、%y（两位年份）、%m（月）、%d（日）"
    if not any(d != "%%" for d in directives):
        return "日期格式里至少要有 %Y、%y、%m、%d 中的一个"
    return None


class Param(_Model):
    key: Annotated[str, StringConstraints(pattern=KEY)]
    label: _text(1, 50)
    type: Literal["text", "number", "boolean", "date", "month", "select"]
    required: bool = False
    default: Any = None
    choices: list[Choice] | None = None
    # Only for text: a regular expression (RE2) the whole value must match.
    pattern: (
        Annotated[str, StringConstraints(min_length=1, max_length=MAX_PATTERN, pattern=NO_NUL)]
        | None
    ) = None
    description: Annotated[str, StringConstraints(max_length=500, pattern=NO_NUL)] = ""
    # How a released job's scheduled runs fill it in; None: its default, else empty.
    scheduled: Scheduled | None = None

    @model_validator(mode="after")
    def valid(self) -> "Param":
        if self.scheduled is not None and (problem := self._scheduled_problem(self.scheduled)):
            raise ValueError(f"参数「{self.label}」定时运行时的取法不对：{problem}")
        if self.pattern is not None:
            if self.type != "text":
                raise ValueError(f"参数「{self.label}」不是文本，不能写格式")
            if problem := pattern_problem(self.pattern):
                raise ValueError(f"参数「{self.label}」的格式{problem}")
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

    def _scheduled_problem(self, rule: Scheduled) -> str | None:
        if rule.use != "date" and (rule.format or rule.months or rule.day):
            return "只有按日期推算（date）才写 format、months、day"
        if rule.use == "run_id" and self.type != "text":
            return "作业运行的 ID 只能填进文本参数"
        if rule.use == "date":
            if self.type not in ("text", "date", "month"):
                return "按日期推算只能用在文本、日期或月份参数上"
            if self.type == "text" and rule.format is None:
                return "文本参数要写日期格式 format，如 FY%y_%m_M"
            if self.type != "text" and rule.format is not None:
                return f"{'日期' if self.type == 'date' else '月份'}参数的格式是固定的，不写 format"
            if self.type == "month" and rule.day is not None:
                return "月份参数不用写 day"
            if rule.format is not None and (problem := format_problem(rule.format)):
                return problem
        return None


class Convention(_Model):
    """A rule the project's runs follow (jadeflow 4th retest I-18: the first code kept
    forgetting to filter by version): told to the model that writes the plan or the code
    (``brief``) and the checklist of a review node (``plan_rules``, ``code_rules``)."""

    key: Annotated[str, StringConstraints(pattern=KEY)]
    title: _text(1, 100)
    text: Annotated[str, StringConstraints(max_length=2000, pattern=NO_NUL)] = ""
    applies_to: Literal["plan", "code", "both"] = "both"


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
    conventions: list[Convention] = Field(default_factory=list, max_length=MAX_CONVENTIONS)
    # The highest severity a check the model proposes in a plan runs at (jadeflow 4th
    # retest I-10); the project's own checks keep theirs.
    model_check_max_severity: Literal["error", "warn", "info"] = "error"
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
            ("约定", [c.key for c in self.conventions]),
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
        # Text is taken without the spaces around it; spaces only is not filled in (jadeflow
        # retest S-19: a run_id of spaces started a run).
        if p["type"] == "text" and isinstance(value, str):
            value = value.strip() or None
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
        # All of them, for a release to another environment (databricks_release).
        "environments": body["environments"],
        "references": body["references"],
        "adopted": body["adopted"],
        "params": body["params"],
        "values": values,
        "checks": body.get("checks") or [],
        "conventions": body.get("conventions") or [],
        "model_check_max_severity": body.get("model_check_max_severity") or "error",
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


def project_value(
    project: dict[str, Any] | None, name: str, timezone: str = "UTC", now: datetime | None = None
) -> Any:
    """``{{project.<name>}}`` in a run bound to ``project`` (a snapshot); None unbound
    (``brief`` and ``release_brief`` are "", the conventions and review rules []). The
    release fields work out scheduled values on the day it is ``now`` in ``timezone``
    (where released jobs run)."""
    if not project:
        if name in ("conventions", "plan_rules", "code_rules"):
            return []
        return "" if name in ("brief", "release_brief") else None
    if name == "can_release":
        return can_release(project)
    if name == "release_ready":
        return can_release(project) and not scheduled_values(project, timezone, now).problems
    if name == "release_brief":
        return release_brief(project, timezone, now) if can_release(project) else ""
    if name == "conventions":
        # Snapshots taken before projects had conventions have none.
        return project.get("conventions") or []
    if name in ("plan_rules", "code_rules"):
        return review_rules(project, name.removesuffix("_rules"))
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


def _kind(param: dict[str, Any]) -> str:
    pattern = param.get("pattern")
    return param["type"] + (f"，格式 `{_cell(pattern)}`" if pattern else "")


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
            f"| `{p['key']}` | {_cell(p['label'])} | {_kind(p)} "
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
    cap = project.get("model_check_max_severity") or "error"
    if cap != "error":
        lines += [
            "",
            f"方案里建议的数据检查最高只能是 {cap}（项目规定），写得更高的按 {cap} 运行。",
        ]
    if project.get("conventions"):
        lines += ["", "### 约定（必须遵守，平台会按它们审查）", ""]
        lines += [
            f"- **{c['title']}**（{STAGE_TEXT.get(c.get('applies_to') or 'both', '')}）"
            + (f"：{c['text']}" if c.get("text") else "")
            for c in project["conventions"]
        ]
    return "\n".join(lines)


def review_rules(project: dict[str, Any] | None, stage: str) -> list[dict[str, str]]:
    """The project's conventions for ``stage`` (plan or code) as a review node's checklist
    (``{{project.plan_rules}}``, ``{{project.code_rules}}``)."""
    return [
        {"key": c["key"], "title": c["title"], "instruction": c.get("text") or ""}
        for c in (project or {}).get("conventions") or []
        if (c.get("applies_to") or "both") in (stage, "both")
    ]


# Releasing: what a released job's scheduled runs use


def can_release(project: dict[str, Any]) -> bool:
    """Whether a run bound to ``project`` can release to its prod environment: the
    project has one and the run is not in it (snapshots from before runs kept all the
    environments cannot)."""
    keys = [e.get("key") for e in project.get("environments") or []]
    return RELEASE_ENVIRONMENT in keys and project["environment"]["key"] != RELEASE_ENVIRONMENT


@dataclass(frozen=True)
class Scheduling:
    """How a released job fills in the run parameters (``scheduled_values``): ``fixed``
    values set at the release, ``rules`` worked out at each run (the job's
    ``param_rules``, read by ``aep.cluster_harness.scheduled_params``), a line per
    parameter for people (``lines``: key, label, how, the value on ``today``, a warning)
    and why the job cannot be released (``problems``)."""

    fixed: dict[str, Any]
    rules: list[dict[str, Any]]
    lines: list[dict[str, Any]]
    problems: list[str]
    today: str
    timezone: str


def _months_text(months: int, day: str | None, with_day: bool) -> str:
    month = {-1: "上个月", 0: "当月", 1: "下个月"}.get(
        months, f"{abs(months)} 个月{'前' if months < 0 else '后'}"
    )
    if not with_day:
        return month
    if day is not None:
        return f"{month}{'第一天' if day == 'first' else '最后一天'}"
    if months == 0:
        return "运行当天"
    return f"{month}的同一天（没有这一天时取月底）"


def _form_text(form: str) -> str:
    """A date format as people write it: ``FY%y_%m_M`` is ``FY{yy}_{MM}_M``."""
    names = {"%Y": "{yyyy}", "%y": "{yy}", "%m": "{MM}", "%d": "{dd}", "%%": "%"}
    return re.sub(r"%.", lambda m: names.get(m[0], m[0]), form)


def scheduled_values(
    project: dict[str, Any], timezone: str = "UTC", now: datetime | None = None
) -> Scheduling:
    """How a job released from a run bound to ``project`` fills in each run parameter:
    by its ``scheduled`` rule, else its default, else empty; a required parameter with
    neither cannot be filled in, so the job cannot be released."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        zone: Any = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        zone, timezone = UTC, "UTC"
    today = (now or datetime.now(UTC)).astimezone(zone).date()
    fixed: dict[str, Any] = {}
    rules: list[dict[str, Any]] = []
    lines: list[dict[str, Any]] = []
    problems: list[str] = []
    values = project.get("values") or {}
    for p in project.get("params") or []:
        key, label = p["key"], p["label"]
        rule = p.get("scheduled")
        line: dict[str, Any] = {"key": key, "label": label, "value": None, "warning": None}
        if rule is None:
            if p.get("default") is not None:
                fixed[key] = p["default"]
                line |= {"how": "用默认值", "value": p["default"]}
            elif not p.get("required"):
                fixed[key] = None
                line |= {"how": "不填（空值）"}
            else:
                line |= {"how": "没有写定时运行时怎么取，也没有默认值"}
                problems.append(
                    f"参数「{label}」没有写定时运行时怎么取，也没有默认值：在项目的运行参数里"
                    "给它写上定时运行时的取法（或者默认值），再重新发起运行"
                )
        elif rule["use"] == "this_run":
            fixed[key] = values.get(key)
            line |= {"how": "一直用这次运行的值", "value": values.get(key)}
        else:
            found = {
                "key": key,
                "label": label,
                "type": p["type"],
                "use": rule["use"],
                "format": rule.get("format"),
                "months": rule.get("months") or 0,
                "day": rule.get("day"),
                "pattern": p.get("pattern"),
                "description": p.get("description") or "",
            }
            rules.append(found)
            fixed[key] = None
            if rule["use"] == "run_id":
                line |= {"how": "每次用 Databricks 作业运行的 ID", "value": "（运行时才知道）"}
            else:
                form = FIXED_FORMATS.get(p["type"]) or rule["format"]
                with_day = "%d" in form
                when = _months_text(found["months"], found["day"], with_day)
                value = rule_value(found, today, "")
                line |= {
                    "how": f"按运行那天推算{when}，写成 {_form_text(form)}",
                    "value": value,
                }
                if (pattern := p.get("pattern")) and _pattern(pattern).fullmatch(value) is None:
                    line["warning"] = "这个值不符合参数的格式，这样的日子定时运行会报错、不写表"
        lines.append(line)
    return Scheduling(fixed, rules, lines, problems, today.isoformat(), timezone)


def release_brief(
    project: dict[str, Any], timezone: str = "UTC", now: datetime | None = None
) -> str:
    """What a person reads before releasing a run bound to ``project`` to its prod
    environment (Markdown): where it goes, what runs, the parameters of the scheduled
    runs and how to take it back."""
    env = next(e for e in project["environments"] if e["key"] == RELEASE_ENVIRONMENT)
    scheduling = scheduled_values(project, timezone, now)
    lines = [
        "## 上线说明",
        "",
        f"- 上线到项目「{project['name']}」的 {env['label']} 环境（{env['key']}）："
        f"结果写到 `{env['catalog']}.{env['schema']}`，只写以 `{project['table_prefix']}` "
        "开头、属于本项目的表，按方案的写表方式写。",
        "- 平台在 Databricks 上建一个 notebook Job（这个项目和应用已有的就更新它），"
        "按选的时间自动运行；上线时不会马上运行。",
    ]
    if project.get("params"):
        lines += [
            "",
            f"### 定时运行时的参数（按今天 {scheduling.today}，{scheduling.timezone}）",
            "",
            "| 参数 | 定时运行时怎么取 | 今天运行的话 |",
            "| --- | --- | --- |",
        ]
        for line in scheduling.lines:
            value = _cell(line["value"]) if line["value"] is not None else "—"
            if line["warning"]:
                value += f"（{line['warning']}）"
            lines.append(f"| {_cell(line['label'])}（`{line['key']}`） | {line['how']} | {value} |")
    if scheduling.problems:
        lines += ["", "**现在不能上线：**", ""]
        lines += [f"- {problem}" for problem in scheduling.problems]
    lines += [
        "",
        "### 怎么撤销",
        "",
        "- 在 Databricks 的 Jobs 里删掉这个 Job，或者暂停它的定时，以后就不再自动运行。",
        "- 已经写进表里的数据不会跟着撤销；Delta 表可以用 `RESTORE TABLE … TO VERSION AS OF …`"
        " 恢复到写之前的版本。",
    ]
    return "\n".join(lines)
