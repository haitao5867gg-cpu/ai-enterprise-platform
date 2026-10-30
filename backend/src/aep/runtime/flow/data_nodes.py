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
from aep.datasources import DATABASE_KINDS, DataSource
from aep.etl import codegen
from aep.etl.plan import (
    BASIS_NAMES,
    IDENTIFIER,
    Confirmed,
    EtlPlan,
    EtlPlanV2,
    PlanError,
    Target,
    answer_output,
    bare,
    uncited,
    validate_plan,
    validate_plan_v2,
)
from aep.etl.profile import (
    MAX_TABLES,
    describe_for_model,
    known_values,
    profile_tables,
    seen_values,
)
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


def _etl_source(source: DataSource) -> DataSource:
    """The source a run chose, if ETL can read it: a tool service cannot."""
    if source.kind not in DATABASE_KINDS:
        raise NodeFailure(
            INPUT_ERROR,
            f"「{source.name}」是工具服务，不能用来探查或抽取数据，请选数据库或上传文件的数据源",
        )
    return source


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


def _rewrite_said(problems: str, not_json: bool, failures: int, node: dict[str, Any]) -> str:
    """What a check says when the model rewrites its reply; ``failures`` in a row so far."""
    what = "模型没按要求的 JSON 格式回答" if not_json else problems
    return f"{what}，让模型重写（第 {failures + 1}/{node.get('max_attempts', 3)} 次）"


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
        node.get("datasource"), ("datasource",), p, env, PROFILE_TOOLS, ("datasource",), etl=True
    )
    _check_tables(node, p, required=False)
    _int(node, "sample_rows", p, "样本行数", 1, 20)


async def run_profile(node: dict[str, Any], state: dict[str, Any], rt: Runtime) -> Outcome:
    get = rt.lookup(state)
    source = _etl_source(await rt.source(refs.resolve(node["datasource"], get)))
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
    version = node.get("format")
    if version is not None and (
        not isinstance(version, int) or isinstance(version, bool) or version not in (1, 2)
    ):
        p.add(("format",), "方案版本 format 只能是 1 或 2")
    answered = node.get("answered")
    if answered is not None and answered != "":
        if version != 2:
            p.add(("answered",), "只有方案版本 format 为 2 时才能写已答的题")
        elif not isinstance(answered, str) or refs.whole(answered) is None:
            p.add(("answered",), "已答的题必须恰好是一个变量，如 {{review.record}}")


def _answered(
    node: dict[str, Any], state: dict[str, Any], get: refs.Lookup
) -> dict[str, Confirmed]:
    """The answers people gave to this node's plans, by ``bare`` question text: what the
    node kept from earlier runs, then the answers in ``answered`` (a human node's record,
    which holds its latest round only). A newer answer to a question replaces the older."""
    memory: dict[str, Confirmed] = dict(_mine(state, node).get("answered") or {})
    record = refs.resolve(node["answered"], get) if node.get("answered") else None
    given = record.get("answers") if isinstance(record, dict) else None
    if not isinstance(record, dict) or not isinstance(given, list):
        return memory
    actor = record.get("actor")
    who = actor if isinstance(actor, str) and actor.strip() else None
    for item in given:
        label = item.get("label") if isinstance(item, dict) else None
        value = item.get("value_label") if isinstance(item, dict) else None
        if isinstance(label, str) and isinstance(value, str) and bare(label):
            memory[bare(label)] = {"label": label, "value_label": value, "actor": who}
    return memory


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


def _target_lines(n: int, t: Target) -> list[str]:
    """Target table ``n`` of a plan as a person reviews it (Markdown)."""
    load = "全量覆盖"
    if t.load.mode == "incremental":
        load = f"增量（增量键 `{t.load.key}`）"
    lines = ["", f"### 目标表 {n}：`{t.name}`", ""]
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
    return lines


def _reference_lines(references: list[IndexedHit]) -> list[str]:
    if not references:
        return []
    lines = ["", "### 依据的资料", ""]
    for hit in references:
        heading = f" {hit.heading}" if hit.heading else ""
        lines.append(f"- [{hit.index}]《{hit.document_name}》{heading}（知识库「{hit.kb_name}」）")
    return lines


def _language(plan: EtlPlan) -> str:
    return "PySpark" if plan.language == "pyspark" else "SQL"


def plan_message(plan: EtlPlan) -> str:
    """The plan as a person reviews it (Markdown)."""
    lines = [f"**方案**：{plan.summary}", "", f"代码语言：{_language(plan)}"]
    for n, t in enumerate(plan.targets, 1):
        lines += _target_lines(n, t)
    if plan.assumptions:
        lines += ["", "### 需要确认的假设", ""]
        lines += [f"- {a}" for a in plan.assumptions]
    lines += _reference_lines(plan.references)
    return "\n".join(lines)


def plan_message_v2(plan: EtlPlanV2, answer: dict[str, Any] | None) -> str:
    """A version 2 plan as a person reviews it (Markdown): the business part first,
    then the technical one."""
    brief = plan.brief
    lines = ["## 业务说明"]
    if brief is not None:
        lines += ["", f"**目标**：{brief.goal}"]
        if brief.grain and brief.grain.strip():
            lines += ["", f"**粒度**：{brief.grain}"]
        if brief.metrics:
            rows = [
                [m.id, m.name, m.unit or "", m.definition, _code(m.column) if m.column else ""]
                for m in brief.metrics
            ]
            lines += ["", "### 指标", ""] + _table(["编号", "指标", "单位", "口径", "结果列"], rows)
        if brief.dimensions:
            rows = [
                [
                    d.id,
                    d.name,
                    d.definition,
                    _code(d.column) if d.column else "",
                    "、".join(d.examples or []),
                ]
                for d in brief.dimensions
            ]
            lines += ["", "### 维度", ""] + _table(["编号", "维度", "说明", "结果列", "例子"], rows)
        if brief.filters:
            lines += ["", "### 筛选", ""] + [f"- {f.text}" for f in brief.filters]
        if brief.time_range is not None:
            column = brief.time_range.column
            by = f"（按 {_code(column)}）" if column else ""
            lines += ["", "### 时间范围", "", brief.time_range.text + by]
    if plan.questions:
        lines += ["", "### 要回答的问题", ""]
        for n, q in enumerate(plan.questions, 1):
            names = {o.value: o.label for o in q.options or []}
            lines.append(f"{n}. **{q.text}**（建议：{names.get(q.suggested, q.suggested)}）")
            lines.append("   - 选项：" + "、".join(o.label or "" for o in q.options or []))
            if q.why and q.why.strip():
                lines.append(f"   - 为什么问：{q.why}")
    if plan.rules:
        lines += ["", "### 已按口径处理的规则", ""]
        lines += [f"- {r.text}（{BASIS_NAMES.get(r.basis, r.basis)}）" for r in plan.rules]
    if answer is not None:
        lines += ["", "### 最后给你的答案", "", answer["text"]]
    lines += ["", "## 技术方案", "", f"**方案**：{plan.summary}", ""]
    lines.append(f"代码语言：{_language(plan)}")
    for n, t in enumerate(plan.targets, 1):
        lines += _target_lines(n, t)
    lines += _reference_lines(plan.references)
    return "\n".join(lines)


def human_fields(plan: EtlPlanV2) -> list[dict[str, Any]]:
    """The plan's questions as a human node's questions (flow-apps.md, 确认模式的题
    ``fields``): ``why`` and ``affects`` only when there is something in them."""
    fields = []
    for q in plan.questions:
        field: dict[str, Any] = {
            "key": q.id,
            "label": q.text,
            "type": "select",
            "required": True,
            "choices": [{"value": o.value, "label": o.label} for o in q.options or []],
            "suggested": q.suggested,
        }
        if q.why and q.why.strip():
            field["why"] = q.why
        if q.affects:
            field["affects"] = list(q.affects)
        fields.append(field)
    return fields


def _plan_summary(plan: EtlPlan) -> str:
    """The plan in a few words, as the run says it."""
    if isinstance(plan, EtlPlanV2):
        metrics = len(plan.brief.metrics) if plan.brief is not None else 0
        summary = f"{len(plan.targets)} 张目标表、{metrics} 个指标"
        if plan.questions:
            return summary + f"、{len(plan.questions)} 个问题待回答"
        return summary + "，没有要回答的问题"
    summary = f"{len(plan.targets)} 张目标表"
    if plan.assumptions:
        summary += f"、{len(plan.assumptions)} 条假设待确认"
    return summary


def _no_plan() -> dict[str, Any]:
    """The outputs a failed check (or version 1, for those of version 2) leaves empty."""
    return {
        "references": [],
        "summary": "",
        "brief": None,
        "questions": [],
        "answer": None,
        "uncited": [],
        "cleaned": [],
    }


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
    v2 = node.get("format") == 2
    # Version 2 remembers every answer people gave, across rounds: the human node's
    # record holds only its latest one.
    memory = _answered(node, state, get) if v2 else {}
    raw: Any = None
    plan: EtlPlan | None = None
    cleaned: list[str] = []
    not_json = False
    try:
        raw = llm.parse_json(refs.render(node["reply"], get))
        known, hits = known_values(profile), _hits(node, get)
        if v2:
            plan, cleaned = validate_plan_v2(
                raw, sources, known, seen_values(profile), trust, hits, memory
            )
        else:
            plan = validate_plan(raw, sources, known, trust, hits)
        problems: list[str] = []
    except PlanError as exc:
        problems = exc.problems
    except ValueError as exc:
        problems = [f"回复不是要求的 JSON：{exc}"]
        not_json = True
    failures = _failures(state, node, plan is not None, "方案", problems)
    private: dict[str, Any] = {"failures": failures}
    if v2:
        private["answered"] = memory
    if plan is None:
        text = "方案没有通过平台校验，带着问题让模型重写：\n" + "\n".join(
            f"- {p}" for p in problems
        )
        said = _rewrite_said(f"方案有 {len(problems)} 处没通过校验", not_json, failures, node)
        outputs = {
            "ok": False,
            "plan": raw,
            "problems": "\n".join(problems),
            "message": text,
            "sources": [],
            "targets": [],
        } | _no_plan()
        return Outcome(
            outputs,
            "retry",
            messages=[{"role": "assistant", "content": said}],
            private=private,
        )
    if language in ("pyspark", "sql"):
        plan.language = language  # type: ignore[assignment]
    used = sorted({s for t in plan.targets for s in t.sources if s in sources})
    summary = _plan_summary(plan)
    dumped = plan.model_dump()
    outputs = {
        "ok": True,
        "plan": dumped,
        "problems": "",
        "message": plan_message(plan),
        "sources": used,
        "targets": [t.name for t in plan.targets],
    } | _no_plan()
    outputs |= {"references": dumped["references"], "summary": summary}
    if isinstance(plan, EtlPlanV2):
        answer = answer_output(plan)
        outputs |= {
            "message": plan_message_v2(plan, answer),
            "brief": dumped["brief"],
            "questions": human_fields(plan),
            "answer": answer,
            "uncited": uncited(plan),
            "cleaned": cleaned,
        }
    # The whole plan is in ``message``, for the person who reviews it.
    return Outcome(
        outputs,
        "ok",
        messages=[{"role": "assistant", "content": f"方案整理好了：{summary}"}],
        private=private,
    )


# databricks_ingest


async def check_ingest(node: dict[str, Any], p: Problems, env: CheckEnv) -> None:
    await _check_source(
        node.get("datasource"), ("datasource",), p, env, INGEST_TOOLS, ("datasource",), etl=True
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
    source = _etl_source(await rt.source(refs.resolve(node["datasource"], get)))
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
    not_json = False
    try:
        data = llm.parse_json(refs.render(node["reply"], get))
        code = data.get("targets") if isinstance(data, dict) else None
        if not isinstance(code, dict):
            raise ValueError("要有 targets 对象")
        problems = codegen.check_code(plan, code)
    except ValueError as exc:
        code, problems = _last(state, node).get("code") or {}, [f"回复不是要求的 JSON：{exc}"]
        not_json = True
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
        said = _rewrite_said(f"代码有 {len(problems)} 处没通过检查", not_json, failures, node)
    else:
        summary = f"已生成 Databricks bundle {bundle['name']}：" + "、".join(
            sorted(bundle["files"])
        )
        said = f"代码检查通过，打包成 bundle {bundle['name']}（{len(bundle['files'])} 个文件）"
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
        messages=[{"role": "assistant", "content": said}],
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


def dev_said(
    dev: Any,
    attempt: int,
    branch: str,
    tables: list[dict[str, Any]],
    errors: list[str],
    max_runs: int,
) -> str:
    """The dev run in one sentence, for the run's messages (the report has the rest)."""
    if branch == "passed":
        if not tables:
            return "dev 运行通过"
        verb = "得到" if dev.mode == "cluster" else "写出"
        said = f"dev 运行通过：{verb} {len(tables)} 张表"
        if all(t["rows"] is not None for t in tables):
            said += f"共 {sum(t['rows'] for t in tables)} 行"
        dropped = sum(t["dropped"] or 0 for t in tables)
        if dropped > 0:
            said += f"，质量规则丢弃 {dropped} 行"
        return said
    head = f"dev 运行失败（{len(errors)} 条报错）" if errors else "dev 运行失败"
    if branch == "retry":
        return f"{head}，让模型改代码（第 {attempt + 1}/{max_runs} 次运行）"
    return f"{head}，不再重试"


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
    said = dev_said(dev, attempt, branch, tables, errors, node.get("max_runs", 3))
    return Outcome(outputs, branch, messages=[{"role": "assistant", "content": said}])


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
            _fixed(
                "ok",
                "plan",
                "problems",
                "message",
                "sources",
                "targets",
                "references",
                "summary",
                "brief",
                "questions",
                "answer",
                "uncited",
                "cleaned",
            ),
            outputs=(
                ("ok", "是否通过"),
                ("plan", "校验后的方案（JSON）"),
                ("problems", "没通过的原因"),
                ("message", "给人看的方案"),
                ("sources", "方案用到的源表"),
                ("targets", "目标表名"),
                ("references", "方案引用了的资料"),
                ("summary", "一句话总结"),
                ("brief", "业务说明（第 2 版方案）"),
                ("questions", "要人回答的题，可以整段给人工节点的 fields（第 2 版方案）"),
                ("answer", "答案设计（第 2 版方案）"),
                ("uncited", "没找到口径依据的指标、维度和筛选（第 2 版方案）"),
                ("cleaned", "平台自动整理过的地方（第 2 版方案）"),
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
