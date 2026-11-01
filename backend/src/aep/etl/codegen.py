"""Code for a confirmed ETL plan: a Lakeflow declarative pipeline in a Databricks bundle.

The bundle deploys the pipeline file as a Lakeflow pipeline, or, where pipelines
cannot start clusters, as a notebook job on an existing cluster that runs the
same file through ``aep.cluster_harness``.

The model writes only the transformation of each target table (a PySpark
function body or a SQL SELECT). Everything around it (decorators, quality
rules, how sources are read, the bundle) comes from templates here, and
``check`` rejects bodies that do anything but read and transform.

Sources are read through a Unity Catalog foreign catalog (Lakehouse Federation)
named by the bundle variable ``source_catalog``, so database credentials stay in
Databricks and never appear in the code; tables already on Databricks are named
``catalog.schema.table`` and read as they are.

A run bound to a project (docs/api/data-nodes.md, "第三批") also gets the read filters
of the project's reference tables written into the code, its run parameters through
``param()``, its environment as the dev target's defaults, and each table's write mode;
the cluster program (``aep.cluster_harness``) keeps it to the project's tables.
"""

import ast
import json
import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from aep import cluster_harness
from aep.config import Settings
from aep.etl.plan import PLAN_MODES, EtlPlan, QualityRule, Target

PIPELINE_PY = "src/pipeline.py"
PIPELINE_SQL = "src/pipeline.sql"
# A bundle deployed as a job: a notebook running the pipeline file through the harness.
JOB_NOTEBOOK = "src/run.py"
JOB_HARNESS = "src/aep_harness.py"

# The platform's part of the codegen node's prompt: the JSON it parses and the rules
# ``check_code`` enforces. How to write good code is the ``etl-codegen`` skill.
OUTPUT_FORMAT = """只输出一个 JSON 对象：{"targets": {"目标表名": "代码"}}，\
每张目标表都要有，不要输出其他文字。

language 是 pyspark 时，代码是一个 Python 函数体（不含 def 那一行），最后 return 一个 DataFrame：
- 读源表只能用 source("schema.table")；读本管道里的其他目标表用 spark.read.table("目标表名")。
- 可以直接用 F（pyspark.sql.functions）和 Window，不要写 import。
- 只读和转换：不要写表、不要 collect / toPandas、不要调用 spark.sql、dbutils 或文件系统。
- 质量规则由平台按方案生成，函数体里不用再写。

language 是 sql 时，代码是一条 SELECT（可以用 WITH），结尾不要分号：
- 源表写成 schema.table，其他目标表直接写表名。
- 只能查询，不要写 INSERT、CREATE 等语句。"""


@dataclass(frozen=True)
class Compute:
    """Classic pipeline compute: each update starts a job cluster of this size.

    ``policy_id`` names a cluster policy to create the cluster under, for workspaces
    where users may create clusters only through a policy; the policy can then
    also fix the node type, which may be left empty.
    """

    node_type: str = ""
    workers: int = 1
    policy_id: str = ""


def compute_for(settings: Settings) -> Compute | None:
    """The pipeline compute the platform's settings ask for; None: serverless."""
    if not (settings.databricks_pipeline_node_type or settings.databricks_pipeline_policy_id):
        return None
    return Compute(
        settings.databricks_pipeline_node_type,
        settings.databricks_pipeline_workers,
        settings.databricks_pipeline_policy_id,
    )


@dataclass(frozen=True)
class Bundle:
    """A bundle's files; ``settings`` are what the cluster program needs besides them when
    the code runs without being deployed (``run_settings``), empty when it needs nothing."""

    name: str
    files: dict[str, str]
    settings: dict[str, Any] = field(default_factory=dict)


class CodeError(ValueError):
    pass


def _rule_decorator(rule: QualityRule) -> str:
    fn = {"warn": "expect", "drop": "expect_or_drop", "fail": "expect_or_fail"}[rule.on_violation]
    return f"@dp.{fn}({json.dumps(rule.name)}, {json.dumps(rule.condition, ensure_ascii=False)})"


def _target_header(target: Target) -> list[str]:
    lines = [f"{target.name}: {target.description}".rstrip(": "), f"做法：{target.logic}"]
    load = target.load
    if load.mode == "incremental":
        lines.append(
            f"增量键：{target.load.key}（部署成 Lakeflow 管道时，物化视图会尽量只处理新增和变更的"
            "数据；Job 模式每次整表重算）"
        )
    elif load.mode == "replace_where":
        lines.append(f"写表方式：只替换满足 {load.where} 的行")
    elif load.mode == "append":
        lines.append("写表方式：追加")
    elif load.mode == "merge":
        keys = "、".join(load.keys or target.primary_key)
        lines.append(f"写表方式：按 {keys} 合并，有的更新，没有的插入")
    return lines


def source_filters(plan: EtlPlan, project: dict[str, Any] | None) -> dict[str, str]:
    """The read filters of the project's reference tables that the plan reads, by the
    name the plan gives the table (reference names are not case sensitive)."""
    filters = {
        r["name"].lower(): r["filter"]
        for r in (project or {}).get("references") or []
        if r.get("filter")
    }
    sources = dict.fromkeys(s for t in plan.targets for s in t.sources)
    return {s: filters[s.lower()] for s in sources if s.lower() in filters}


def run_settings(plan: EtlPlan, project: dict[str, Any] | None) -> dict[str, Any]:
    """What the cluster program needs besides the code: each table's write mode
    (``loads``), in a run bound to a project the parameters' types (``param_types``),
    and whether the code reads through the source catalog (``source_catalog``). Empty
    when every table is overwritten and no project is bound."""
    plain = all(t.load.mode in PLAN_MODES for t in plan.targets)
    if project is None and plain:
        return {}
    loads = {t.name: t.load.stored(t.primary_key) for t in plan.targets}
    kinds = {p["key"]: p["type"] for p in (project or {}).get("params") or []}
    # Whether the code reads through the source catalog (a schema.table) at all.
    catalog = any(s.count(".") == 1 for t in plan.targets for s in t.sources)
    return {"loads": loads, "param_types": kinds, "source_catalog": catalog}


def render_pyspark(
    plan: EtlPlan, bodies: dict[str, str], filters: dict[str, str] | None = None
) -> str:
    shown = json.dumps(filters or {}, ensure_ascii=False)
    parts = [
        '"""Lakeflow declarative pipeline, generated from a confirmed ETL plan.',
        "",
        *textwrap.wrap(plan.summary, 96),
        '"""',
        "",
        "import json",
        "",
        "from pyspark import pipelines as dp",
        "from pyspark.sql import Window",
        "from pyspark.sql import functions as F",
        "",
        'SOURCE_CATALOG = spark.conf.get("source_catalog")',
        "# The read filters the project gives its reference tables.",
        f"SOURCE_FILTERS = {shown}",
        'PARAMS = json.loads(spark.conf.get("aep_params", "{}"))',
        "",
        "",
        "def source(name: str):",
        '    """A source table: schema.table through the source catalog, or',
        '    catalog.schema.table as is."""',
        '    parts = name.split(".")',
        "    if len(parts) == 2:",
        "        parts = [SOURCE_CATALOG, *parts]",
        '    table = spark.read.table(".".join(f"`{part}`" for part in parts))',
        "    if name in SOURCE_FILTERS:",
        "        table = table.where(SOURCE_FILTERS[name])",
        "    return table",
        "",
        "",
        "def param(name: str):",
        '    """A run parameter of the project: str, int, float, bool or None."""',
        "    return PARAMS.get(name)",
    ]
    for target in plan.targets:
        parts += ["", ""]
        parts += [f"# {line}" for line in _target_header(target)]
        comment = json.dumps(target.description, ensure_ascii=False)
        parts.append(f"@dp.materialized_view(name={json.dumps(target.name)}, comment={comment})")
        parts += [_rule_decorator(rule) for rule in target.quality_rules]
        parts.append(f"def {target.name}():")
        parts.append(textwrap.indent(textwrap.dedent(bodies[target.name]).strip(), "    "))
    return "\n".join(parts) + "\n"


def _sql_constraint(rule: QualityRule) -> str:
    action = {"warn": "", "drop": " ON VIOLATION DROP ROW", "fail": " ON VIOLATION FAIL UPDATE"}
    return f"  CONSTRAINT {rule.name} EXPECT ({rule.condition}){action[rule.on_violation]}"


PLAIN_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def sql_name(*parts: str) -> str:
    """A table's name as SQL takes it, parts quoted only where they need it."""
    return ".".join(p if PLAIN_NAME.match(p) else "`" + p.replace("`", "``") + "`" for p in parts)


def render_sql(plan: EtlPlan, queries: dict[str, str]) -> str:
    sources = sorted(
        {s for t in plan.targets for s in t.sources if "." in s}, key=len, reverse=True
    )
    # A schema.table goes through the source catalog; a catalog.schema.table stays, quoted
    # where it needs it (a catalog like icsd-ace-dev).
    names = {
        s: f"`${{source_catalog}}`.{s}" if s.count(".") == 1 else sql_name(*s.split("."))
        for s in sources
    }
    parts = ["-- Lakeflow declarative pipeline, generated from a confirmed ETL plan."]
    parts += [f"-- {line}" for line in textwrap.wrap(plan.summary, 96)]
    for target in plan.targets:
        query = queries[target.name].strip().rstrip(";")
        for name in sources:
            query = re.sub(
                rf"(?<![\w.`$-]){re.escape(name)}(?![\w-])",
                lambda _, name=name: names[name],
                query,
            )
        parts += ["", *[f"-- {line}" for line in _target_header(target)]]
        head = f"CREATE OR REFRESH MATERIALIZED VIEW {target.name}"
        if target.quality_rules:
            rules = ",\n".join(_sql_constraint(r) for r in target.quality_rules)
            head += f" (\n{rules}\n)"
        comment = target.description.replace("'", "''")
        parts.append(f"{head}\nCOMMENT '{comment}'\nAS\n{query};")
    return "\n".join(parts) + "\n"


def parse_sql(pipeline: str) -> list[dict[str, object]]:
    """The targets of a pipeline written by ``render_sql``: name, query and quality rules."""
    try:
        return cluster_harness.parse_sql(pipeline)
    except ValueError as exc:
        raise CodeError(str(exc)) from exc


DEFAULT_SCHEDULE = "0 0 2 * * ?"
DEFAULT_TIMEZONE = "Asia/Shanghai"


def _pipeline(name: str, library: str, compute: Compute | None) -> dict[str, object]:
    pipeline: dict[str, object] = {
        "name": name,
        "catalog": "${var.catalog}",
        "schema": "${var.schema}",
    }
    if compute is None:
        pipeline["serverless"] = True
    else:
        # Quality rules (expectations) need the Advanced edition on classic compute.
        pipeline["edition"] = "ADVANCED"
        cluster: dict[str, object] = {"label": "default", "num_workers": compute.workers}
        if compute.node_type:
            cluster["node_type_id"] = compute.node_type
        if compute.policy_id:
            cluster |= {"policy_id": compute.policy_id, "apply_policy_default_values": True}
        pipeline["clusters"] = [cluster]
    pipeline["libraries"] = [{"file": {"path": library}}]
    pipeline["configuration"] = {"source_catalog": "${var.source_catalog}"}
    return pipeline


# The job's settings for a run bound to a project (the notebook's widgets, bundle
# variables of the same names).
PROJECT_VARIABLES = {
    "project": "Project id: only the project's tables are written",
    "project_name": "Project name, for messages",
    "table_prefix": "Prefix every table the job writes starts with",
    "adopted": "Existing tables the project adopted (JSON list of names)",
    "params": "Run parameter values (JSON object)",
}


def project_values(project: dict[str, Any]) -> dict[str, str]:
    """The job's settings for a run bound to ``project`` (the run's snapshot)."""
    return {
        "project": project["project_id"],
        "project_name": project["name"],
        "table_prefix": project["table_prefix"],
        "adopted": json.dumps(project["adopted"], ensure_ascii=False),
        "params": json.dumps(project["values"], ensure_ascii=False),
    }


def _job(name: str, bound: bool = False) -> dict[str, object]:
    parameters = {
        "catalog": "${var.catalog}",
        "schema": "${var.schema}",
        "source_catalog": "${var.source_catalog}",
    }
    if bound:
        parameters |= {key: f"${{var.{key}}}" for key in PROJECT_VARIABLES}
        # Marks the tables it writes; the platform passes its own run id when it runs
        # the job, a scheduled run marks them with the job run's id.
        parameters["run_id"] = "{{job.run_id}}"
    return {
        "name": name,
        "max_concurrent_runs": 1,
        "tasks": [
            {
                "task_key": "run",
                "existing_cluster_id": "${var.cluster_id}",
                "notebook_task": {
                    "notebook_path": JOB_NOTEBOOK,
                    "base_parameters": parameters,
                },
                "max_retries": 1,
                "min_retry_interval_millis": 60000,
            }
        ],
    }


def _job_notebook(library: str, settings: dict[str, Any] | None = None) -> str:
    call = f'run_notebook(spark, dbutils, "{library.removeprefix("src/")}")'
    if settings:
        # Each table's write mode and the run parameters' types.
        call = call.removesuffix(")") + f", {settings!r})"
    return "\n".join(
        [
            "# Databricks notebook source",
            "# Generated, do not edit. Runs the transformations in "
            f"{library.removeprefix('src/')} on the job's cluster",
            "# and writes each table that passes its quality rules to the catalog and schema",
            "# given as job parameters; a table that fails fails the run.",
            "from aep_harness import run_notebook",
            "",
            call,
            "",
        ]
    )


def render_bundle(
    name: str,
    plan: EtlPlan,
    source_name: str,
    compute: Compute | None = None,
    job: bool = False,
    project: dict[str, Any] | None = None,
) -> dict[str, str]:
    """The bundle around the pipeline file: a Lakeflow pipeline, or with ``job`` a
    notebook job on an existing cluster (``aep.cluster_harness`` runs the file). A job of
    a run bound to ``project`` (a run's snapshot) writes to the project's environment by
    default and only the project's tables."""
    library = PIPELINE_PY if plan.language == "pyspark" else PIPELINE_SQL
    bound = job and project is not None
    variables: dict[str, object] = {
        "catalog": {
            "description": "Unity Catalog catalog for the target tables",
            "default": "main",
        },
        "schema": {"description": "Schema for the target tables", "default": name},
        "source_catalog": {
            "description": f"Lakehouse Federation catalog for the source {source_name}"
        },
    }
    variables |= {
        "schedule": {
            "description": "When prod refreshes the tables (Quartz cron)",
            "default": DEFAULT_SCHEDULE,
        },
        "timezone": {"description": "Time zone of the schedule", "default": DEFAULT_TIMEZONE},
    }
    schedule = {
        "quartz_cron_expression": "${var.schedule}",
        "timezone_id": "${var.timezone}",
        "pause_status": "UNPAUSED",
    }
    if job:
        variables["cluster_id"] = {"description": "Existing cluster the job runs on"}
        resources: dict[str, object] = {"jobs": {name: _job(name, bound)}}
        # Only prod runs on a schedule; dev runs when the platform runs it.
        prod_jobs: dict[str, object] = {name: {"schedule": schedule}}
    else:
        resources = {"pipelines": {name: _pipeline(name, library, compute)}}
        refresh = {
            "name": f"{name}_refresh",
            "schedule": schedule,
            "tasks": [
                {
                    "task_key": "refresh",
                    "pipeline_task": {"pipeline_id": f"${{resources.pipelines.{name}.id}}"},
                }
            ],
        }
        prod_jobs = {f"{name}_refresh": refresh}
    dev: dict[str, object] = {"schema": f"${{workspace.current_user.short_name}}_{name}"}
    note = ""
    if bound and project is not None:
        values = project_values(project)
        variables |= {
            key: {"description": text, "default": values[key]}
            for key, text in PROJECT_VARIABLES.items()
        }
        env = project["environment"]
        dev = {"catalog": env["catalog"], "schema": env["schema"]}
        if env.get("cluster_id"):
            dev["cluster_id"] = env["cluster_id"]
        note = (
            f"\n这次运行绑定了项目「{project['name']}」（{project['project_id']}）：dev 目标写到"
            f" `{env['catalog']}.{env['schema']}`，只写以 `{project['table_prefix']}` 开头、"
            "属于这个项目的表（表属性 `aep.project`，或登记在项目的“已有的表”里）；"
            "运行参数在 `params` 变量里。\n"
        )
    config = {
        "bundle": {"name": name},
        "variables": variables,
        "resources": resources,
        "targets": {
            "dev": {
                "mode": "development",
                "default": True,
                "variables": dev,
            },
            "prod": {
                "mode": "production",
                "workspace": {
                    "root_path": "/Workspace/Users/${workspace.current_user.userName}"
                    "/.bundle/${bundle.name}/${bundle.target}"
                },
                "resources": {"jobs": prod_jobs},
            },
        },
    }
    rows = [f"| `{t.name}` | {', '.join(t.sources)} | {t.description} |" for t in plan.targets]
    readme = "\n".join(
        [
            f"# {name}",
            "",
            plan.summary,
            "",
            "| 目标表 | 来源 | 说明 |",
            "| --- | --- | --- |",
            *rows,
            "",
            "源表通过 Unity Catalog 的外部目录（Lakehouse Federation）读取，部署时用",
            "`source_catalog` 指定它：",
            "",
            "```bash",
            "databricks bundle deploy -t dev --var source_catalog=<外部目录名>"
            + (" --var cluster_id=<集群 id>" if job else ""),
            f"databricks bundle run -t dev {name}",
            "```",
            "",
            "上线到 prod 目标（`-t prod`，另外用 `--var catalog=... --var schema=...` 指定 prod 的",
            f"catalog 和 schema）后按计划刷新，默认每天 2 点（`{DEFAULT_TIMEZONE}`），用",
            "`--var schedule=<Quartz cron>` 修改。",
            "",
        ]
    )
    settings = run_settings(plan, project)
    if job:
        how = "按方案的写表方式写入目标表" if settings else "整表覆盖写入目标表"
        readme += (
            "\n这个 bundle 部署成一个 Job，跑在已有的集群上：`src/run.py` 用"
            f" `src/aep_harness.py` 执行 `{library}`，\n按质量规则检查后{how}。\n"
        )
    files = {
        "databricks.yml": yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        "README.md": readme + note,
    }
    if job:
        files[JOB_NOTEBOOK] = _job_notebook(library, settings)
        files[JOB_HARNESS] = Path(cluster_harness.__file__).read_text()
    return files


def render(
    name: str,
    plan: EtlPlan,
    code: dict[str, str],
    source_name: str,
    compute: Compute | None = None,
    job: bool = False,
    project: dict[str, Any] | None = None,
) -> Bundle:
    """The bundle for ``code``; ``project`` is the snapshot of the project the run is
    bound to, if any."""
    files = render_bundle(name, plan, source_name, compute, job, project)
    if plan.language == "pyspark":
        files[PIPELINE_PY] = render_pyspark(plan, code, source_filters(plan, project))
    else:
        files[PIPELINE_SQL] = render_sql(plan, code)
    return Bundle(name, files, run_settings(plan, project))


FORBIDDEN_ATTRS = {
    "write", "writeStream", "save", "saveAsTable", "insertInto", "sql", "collect", "toPandas",
}  # fmt: skip
FORBIDDEN_NAMES = {"dbutils", "os", "sys", "subprocess", "open", "eval", "exec", "__import__"}
# What the platform's part of the pipeline file defines; a body must not redefine them,
# and reads parameters and filtered sources only through param() and source().
PLATFORM_NAMES = {
    "source", "param", "spark", "json", "dp", "F", "Window",
    "SOURCE_CATALOG", "SOURCE_FILTERS", "PARAMS",
}  # fmt: skip
HIDDEN_NAMES = {"SOURCE_FILTERS", "PARAMS"}
FORBIDDEN_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|COPY)\b", re.I
)


def _string_arg(call: ast.Call) -> str | None:
    if len(call.args) == 1 and isinstance(call.args[0], ast.Constant):
        value = call.args[0].value
        return value if isinstance(value, str) else None
    return None


def _param_problems(name: str, key: str | None, project: dict[str, Any] | None) -> list[str]:
    if project is None:
        return [f"{name}: 没有绑定项目，不能用 param()"]
    if key is None:
        return [f'{name}: param() 的参数要写成字符串，如 param("update_month")']
    if key not in {p["key"] for p in project["params"]}:
        return [f"{name}: param({key!r}) 不是项目定义的参数"]
    return []


def _check_pyspark_body(
    target: Target, body: str, targets: set[str], project: dict[str, Any] | None = None
) -> list[str]:
    name = target.name
    try:
        tree = ast.parse(
            f"def {name}():\n" + textwrap.indent(textwrap.dedent(body).strip(), "    ")
        )
    except SyntaxError as exc:
        return [f"{name}: Python 语法错误（第 {exc.lineno} 行）：{exc.msg}"]
    problems = []
    if not any(isinstance(n, ast.Return) and n.value is not None for n in ast.walk(tree)):
        problems.append(f"{name}: 函数体最后要 return 一个 DataFrame")
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            problems.append(f"{name}: 不要写 import")
        elif isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRS:
            problems.append(f"{name}: 不允许使用 .{node.attr}")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            problems.append(f"{name}: 不允许使用 {node.id}")
        elif isinstance(node, ast.Name) and node.id in PLATFORM_NAMES:
            if not isinstance(node.ctx, ast.Load):
                problems.append(f"{name}: 不要重新定义平台提供的 {node.id}")
            elif node.id in HIDDEN_NAMES:
                problems.append(
                    f"{name}: 不要直接用 {node.id}，读参数用 param()，读源表用 source()"
                )
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            if node.name in PLATFORM_NAMES:
                problems.append(f"{name}: 不要重新定义平台提供的 {node.name}")
        elif isinstance(node, ast.Global | ast.Nonlocal):
            problems.append(f"{name}: 不要写 global 或 nonlocal")
        elif isinstance(node, ast.Call):
            arg = _string_arg(node)
            func = node.func
            if isinstance(func, ast.Name) and func.id == "source":
                if arg not in target.sources:
                    problems.append(f"{name}: source({arg!r}) 不在方案列出的来源里")
            elif isinstance(func, ast.Name) and func.id == "param":
                problems += _param_problems(name, arg, project)
            elif isinstance(func, ast.Attribute) and func.attr == "table":
                if arg not in targets - {name}:
                    problems.append(
                        f"{name}: spark.read.table({arg!r}) 只能读本管道的其他目标表，"
                        "源表请用 source()"
                    )
    return problems


def _check_sql_query(
    target: Target,
    query: str,
    project: dict[str, Any] | None = None,
    filters: dict[str, str] | None = None,
) -> list[str]:
    q = query.strip().rstrip(";").strip()
    problems = []
    for key in dict.fromkeys(cluster_harness.PARAM.findall(q)):
        if project is None:
            problems.append(f"{target.name}: 没有绑定项目，不能用 ${{param.{key}}}")
        elif key not in {p["key"] for p in project["params"]}:
            problems.append(f"{target.name}: ${{param.{key}}} 不是项目定义的参数")
    for source in target.sources:
        if source in (filters or {}):
            problems.append(f"表 {source} 有读取筛选，SQL 代码暂不支持，请改用 PySpark")
    if not re.match(r"(?is)^(SELECT|WITH)\b", q):
        problems.append(f"{target.name}: SQL 必须是一条 SELECT（可以用 WITH 开头）")
    if ";" in q:
        problems.append(f"{target.name}: 只能有一条语句")
    if match := FORBIDDEN_SQL.search(q):
        problems.append(f"{target.name}: 不允许 {match.group(1).upper()}")
    return problems


def check_code(
    plan: EtlPlan, code: dict[str, str], project: dict[str, Any] | None = None
) -> list[str]:
    """Problems in the model's code for ``plan``; empty when it may be rendered.
    ``project`` is the snapshot of the project the run is bound to: its parameters are
    the ones the code may read."""
    names = {t.name for t in plan.targets}
    problems = [f"缺少目标表 {n} 的代码" for n in sorted(names - set(code))]
    filters = source_filters(plan, project)
    for target in plan.targets:
        body = code.get(target.name)
        if not isinstance(body, str) or not body.strip():
            continue
        if plan.language == "pyspark":
            problems += _check_pyspark_body(target, body, names, project)
        else:
            problems += _check_sql_query(target, body, project, filters)
    return list(dict.fromkeys(problems))


def check_bundle(bundle: Bundle) -> list[str]:
    """Static checks on the rendered bundle: the files parse and fit together."""
    problems = []
    try:
        config = yaml.safe_load(bundle.files["databricks.yml"])
        if set(config["targets"]) != {"dev", "prod"}:
            problems.append("databricks.yml 需要 dev 和 prod 两个目标")
        if "jobs" in config["resources"]:
            job = config["resources"]["jobs"][bundle.name]
            paths = [task["notebook_task"]["notebook_path"] for task in job["tasks"]]
        else:
            pipeline = config["resources"]["pipelines"][bundle.name]
            paths = [lib["file"]["path"] for lib in pipeline["libraries"]]
        for path in paths:
            if path not in bundle.files:
                problems.append(f"databricks.yml 引用的 {path} 不存在")
    except (yaml.YAMLError, KeyError, TypeError) as exc:
        problems.append(f"databricks.yml 结构不对：{exc}")
    if PIPELINE_PY in bundle.files:
        try:
            ast.parse(bundle.files[PIPELINE_PY])
        except SyntaxError as exc:
            problems.append(f"{PIPELINE_PY} 第 {exc.lineno} 行语法错误：{exc.msg}")
    return problems
