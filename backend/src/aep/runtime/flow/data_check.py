"""The node ``data_check``: checks a run's data through a Databricks data source.

It reads through the source's read-only connector, like profiling does, never with the
identity the platform writes with. The checks (totals, reconciling with the sources,
repeated keys, rows that must not exist, and a row-by-row comparison with the old
system) are described and judged in ``aep.etl.checks``; settings, outputs and branches
are documented in docs/api/data-nodes.md, "data_check 数据检查".
"""

import math
from typing import Any

from aep.datasources import tool_seconds
from aep.etl import checks
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
)
from aep.tools.mcp import ConnectorUnavailable

TOOLS = ["aggregate", "duplicates", "compare", "sample_rows"]
MAX_INCLUDE = 5
GOLDEN_KEYS = {"expected", "table", "keys", "tolerance", "severity"}
NOT_DATABRICKS = "数据检查要用 Databricks 数据源"
SKIPPED = "前一步没有通过，没有做数据检查"


# Checking settings


def _with_refs(value: Any) -> bool:
    return any(refs.find(text) for _, text in refs.strings(value))


async def check_data_check(node: dict[str, Any], p: Problems, env: CheckEnv) -> None:
    value = node.get("datasource")
    source = None
    if (
        isinstance(value, str)
        and value.strip()
        and refs.whole(value) is None
        and env.strict
        and env.datasources is not None
    ):
        source = await env.datasources.get(value)
    if source is not None and source.kind != "databricks":
        p.add(("datasource",), NOT_DATABRICKS)
    else:
        await _check_source(value, ("datasource",), p, env, TOOLS, ("datasource",))

    written = node.get("checks", [])
    if not isinstance(written, list) or len(written) > checks.MAX_CHECKS:
        p.add(("checks",), f"checks 要是 0–{checks.MAX_CHECKS} 条检查的列表")
        written = []
    seen: set[str] = set()
    for i, check in enumerate(written):
        # Fields with references are checked when the run has their values.
        later = {k for k, v in check.items() if _with_refs(v)} if isinstance(check, dict) else set()
        for loc, message in checks.problems(check):
            if not loc or loc[0] not in later:
                p.add(("checks", i, *loc), message)
        cid = check.get("id") if isinstance(check, dict) else None
        if isinstance(cid, str) and "id" not in later:
            if cid == checks.GOLDEN:
                p.add(("checks", i, "id"), "检查 id golden 是留给和老系统对比的，请换一个")
            elif cid in seen:
                p.add(("checks", i, "id"), f"检查 id {cid} 重复了")
            seen.add(cid)

    include = node.get("include", [])
    if not isinstance(include, list) or len(include) > MAX_INCLUDE:
        p.add(("include",), f"include 要是 0–{MAX_INCLUDE} 个变量的列表")
    else:
        for i, item in enumerate(include):
            if not isinstance(item, str) or refs.whole(item) is None:
                p.add(("include", i), "每项必须恰好是一个变量，如 {{check_plan.checks}}")

    golden = node.get("golden")
    if golden is not None:
        if not isinstance(golden, dict):
            p.add(("golden",), "golden 要写成 {expected, table, keys, tolerance, severity}")
        else:
            for key in golden:
                if key not in GOLDEN_KEYS:
                    p.add(("golden", key), f"golden 没有 {key} 这一项")
            for key in ("expected", "table"):
                item = golden.get(key)
                if item is not None and not isinstance(item, str):
                    p.add(("golden", key), f"golden 的 {key} 要是文字或一个变量")
            keys = golden.get("keys")
            if keys is not None and not (
                (isinstance(keys, str) and refs.whole(keys) is not None)
                or (isinstance(keys, list) and all(isinstance(k, str) for k in keys))
            ):
                p.add(("golden", "keys"), "golden 的 keys 要是列名列表或一个变量")
            for key, problem in _golden_extras(golden).items():
                p.add(("golden", key), problem)

    only_if = node.get("only_if")
    if only_if is not None and (not isinstance(only_if, str) or refs.whole(only_if) is None):
        p.add(("only_if",), "only_if 必须恰好是一个变量，如 {{dev_run.ok}}")


def _golden_extras(golden: dict[str, Any]) -> dict[str, str]:
    """What is wrong with the comparison's ``tolerance`` and ``severity`` (references are
    taken at run time)."""
    found = {}
    tolerance = golden.get("tolerance")
    if tolerance is not None and not (isinstance(tolerance, str) and refs.whole(tolerance)):
        number = isinstance(tolerance, int | float) and not isinstance(tolerance, bool)
        if not (number and math.isfinite(tolerance) and tolerance >= 0):
            found["tolerance"] = "golden 的 tolerance 要是不小于 0 的数或一个变量"
    severity = golden.get("severity")
    if severity is not None and not (isinstance(severity, str) and refs.whole(severity)):
        if severity not in checks.SEVERITIES:
            found["severity"] = "golden 的 severity 只能是 " + "、".join(checks.SEVERITIES)
    return found


# Running


def _merged(node: dict[str, Any], get: refs.Lookup) -> list[dict[str, Any]]:
    """The node's checks with those it includes, each one valid, ids unique."""
    found: list[Any] = list(refs.resolve(node.get("checks") or [], get))
    for i, item in enumerate(node.get("include") or []):
        value = refs.resolve(item, get)
        if value is None:
            continue
        if not isinstance(value, list):
            raise NodeFailure(INPUT_ERROR, f"include 第 {i + 1} 项（{item}）的值不是检查的列表")
        found += value
    if len(found) > checks.MAX_ALL:
        raise NodeFailure(INPUT_ERROR, f"一共 {len(found)} 条检查，最多 {checks.MAX_ALL} 条")
    seen: set[str] = set()
    for n, check in enumerate(found, start=1):
        wrong = checks.problems(check)
        cid = check.get("id") if isinstance(check, dict) else None
        named = f"检查 {cid}" if isinstance(cid, str) and cid else f"第 {n} 条检查"
        if wrong:
            loc, message = wrong[0]
            where = ".".join(str(x) for x in loc)
            raise NodeFailure(INPUT_ERROR, f"{named}{f'（{where}）' if where else ''}：{message}")
        if cid == checks.GOLDEN:
            raise NodeFailure(INPUT_ERROR, "检查 id golden 是留给和老系统对比的，请换一个")
        if cid in seen:
            raise NodeFailure(INPUT_ERROR, f"检查 id {cid} 重复了")
        seen.add(cid)  # type: ignore[arg-type]
    return found


def scope(state: dict[str, Any], rt: Runtime) -> checks.Scope:
    """Where this run's tables are (docs/api/data-nodes.md, "data_check" 表名)."""
    s = rt.settings
    project = state.get("project") or None
    if s.databricks_deploy_mode == "cluster":
        results, no_results = None, "cluster 方式不落表，读不到结果表"
    elif project:
        env = project.get("environment") or {}
        results, no_results = f"{env.get('catalog')}.{env.get('schema')}", ""
    elif s.databricks_catalog and s.databricks_schema:
        results, no_results = f"{s.databricks_catalog}.{s.databricks_schema}", ""
    else:
        results = None
        no_results = "平台没有配置 AEP_DATABRICKS_CATALOG、AEP_DATABRICKS_SCHEMA，读不到结果表"
    references = (project or {}).get("references") or []
    filters = {
        str(r.get("name")).lower(): r["filter"]
        for r in references
        if isinstance(r, dict) and r.get("filter")
    }
    params = None
    kinds: dict[str, str] = {}
    if project:
        params = dict(project.get("values") or {})
        kinds = {p["key"]: p.get("type", "text") for p in project.get("params") or []}
    return checks.Scope(
        results,
        no_results,
        s.databricks_source_catalog or None,
        filters,
        params,
        kinds,
    )


def _text(value: Any) -> Any:
    return value.strip() if isinstance(value, str) else value


async def run_data_check(node: dict[str, Any], state: dict[str, Any], rt: Runtime) -> Outcome:
    get = rt.lookup(state)
    only_if = node.get("only_if")
    if only_if is not None and refs.resolve(only_if, get) is not True:
        return Outcome(
            {"ok": False, "results": [], "summary": SKIPPED, "report": SKIPPED},
            branch="failed",
            messages=[{"role": "assistant", "content": SKIPPED}],
            data=["results", "report"],
        )
    source = await rt.source(refs.resolve(node["datasource"], get))
    if source.kind != "databricks":
        raise NodeFailure(INPUT_ERROR, NOT_DATABRICKS)
    if rt.tools is None:
        raise NodeFailure("tool_error", "平台没有配置工具调用")
    tools = rt.tools
    found = _merged(node, get)
    golden = node.get("golden") or {}
    expected = _text(refs.resolve(golden.get("expected"), get))
    where = scope(state, rt)
    seconds = tool_seconds(source, rt.settings)

    async def call(tool: str, args: dict[str, Any]) -> Any:
        try:
            return await tools.call(source.connector_url, tool, args, seconds)
        except ConnectorUnavailable as exc:
            kind = "timeout" if isinstance(exc.__cause__, TimeoutError) else "tool_error"
            raise NodeFailure(kind, f"连不上数据源「{source.name}」：{exc}") from exc

    default = expected if isinstance(expected, str) and expected else None
    results = [await checks.run(check, where, call, default) for check in found]
    if expected not in (None, ""):
        check, problem = checks.golden_check(
            _text(refs.resolve(golden.get("table"), get)),
            expected,
            refs.resolve(golden.get("keys"), get),
            refs.resolve(golden.get("tolerance"), get),
            _text(refs.resolve(golden.get("severity"), get)) or None,
        )
        if problem is None:
            results.append(await checks.run(check, where, call))
        else:
            results.append(checks.result(check, "error", problem))
    ok = checks.passed(results)
    said = checks.summary(results)
    return Outcome(
        {"ok": ok, "results": results, "summary": said, "report": checks.report(results)},
        branch="passed" if ok else "failed",
        messages=[{"role": "assistant", "content": said}],
        data=["results", "report"],
    )


TYPES: dict[str, NodeType] = {
    "data_check": NodeType(
        "data_check",
        "数据检查",
        "经 Databricks 只读连接器核对结果数据：总数、和源表对账、键是否重复、"
        "不该有的行、和老系统逐行对比",
        "data",
        "tool",
        check_data_check,
        run_data_check,
        lambda node: ["passed", "failed"],
        lambda node: ["ok", "results", "summary", "report"],
        outputs=(
            ("ok", "是否通过（没有严重级别为 error 的检查不通过或出错）"),
            ("results", "每条检查的结果"),
            ("summary", "一句话总结"),
            ("report", "检查报告（Markdown）"),
        ),
        defaults={"datasource": "", "checks": [], "include": []},
        error_handling=True,
        branch_labels=lambda node: {"passed": "通过", "failed": "不通过"},
    )
}
