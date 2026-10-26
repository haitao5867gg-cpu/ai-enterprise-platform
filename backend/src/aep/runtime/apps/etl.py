"""``etl``: from a natural-language request to a Databricks Lakeflow pipeline.

profile (read the source through its connector) → plan (the model proposes an
ETL plan) → review (the user confirms or sends it back with comments) →
codegen (the model writes each table's transformation, checked statically,
retried with the problems) → package (bundle files as the run result) → verify
(deploy to the Databricks dev target and run it; a failed run goes back to
codegen with its errors). Without a configured workspace, verify is skipped and
the result says so.

Input: ``datasource_id`` and ``request``; optional ``tables`` (schema.table to
limit profiling), ``language`` ("pyspark" by default, or "sql"), ``name`` (the
bundle name) and ``models`` (a model alias per step: ``plan``, ``codegen``).
"""

import json
import operator
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from aep import llm
from aep.databricks import DevRun
from aep.etl import codegen
from aep.etl.plan import IDENTIFIER, EtlPlan, PlanError, validate_plan
from aep.etl.plan import SYSTEM_PROMPT as PLAN_PROMPT
from aep.etl.profile import describe_for_model, profile_tables
from aep.runtime.registry import AppContext

CODEGEN_ATTEMPTS = 3
PLAN_ATTEMPTS = 2
DEV_RUN_ATTEMPTS = 3
ERROR_CHARS = 1500


class EtlState(TypedDict, total=False):
    input: dict[str, Any]
    messages: Annotated[list[dict[str, str]], operator.add]
    source_name: str
    profile: list[dict[str, Any]]
    plan: dict[str, Any]
    feedback: str | None
    code: dict[str, str]
    problems: list[str]
    attempts: int
    dev_runs: int
    result: dict[str, Any] | None


def _plan_message(plan: EtlPlan) -> str:
    lines = [plan.summary, ""]
    for t in plan.targets:
        load = "增量" if t.load.mode == "incremental" else "全量"
        lines.append(f"- {t.name}（{load}，来自 {', '.join(t.sources)}）：{t.description}")
    return "\n".join(lines)


def _dev_message(name: str, dev: DevRun, retry: bool) -> str:
    if dev.status == "passed":
        lines = [f"已在 Databricks dev 目标上跑通 {name}" + (f"：{dev.url}" if dev.url else "")]
        for t in dev.tables:
            rows = "未知" if t.rows is None else str(t.rows)
            dropped = f"，质量规则丢弃 {t.dropped} 行" if t.dropped else ""
            lines.append(f"- {t.name}：写入 {rows} 行{dropped}")
    else:
        stage = {"validate": "校验", "deploy": "部署", "run": "运行"}[dev.stage]
        lines = [f"Databricks dev 目标{stage}失败："]
        lines += [f"- {e[:500]}" for e in dev.errors[:5]]
        if retry:
            lines.append("带着报错重新生成代码。")
    lines += [f"注意：{w}" for w in dev.warnings]
    return "\n".join(lines)


def build(ctx: AppContext) -> StateGraph:
    compute = None
    if ctx.settings.databricks_pipeline_node_type:
        compute = codegen.Compute(
            ctx.settings.databricks_pipeline_node_type, ctx.settings.databricks_pipeline_workers
        )

    def model_for(state: EtlState, step: str) -> str:
        return (state["input"].get("models") or {}).get(step) or ctx.settings.default_model

    async def profile(state: EtlState) -> dict[str, Any]:
        inp = state["input"]
        source = await ctx.datasources.get(str(inp.get("datasource_id", "")))
        if source is None:
            raise ValueError("请选择一个已登记的数据源（input.datasource_id）")
        if not str(inp.get("request", "")).strip():
            raise ValueError("请用自然语言描述需求（input.request）")
        tables = await profile_tables(ctx.tools, source, inp.get("tables"))
        names = "、".join(t["name"] for t in tables)
        return {
            "source_name": source.name,
            "profile": tables,
            "messages": [
                {"role": "user", "content": inp["request"]},
                {
                    "role": "assistant",
                    "content": f"已探查 {source.name} 的 {len(tables)} 张表：{names}",
                },
            ],
        }

    async def plan(state: EtlState) -> dict[str, Any]:
        inp = state["input"]
        language = inp.get("language") or "pyspark"
        ask = (
            f"需求：{inp['request']}\n期望的代码语言：{language}\n\n"
            f"源表（只能使用这些）：\n{describe_for_model(state['profile'])}"
        )
        prompt = [{"role": "system", "content": PLAN_PROMPT}, {"role": "user", "content": ask}]
        if state.get("feedback"):
            prompt.append(
                {"role": "assistant", "content": json.dumps(state["plan"], ensure_ascii=False)}
            )
            prompt.append({"role": "user", "content": f"用户的修改意见：{state['feedback']}"})
        sources = [t["name"] for t in state["profile"]]
        for attempt in range(PLAN_ATTEMPTS):
            reply = await llm.chat(ctx.llm_client, prompt, model=model_for(state, "plan"))
            try:
                result = validate_plan(llm.parse_json(reply.text), sources)
                break
            except (ValueError, PlanError) as exc:
                if attempt == PLAN_ATTEMPTS - 1:
                    raise PlanError(f"模型给出的方案不可用：{exc}") from exc
                prompt.append({"role": "assistant", "content": reply.text})
                prompt.append(
                    {"role": "user", "content": f"这个方案有问题，请修正后重新输出完整 JSON：{exc}"}
                )
        if inp.get("language") in ("pyspark", "sql"):
            result.language = inp["language"]
        return {
            "plan": result.model_dump(),
            "feedback": None,
            "messages": [{"role": "assistant", "content": _plan_message(result)}],
        }

    def review(state: EtlState) -> dict[str, Any]:
        answer = interrupt(
            {
                "kind": "approval",
                "prompt": "确认这个 ETL 方案吗？确认后开始生成代码；驳回时写下修改意见。",
                "options": ["approve", "reject"],
                "details": {"type": "etl_plan", "plan": state["plan"]},
            }
        )
        if answer.get("decision") == "approve":
            return {"messages": [{"role": "user", "content": "确认方案"}], "attempts": 0}
        comment = answer.get("comment") or "请换一种做法"
        return {"messages": [{"role": "user", "content": f"驳回：{comment}"}], "feedback": comment}

    async def write_code(state: EtlState) -> dict[str, Any]:
        confirmed = EtlPlan.model_validate(state["plan"])
        columns = {
            t["name"]: [f"{c['name']} {c['data_type']}" for c in t["columns"]]
            for t in state["profile"]
        }
        ask = (
            f"已确认的方案：\n{json.dumps(state['plan'], ensure_ascii=False, indent=1)}\n\n"
            f"源表的列：\n{json.dumps(columns, ensure_ascii=False)}"
        )
        prompt = [
            {"role": "system", "content": codegen.SYSTEM_PROMPT},
            {"role": "user", "content": ask},
        ]
        if state.get("problems"):
            prompt.append(
                {
                    "role": "assistant",
                    "content": json.dumps({"targets": state["code"]}, ensure_ascii=False),
                }
            )
            prompt.append(
                {
                    "role": "user",
                    "content": "这些问题需要修正，请输出完整 JSON：\n"
                    + "\n".join(state["problems"]),
                }
            )
        reply = await llm.chat(ctx.llm_client, prompt, model=model_for(state, "codegen"))
        try:
            code = llm.parse_json(reply.text).get("targets") or {}
            problems = codegen.check_code(confirmed, code)
        except (ValueError, AttributeError) as exc:
            code, problems = state.get("code", {}), [f"回复不是要求的 JSON：{exc}"]
        return {"code": code, "problems": problems, "attempts": state.get("attempts", 0) + 1}

    def after_code(state: EtlState) -> str:
        if state["problems"] and state["attempts"] < CODEGEN_ATTEMPTS:
            return "codegen"
        return "package"

    def package(state: EtlState, config: RunnableConfig) -> dict[str, Any]:
        confirmed = EtlPlan.model_validate(state["plan"])
        problems = list(state["problems"])
        files: dict[str, str] = {}
        name = state["input"].get("name") or ""
        if not IDENTIFIER.match(name):
            name = "etl_" + config["configurable"]["thread_id"].replace("-", "")[:8]
        if not problems:
            bundle = codegen.render(name, confirmed, state["code"], state["source_name"], compute)
            problems = codegen.check_bundle(bundle)
            files = bundle.files
        verification: dict[str, Any] | None = None
        if problems:
            text = "代码没有通过检查，已停止：\n" + "\n".join(f"- {p}" for p in problems)
            verification = {"status": "skipped", "reason": "代码没有通过静态检查"}
        else:
            text = f"已生成 Databricks bundle {name}：" + "、".join(sorted(files))
            if ctx.databricks is None:
                verification = {
                    "status": "skipped",
                    "reason": "没有配置 Databricks 工作区（AEP_DATABRICKS_HOST、"
                    "AEP_DATABRICKS_TOKEN），跳过 dev 试跑",
                }
            else:
                text += "\n接下来部署到 Databricks dev 目标试跑。"
        return {
            "messages": [{"role": "assistant", "content": text}],
            "problems": problems,
            "result": {
                "bundle": name,
                "language": confirmed.language,
                "plan": state["plan"],
                "files": files,
                "checks": {"passed": not problems, "problems": problems},
                "verification": verification,
            },
        }

    def after_package(state: EtlState) -> str:
        result = state["result"] or {}
        return "verify" if result.get("verification") is None else END

    async def verify(state: EtlState) -> dict[str, Any]:
        assert ctx.databricks is not None
        result = dict(state["result"] or {})
        dev = await ctx.databricks.run(codegen.Bundle(result["bundle"], result["files"]))
        runs = state.get("dev_runs", 0) + 1
        retry = dev.status == "failed" and dev.fixable and runs < DEV_RUN_ATTEMPTS
        result["verification"] = dev.as_dict() | {"attempt": runs}
        update: dict[str, Any] = {
            "result": result,
            "dev_runs": runs,
            "messages": [
                {"role": "assistant", "content": _dev_message(result["bundle"], dev, retry)}
            ],
        }
        if retry:
            errors = [e[:ERROR_CHARS] for e in dev.errors[:10]]
            update["problems"] = ["代码在 Databricks dev 目标上运行失败，报错如下：", *errors]
            update["attempts"] = 0
        return update

    def after_verify(state: EtlState) -> str:
        return "codegen" if state.get("problems") else END

    def after_review(state: EtlState) -> str:
        return "plan" if state.get("feedback") else "codegen"

    graph = StateGraph(EtlState)
    graph.add_node("profile", profile)
    graph.add_node("plan", plan)
    graph.add_node("review", review)
    graph.add_node("codegen", write_code)
    graph.add_node("package", package)
    graph.add_node("verify", verify)
    graph.add_edge(START, "profile")
    graph.add_edge("profile", "plan")
    graph.add_edge("plan", "review")
    graph.add_conditional_edges("review", after_review, ["plan", "codegen"])
    graph.add_conditional_edges("codegen", after_code, ["codegen", "package"])
    graph.add_conditional_edges("package", after_package, ["verify", END])
    graph.add_conditional_edges("verify", after_verify, ["codegen", END])
    return graph
