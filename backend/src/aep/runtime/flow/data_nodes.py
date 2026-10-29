"""The data-engineering node types of flow apps: natural-language ETL as nodes.

``etl_profile`` reads source tables through a data source's connector,
``etl_plan_check`` checks a model's ETL plan against them, ``databricks_ingest``
copies the plan's source tables into Databricks, ``etl_code_check`` checks the
model's code and packages it as a bundle, and ``databricks_dev_run`` runs the
bundle on the dev target and reads the first rows of its tables. The model calls
in between are ordinary ``llm`` nodes. They reuse what the code app ``etl`` does
(``aep.etl``, ``aep.databricks``) rather than repeat its rules. Settings, outputs
and branches are documented in docs/api/data-nodes.md.
"""

import re
from dataclasses import fields
from typing import Any

from pydantic import ValidationError

from aep import llm
from aep.databricks import DatabricksError, DevTarget, Landing, full_name
from aep.datasources import DataSource
from aep.etl import codegen
from aep.etl.plan import IDENTIFIER, EtlPlan, PlanError, validate_plan
from aep.etl.profile import MAX_TABLES, describe_for_model, known_values, profile_tables
from aep.knowledge.citations import IndexedHit
from aep.runtime.flow import refs
from aep.runtime.flow.nodes import (
    INPUT_ERROR,
    CheckEnv,
    NodeFailure,
    NodeType,
    Outcome,
    Problems,
    Runtime,
    _check_source,
    _text,
)
from aep.tools.mcp import ConnectorUnavailable, ToolError

# Rows per read_rows call while copying a table.
PAGE = 5000
MAX_ROWS = 1_000_000
# How much of each dev run error goes back to the model (as in the code app).
ERROR_CHARS = 1500
MAX_ERRORS = 10
# Columns a preview table in the report shows.
PREVIEW_COLUMNS = 10
SPLIT = re.compile(r"[\s,，、;；]+")
PROFILE_TOOLS = ["list_schemas", "list_tables", "describe_table", "table_stats", "sample_rows"]
INGEST_TOOLS = ["list_tables", "describe_table", "read_rows"]


# Shared settings


def _int(node: dict[str, Any], key: str, p: Problems, label: str, low: int, high: int) -> None:
    value = node.get(key)
    if value is None:
        return
    if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
        p.add((key,), f"{label}要在 {low}–{high} 之间")


def _whole(node: dict[str, Any], key: str, p: Problems, label: str, example: str) -> None:
    """A required setting that must be exactly one reference, like ``example``."""
    value = node.get(key)
    if value is None or value == "":
        p.add((key,), f"{label}不能为空")
    elif not isinstance(value, str) or refs.whole(value) is None:
        p.add((key,), f"{label}必须恰好是一个变量，如 {example}")


def _check_tables(node: dict[str, Any], p: Problems, required: bool) -> None:
    value = node.get("tables")
    if value is None or value == "" or value == []:
        if required:
            p.add(("tables",), "要指定表")
        return
    items = [value] if isinstance(value, str) else value
    if not isinstance(items, list) or not all(isinstance(i, str) for i in items):
        p.add(("tables",), "表要写成文字（逗号或换行分隔）或文字列表")
        return
    for i, item in enumerate(items):
        if refs.find(item):
            continue
        for name in _split(item):
            schema, dot, table = name.partition(".")
            if not (schema and dot and table):
                loc = ("tables",) if isinstance(value, str) else ("tables", i)
                p.add(loc, f"{name} 要写成 schema.table 或 schema.*")


def _split(value: Any) -> list[str]:
    """Table names in a setting's value: text split on commas and spaces, or a list."""
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    names: list[str] = []
    for item in items:
        names += [n for n in SPLIT.split(refs.as_text(item)) if n]
    return names


async def _table_names(
    rt: Runtime, source: DataSource, value: Any, everything: bool
) -> list[tuple[str, str]]:
    """``(schema, table)`` for each name (``schema.*`` is every table of the schema);
    no names means every table of the source when ``everything``."""
    wanted = _split(value)
    if not wanted:
        if not everything:
            raise NodeFailure(INPUT_ERROR, "没有指定要处理的表")
        schemas = (await rt.call(source, "list_schemas", {}))["schemas"]
        wanted = [f"{schema}.*" for schema in schemas]
    found: list[tuple[str, str]] = []
    for name in wanted:
        schema, dot, table = name.partition(".")
        if not (schema and dot and table):
            raise NodeFailure(INPUT_ERROR, f"表 {name} 要写成 schema.table 或 schema.*")
        if table == "*":
            listed = await rt.call(source, "list_tables", {"schema": schema})
            found += [(schema, t) for t in listed["tables"]]
        else:
            found.append((schema, table))
    found = list(dict.fromkeys(found))
    if not found:
        raise NodeFailure(INPUT_ERROR, f"数据源「{source.name}」里没有可读的表")
    if len(found) > MAX_TABLES:
        raise NodeFailure(
            INPUT_ERROR, f"一共 {len(found)} 张表，一次最多 {MAX_TABLES} 张，请指定要哪些表"
        )
    return found


def _mine(state: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """What this node kept for itself on its last run."""
    return (state.get("private") or {}).get(node["id"]) or {}


def _last(state: dict[str, Any], node: dict[str, Any]) -> dict[str, Any]:
    """This node's outputs from its last run."""
    return (state.get("vars") or {}).get(node["id"]) or {}


def _failures(
    state: dict[str, Any], node: dict[str, Any], ok: bool, what: str, problems: list[str]
) -> int:
    """Failures in a row, counting this one; too many fails the node."""
    count = 0 if ok else _mine(state, node).get("failures", 0) + 1
    limit = node.get("max_attempts", 3)
    if count >= limit:
        raise NodeFailure(
            "output_format", f"{what}连续 {count} 次没有通过检查：" + "；".join(problems)
        )
    return count


def _databricks(rt: Runtime, *needs: str) -> DevTarget:
    if rt.databricks is None:
        raise NodeFailure(
            "databricks_error",
            "平台没有配置 Databricks 工作区（AEP_DATABRICKS_HOST、AEP_DATABRICKS_TOKEN）",
        )
    s = rt.databricks.settings
    values = {
        "AEP_DATABRICKS_CLUSTER_ID": s.databricks_cluster_id,
        "AEP_DATABRICKS_SOURCE_CATALOG": s.databricks_source_catalog,
    }
    missing = [name for name in needs if not values[name]]
    if missing:
        raise NodeFailure("databricks_error", "平台没有配置 " + "、".join(missing))
    return rt.databricks


def _cell(value: Any) -> str:
    text = refs.as_text(value)
    return text.replace("|", "\\|").replace("\n", " ") if text else " "


def _code(text: str) -> str:
    """``text`` as Markdown inline code; a table name like `` `icsd-ace-dev`.sales.orders ``
    has backticks of its own, so it gets a double-backtick span."""
    return f"`` {text} ``" if "`" in text else f"`{text}`"


def _table(columns: list[str], rows: list[list[Any]]) -> list[str]:
    lines = ["| " + " | ".join(_cell(c) for c in columns) + " |"]
    lines.append("| " + " | ".join("---" for _ in columns) + " |")
    lines += ["| " + " | ".join(_cell(v) for v in row) + " |" for row in rows]
    return lines


# etl_profile


async def check_profile(node: dict[str, Any], p: Problems, env: CheckEnv) -> None:
    await _check_source(
        node.get("datasource"), ("datasource",), p, env, PROFILE_TOOLS, ("datasource",)
    )
    _check_tables(node, p, required=False)
    _int(node, "sample_rows", p, "样本行数", 1, 20)


async def run_profile(node: dict[str, Any], state: dict[str, Any], rt: Runtime) -> Outcome:
    get = rt.lookup(state)
    source = await rt.source(refs.resolve(node["datasource"], get))
    names = await _table_names(rt, source, refs.resolve(node.get("tables"), get), True)
    if rt.tools is None:
        raise NodeFailure("tool_error", "平台没有配置工具调用")
    try:
        tables = await profile_tables(
            rt.tools, source, [f"{s}.{t}" for s, t in names], node.get("sample_rows", 5)
        )
    except (ConnectorUnavailable, ToolError, ValueError, KeyError) as exc:
        raise NodeFailure("tool_error", f"探查数据源「{source.name}」失败：{exc}") from exc
    listed = [t["name"] for t in tables]
    summary = f"已探查 {source.name} 的 {len(tables)} 张表：" + "、".join(listed)
    return Outcome(
        {
            "source_id": source.datasource_id,
            "source_name": source.name,
            "tables": tables,
            "names": listed,
            "description": describe_for_model(tables),
            "summary": summary,
        },
        messages=[{"role": "assistant", "content": summary}],
    )


# etl_plan_check


async def check_plan_check(node: dict[str, Any], p: Problems, env: CheckEnv) -> None:
    _text(node, "reply", p, "模型的回答", required=True)
    _whole(node, "profile", p, "探查结果", "{{profile.tables}}")
    hits = node.get("hits", [])
    if not isinstance(hits, list) or len(hits) > 5:
        p.add(("hits",), "资料最多 5 项")
    else:
        for i, ref in enumerate(hits):
            if not isinstance(ref, str) or refs.whole(ref) is None:
                p.add(("hits", i), "每项必须恰好是一个变量，如 {{glossary.hits}}")
    _text(node, "language", p, "代码语言", max_length=200)
    _int(node, "max_attempts", p, "连续不过的次数上限", 1, 5)


def _hits(node: dict[str, Any], get: refs.Lookup) -> list[IndexedHit]:
    names = {f.name for f in fields(IndexedHit)}
    found: dict[int, IndexedHit] = {}
    for ref in node.get("hits") or []:
        value = refs.resolve(ref, get)
        for hit in value if isinstance(value, list) else []:
            if not isinstance(hit, dict) or not isinstance(hit.get("index"), int):
                continue
            if hit["index"] not in found:
                found[hit["index"]] = IndexedHit(**{k: v for k, v in hit.items() if k in names})
    return [found[i] for i in sorted(found)]


VIOLATION = {"warn": "只记录", "drop": "丢弃这一行", "fail": "整次运行失败"}


def plan_message(plan: EtlPlan) -> str:
    """The plan as a person reviews it (Markdown)."""
    language = "PySpark" if plan.language == "pyspark" else "SQL"
    lines = [f"**方案**：{plan.summary}", "", f"代码语言：{language}"]
    for n, t in enumerate(plan.targets, 1):
        load = "全量覆盖"
        if t.load.mode == "incremental":
            load = f"增量（增量键 `{t.load.key}`）"
        lines += ["", f"### 目标表 {n}：`{t.name}`", ""]
        if t.description:
            lines += [t.description, ""]
        lines.append(f"- 加载方式：{load}")
        lines.append("- 来源：" + "、".join(f"`{s}`" for s in t.sources))
        lines.append(f"- 做法：{t.logic}")
        if t.primary_key:
            lines.append("- 主键：" + "、".join(f"`{k}`" for k in t.primary_key))
        lines.append("")
        rows = [[f"`{c.name}`", f"`{c.source}`", c.description] for c in t.columns]
        lines += _table(["列", "计算方式", "说明"], rows)
        if t.quality_rules:
            lines += ["", "数据质量规则："]
            lines += [
                f"- `{r.name}`：`{r.condition}`（不满足时{VIOLATION[r.on_violation]}）"
                for r in t.quality_rules
            ]
    if plan.assumptions:
        lines += ["", "### 需要确认的假设", ""]
        lines += [f"- {a}" for a in plan.assumptions]
    if plan.references:
        lines += ["", "### 依据的资料", ""]
        for hit in plan.references:
            heading = f" {hit.heading}" if hit.heading else ""
            lines.append(
                f"- [{hit.index}]《{hit.document_name}》{heading}（知识库「{hit.kb_name}」）"
            )
    return "\n".join(lines)


async def run_plan_check(node: dict[str, Any], state: dict[str, Any], rt: Runtime) -> Outcome:
    get = rt.lookup(state)
    profile = refs.resolve(node["profile"], get)
    if not isinstance(profile, list) or not all(isinstance(t, dict) for t in profile):
        raise NodeFailure("output_format", "探查结果要引用探查数据节点的 tables")
    language = refs.as_text(refs.resolve(node.get("language") or "", get)).strip()
    sources = [t["name"] for t in profile]
    # The first time the model sees what the data really holds before its
    # assumptions are taken at their word (as in the code app).
    trust = (state.get("branch") or {}).get(node["id"]) == "retry"
    raw: Any = None
    plan: EtlPlan | None = None
    try:
        raw = llm.parse_json(refs.render(node["reply"], get))
        plan = validate_plan(raw, sources, known_values(profile), trust, _hits(node, get))
        problems: list[str] = []
    except PlanError as exc:
        problems = exc.problems
    except ValueError as exc:
        problems = [f"回复不是要求的 JSON：{exc}"]
    failures = _failures(state, node, plan is not None, "方案", problems)
    if plan is None:
        text = "方案没有通过平台校验，带着问题让模型重写：\n" + "\n".join(
            f"- {p}" for p in problems
        )
        outputs = {
            "ok": False,
            "plan": raw,
            "problems": "\n".join(problems),
            "message": text,
            "sources": [],
            "targets": [],
        }
        return Outcome(
            outputs,
            "retry",
            messages=[{"role": "assistant", "content": text}],
            private={"failures": failures},
        )
    if language in ("pyspark", "sql"):
        plan.language = language  # type: ignore[assignment]
    message = plan_message(plan)
    used = sorted({s for t in plan.targets for s in t.sources if s in sources})
    outputs = {
        "ok": True,
        "plan": plan.model_dump(),
        "problems": "",
        "message": message,
        "sources": used,
        "targets": [t.name for t in plan.targets],
    }
    return Outcome(
        outputs,
        "ok",
        messages=[{"role": "assistant", "content": message}],
        private={"failures": 0},
    )


# databricks_ingest


async def check_ingest(node: dict[str, Any], p: Problems, env: CheckEnv) -> None:
    await _check_source(
        node.get("datasource"), ("datasource",), p, env, INGEST_TOOLS, ("datasource",)
    )
    _check_tables(node, p, required=True)
    _int(node, "max_rows", p, "每张表最多行数", 1, MAX_ROWS)


async def _read_all(rt: Runtime, source: DataSource, schema: str, table: str, most: int) -> Landing:
    args = {"schema": schema, "table": table}
    described = await rt.call(source, "describe_table", args)
    rows: list[dict[str, Any]] = []
    offset: int | None = 0
    while offset is not None:
        page = await rt.call(source, "read_rows", {**args, "offset": offset, "limit": PAGE})
        rows += page.get("rows") or []
        if len(rows) > most:
            raise NodeFailure(
                INPUT_ERROR,
                f"{schema}.{table} 超过 {most} 行，没有抽取任何表；"
                "请调大每张表最多行数（max_rows）或缩小表范围",
            )
        offset = page.get("next_offset") if page.get("rows") else None
    columns = [(c["name"], c["data_type"]) for c in described["columns"]]
    return Landing(schema, table, columns, rows)


async def run_ingest(node: dict[str, Any], state: dict[str, Any], rt: Runtime) -> Outcome:
    databricks = _databricks(rt, "AEP_DATABRICKS_CLUSTER_ID", "AEP_DATABRICKS_SOURCE_CATALOG")
    get = rt.lookup(state)
    source = await rt.source(refs.resolve(node["datasource"], get))
    names = await _table_names(rt, source, refs.resolve(node.get("tables"), get), False)
    most = node.get("max_rows", 200000)
    # Every table is read before anything is written, so a table over the limit
    # leaves Databricks as it was.
    landed = [await _read_all(rt, source, schema, table, most) for schema, table in names]
    try:
        loaded, warnings = await databricks.ingest(
            landed, source.datasource_id, source.name, state.get("run_id") or ""
        )
    except DatabricksError as exc:
        raise NodeFailure("databricks_error", f"抽取到 Databricks 失败：{exc}") from exc
    total = sum(t["rows"] or 0 for t in loaded)
    summary = f"已把 {len(loaded)} 张表共 {total} 行抽取到 Databricks：" + "、".join(
        f"{_code(t['target'])}（{t['rows']} 行）" for t in loaded
    )
    text = "\n".join([summary, *(f"注意：{w}" for w in warnings)])
    return Outcome(
        {
            "catalog": databricks.settings.databricks_source_catalog,
            "tables": loaded,
            "summary": summary,
        },
        messages=[{"role": "assistant", "content": text}],
    )


# etl_code_check


async def check_code_check(node: dict[str, Any], p: Problems, env: CheckEnv) -> None:
    _text(node, "reply", p, "模型的回答", required=True)
    _whole(node, "plan", p, "方案", "{{check_plan.plan}}")
    _text(node, "source_name", p, "数据源名称", max_length=200)
    _text(node, "name", p, "bundle 名", max_length=200)
    _int(node, "max_attempts", p, "连续不过的次数上限", 1, 5)


async def run_code_check(node: dict[str, Any], state: dict[str, Any], rt: Runtime) -> Outcome:
    get = rt.lookup(state)
    try:
        plan = EtlPlan.model_validate(refs.resolve(node["plan"], get))
    except ValidationError as exc:
        raise NodeFailure("output_format", "方案要引用校验方案节点通过时的 plan") from exc
    try:
        data = llm.parse_json(refs.render(node["reply"], get))
        code = data.get("targets") if isinstance(data, dict) else None
        if not isinstance(code, dict):
            raise ValueError("要有 targets 对象")
        problems = codegen.check_code(plan, code)
    except ValueError as exc:
        code, problems = _last(state, node).get("code") or {}, [f"回复不是要求的 JSON：{exc}"]
    bundle = None
    if not problems:
        name = refs.as_text(refs.resolve(node.get("name") or "", get)).strip()
        if not IDENTIFIER.match(name):
            # As in the code app: named after its first target, so later runs for the
            # same table make new versions of the same bundle.
            name = plan.targets[0].name
        source_name = refs.as_text(refs.resolve(node.get("source_name") or "", get)).strip()
        s = rt.databricks.settings if rt.databricks else rt.settings
        built = codegen.render(
            name,
            plan,
            code,
            source_name or "数据源",
            codegen.compute_for(s),
            s.databricks_deploy_mode == "job",
        )
        problems = codegen.check_bundle(built)
        if not problems:
            bundle = {"name": name, "language": plan.language, "files": built.files}
    failures = _failures(state, node, bundle is not None, "代码", problems)
    if bundle is None:
        summary = "代码没有通过检查，带着问题让模型重写：\n" + "\n".join(f"- {p}" for p in problems)
    else:
        summary = f"已生成 Databricks bundle {bundle['name']}：" + "、".join(
            sorted(bundle["files"])
        )
    outputs = {
        "ok": bundle is not None,
        "code": code,
        "problems": "\n".join(problems),
        "bundle": bundle,
        "summary": summary,
    }
    return Outcome(
        outputs,
        "ok" if bundle is not None else "retry",
        messages=[{"role": "assistant", "content": summary}],
        private={"failures": failures},
    )


# databricks_dev_run


async def check_dev_run(node: dict[str, Any], p: Problems, env: CheckEnv) -> None:
    _whole(node, "bundle", p, "bundle", "{{check_code.bundle}}")
    _int(node, "max_runs", p, "最多运行次数", 1, 3)
    _int(node, "preview_rows", p, "预览行数", 0, 100)


HOW = {
    "job": "部署成 notebook Job，在已有集群 {cluster} 上运行，结果写入表",
    "pipeline": "部署成 Lakeflow 管道运行，结果写入表",
    "cluster": "只在集群 {cluster} 上执行转换逻辑，不部署、不写表",
}


def dev_report(
    dev: Any, attempt: int, branch: str, tables: list[dict[str, Any]], errors: list[str]
) -> str:
    """The dev run as a person reads it (Markdown)."""
    head = {
        "passed": "## dev 运行通过",
        "retry": "## dev 运行失败，带着报错回去重写代码",
        "failed": "## dev 运行失败",
    }[branch]
    lines = [head, ""]
    lines.append("- 运行方式：" + HOW[dev.mode].format(cluster=dev.cluster_id))
    lines.append(f"- 第 {attempt} 次运行")
    if dev.url:
        lines.append(f"- Databricks 上的这次运行：[打开]({dev.url})")
    if tables:
        written = "得到行数" if dev.mode == "cluster" else "写入行数"
        rows = []
        for t in tables:
            rules = "；".join(
                f"{e.get('name')} 通过 {e.get('passed')} / 不通过 {e.get('failed')}"
                for e in t["expectations"]
            )
            count = "未知" if t["rows"] is None else t["rows"]
            rows.append([_code(t["target"]), count, t["dropped"], rules or "无"])
        lines += ["", "### 结果表", ""]
        lines += _table(["表", written, "质量规则丢弃", "质量规则"], rows)
    for p in dev.preview:
        columns = p["columns"]
        shown = columns[:PREVIEW_COLUMNS]
        lines += ["", f"### 数据预览：{_code(p['target'])}（前 {len(p['rows'])} 行）", ""]
        lines += _table(shown, [[row.get(c) for c in shown] for row in p["rows"]])
        if len(columns) > len(shown):
            lines += ["", f"共 {len(columns)} 列，只显示前 {PREVIEW_COLUMNS} 列。"]
    if errors:
        lines += ["", "### 报错", ""]
        lines += [f"- {e[:500]}" for e in errors]
    if dev.warnings:
        lines += ["", "### 注意", ""]
        lines += [f"- {w}" for w in dev.warnings]
    return "\n".join(lines)


async def run_dev_run(node: dict[str, Any], state: dict[str, Any], rt: Runtime) -> Outcome:
    databricks = _databricks(rt)
    bundle = refs.resolve(node["bundle"], rt.lookup(state))
    if (
        not isinstance(bundle, dict)
        or not isinstance(bundle.get("name"), str)
        or not isinstance(bundle.get("files"), dict)
    ):
        raise NodeFailure(INPUT_ERROR, "bundle 要引用检查并打包节点的 bundle（代码通过检查后才有）")
    attempt = (state.get("visits") or {}).get(node["id"], 0) + 1
    try:
        dev = await databricks.run(
            codegen.Bundle(bundle["name"], bundle["files"]), node.get("preview_rows", 20)
        )
    except DatabricksError as exc:
        raise NodeFailure("databricks_error", str(exc)) from exc
    passed = dev.status == "passed"
    if passed:
        branch = "passed"
    elif dev.fixable and attempt < node.get("max_runs", 3):
        branch = "retry"
    else:
        branch = "failed"
    where = (dev.catalog, dev.schema) if dev.catalog and dev.schema else None
    tables = [
        {
            "name": t.name,
            "target": full_name(*where, t.name) if where else t.name,
            "rows": t.rows,
            "dropped": t.dropped,
            "expectations": t.expectations,
        }
        for t in dev.tables
    ]
    errors = [] if passed else [e[:ERROR_CHARS] for e in dev.errors[:MAX_ERRORS]]
    report = dev_report(dev, attempt, branch, tables, errors)
    run = {k: v for k, v in dev.as_dict().items() if k != "preview"} | {"attempt": attempt}
    outputs = {
        "status": dev.status,
        "run": run,
        "url": dev.url,
        "tables": tables,
        "preview": dev.preview,
        "errors": "\n".join(errors),
        "report": report,
    }
    return Outcome(outputs, branch, messages=[{"role": "assistant", "content": report}])


def _fixed(*names: str) -> Any:
    return lambda node: list(names)


def _none(node: dict[str, Any]) -> list[str] | None:
    return None


TYPES: dict[str, NodeType] = {
    t.type: t
    for t in (
        NodeType(
            "etl_profile",
            "探查数据",
            "通过数据源的只读连接器读取源表的结构、统计、取值分布和样本行",
            "data",
            "tool",
            check_profile,
            run_profile,
            _none,
            _fixed("source_id", "source_name", "tables", "names", "description", "summary"),
            outputs=(
                ("source_id", "数据源 id"),
                ("source_name", "数据源名称"),
                ("tables", "每张表的探查结果"),
                ("names", "表名列表"),
                ("description", "给模型看的源表说明"),
                ("summary", "一句话总结"),
            ),
            defaults={"datasource": "", "tables": "", "sample_rows": 5},
            error_handling=True,
        ),
        NodeType(
            "etl_plan_check",
            "校验方案",
            "解析模型写的 ETL 方案，按探查结果校验表、列和取值，通过后整理成给人确认的方案",
            "data",
            "code",
            check_plan_check,
            run_plan_check,
            _fixed("ok", "retry"),
            _fixed("ok", "plan", "problems", "message", "sources", "targets"),
            outputs=(
                ("ok", "是否通过"),
                ("plan", "校验后的方案（JSON）"),
                ("problems", "没通过的原因"),
                ("message", "给人看的方案"),
                ("sources", "方案用到的源表"),
                ("targets", "目标表名"),
            ),
            defaults={"reply": "", "profile": "", "hits": [], "language": "", "max_attempts": 3},
            error_handling=True,
            branch_labels=lambda node: {"ok": "通过", "retry": "重写"},
        ),
        NodeType(
            "databricks_ingest",
            "抽取到湖仓",
            "把源表的数据整表复制到 Databricks（源表 catalog 下同名的 schema 和表）",
            "data",
            "tool",
            check_ingest,
            run_ingest,
            _none,
            _fixed("catalog", "tables", "summary"),
            outputs=(
                ("catalog", "写入的 catalog"),
                ("tables", "每张表的来源、目标、行数和列数"),
                ("summary", "一句话总结"),
            ),
            defaults={"datasource": "", "tables": "", "max_rows": 200000},
            error_handling=True,
        ),
        NodeType(
            "etl_code_check",
            "检查并打包",
            "静态检查模型写的转换代码，通过后打包成 Databricks bundle",
            "data",
            "code",
            check_code_check,
            run_code_check,
            _fixed("ok", "retry"),
            _fixed("ok", "code", "problems", "bundle", "summary"),
            outputs=(
                ("ok", "是否通过"),
                ("code", "每张目标表的代码"),
                ("problems", "没通过的原因"),
                ("bundle", "Databricks bundle 文件"),
                ("summary", "一句话总结"),
            ),
            defaults={
                "reply": "",
                "plan": "",
                "source_name": "",
                "name": "",
                "max_attempts": 3,
            },
            error_handling=True,
            branch_labels=lambda node: {"ok": "通过", "retry": "重写"},
        ),
        NodeType(
            "databricks_dev_run",
            "dev 运行",
            "把 bundle 部署到 Databricks dev 目标运行，收集行数和质量规则结果，预览结果数据",
            "data",
            "tool",
            check_dev_run,
            run_dev_run,
            _fixed("passed", "retry", "failed"),
            _fixed("status", "run", "url", "tables", "preview", "errors", "report"),
            outputs=(
                ("status", "passed 或 failed"),
                ("run", "这次运行的详情"),
                ("url", "Databricks 上这次运行的链接"),
                ("tables", "每张结果表的行数和质量规则结果"),
                ("preview", "每张结果表的前几行"),
                ("errors", "失败时的报错"),
                ("report", "运行报告（Markdown）"),
            ),
            defaults={"bundle": "", "max_runs": 3, "preview_rows": 20},
            error_handling=True,
            branch_labels=lambda node: {
                "passed": "通过",
                "retry": "带着报错重写",
                "failed": "失败",
            },
        ),
    )
}
