"""``etl``: from a natural-language request to a Databricks Lakeflow pipeline.

profile (read the source through its connector) → plan (the model proposes an
ETL plan) → review (the user confirms or sends it back with comments) →
codegen (the model writes each table's transformation, checked statically,
retried with the problems) → package (bundle files as the run result) → verify
(deploy the pipeline, or a notebook job on an existing cluster, to the
Databricks dev target and run it, or only run the transformations on an existing
cluster; a failed run goes back to codegen with its errors) → publish (the bundle
goes to the assets repository as a pull request, see ``aep.assets``) → release (a
person approves going to prod, or sends the code back with comments; either way
the decision is added to the PR) → deploy_prod (the bundle goes to the prod
target, on a schedule).
Without a configured workspace, verify is skipped and the result says so; without
an assets repository, publish is skipped and ``result.asset`` says why; without a
prod target, the run ends after publish and ``result.release`` says why.
Every decision is kept in ``result.approvals``.

Input: ``datasource_id`` and ``request``; optional ``tables`` (schema.table to
limit profiling), ``language`` ("pyspark" by default, or "sql"), ``name`` (the
bundle name), ``models`` (a model alias per step: ``plan``, ``codegen``) and
``schedule`` (Quartz cron for prod; AEP_DATABRICKS_PROD_SCHEDULE by default) and
``release`` (false: stop after the dev run, as the eval runner does).
"""

import hashlib
import json
import operator
from datetime import UTC, datetime
from typing import Annotated, Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from aep import llm, tracing
from aep.assets import AssetRepoError, AssetStatus
from aep.databricks import DevRun, Release
from aep.etl import codegen
from aep.etl.plan import IDENTIFIER, EtlPlan, PlanError, validate_plan
from aep.etl.plan import SYSTEM_PROMPT as PLAN_PROMPT
from aep.etl.profile import MAX_TABLES, describe_for_model, known_values, profile_tables
from aep.runtime.design import AppDesign, NodeDesign
from aep.runtime.registry import AppContext

CODEGEN_ATTEMPTS = 3
PLAN_ATTEMPTS = 3
DEV_RUN_ATTEMPTS = 3
ERROR_CHARS = 1500

DESIGN = AppDesign(
    title="自然语言 ETL 工作流",
    summary="连接一个关系型数据库，用自然语言描述需求：平台探查数据、给出方案，经人确认后"
    "生成 Databricks 管道代码（默认 PySpark），在 dev 目标试跑，代码以 PR 提交到资产仓库，"
    "审批通过后上线到 prod。",
    pattern="固定流程的工作流（LangGraph 图），不是自主决定下一步的 agent。节点和走向由图事先"
    "定好，模型只在“出方案”和“写代码”两个节点里生成内容，每次输出都要经过平台校验，不合格就"
    "带着问题让模型重做；方案和上线各有一个人工确认点。这样做是为了让企业 ETL 可控、可审批、"
    "可复现：同样的需求走同样的步骤，每一步做了什么都有记录。",
    knowledge="没有接知识库。模型用到的知识是“探查数据”节点现场读到的源表结构、空值率、取值"
    "分布和样本行，每次运行都重新读取，作为上下文交给模型；再加上写在提示词里的 Databricks "
    "编码规范。",
    skills="没有使用 Skill。两个模型节点的提示词写在代码里（可在节点详情里查看全文），还不是"
    "可单独复用、可版本管理的资产。",
)

NODES = {
    "profile": NodeDesign(
        title="探查数据",
        kind="tool",
        summary="通过数据源的只读连接器读取源表：列出库和表，读取每张表的结构、外键、行数、"
        "每列的空值率和取值分布，以及几行样本。",
        inputs="选择的数据源和需求；可以指定只看哪些表",
        outputs="每张源表的探查结果",
        tools=[
            "数据源连接器（MCP，只读）：list_schemas、list_tables、describe_table、"
            "table_stats、sample_rows"
        ],
        checks=f"数据源必须已登记，需求不能为空；一次最多探查 {MAX_TABLES} 张表",
    ),
    "plan": NodeDesign(
        title="出方案",
        kind="model",
        summary="模型根据需求和探查结果写出 ETL 方案：要建哪些目标表、从哪些源表来、每列怎么算、"
        "全量还是增量加载、数据质量规则，以及需要人确认的假设。被驳回时带着修改意见重写。",
        inputs="需求、期望的代码语言、探查结果；驳回时还有上一版方案和修改意见",
        outputs="ETL 方案（JSON）",
        model_step="plan",
        prompt=PLAN_PROMPT,
        knowledge="探查结果（源表结构、统计、取值分布、样本行），没有知识库",
        checks="平台校验方案：只能用探查到的表和列，目标表名合法，比较用到的取值必须在数据里"
        "出现过（否则要写进假设让人确认）",
        retry=f"校验不通过时把问题告诉模型重写，最多 {PLAN_ATTEMPTS} 次",
    ),
    "review": NodeDesign(
        title="确认方案",
        kind="human",
        summary="运行暂停，等人看方案。确认后开始写代码；驳回并写下意见，就回到“出方案”按意见重写。",
        inputs="方案",
        outputs="确认或驳回的决定（谁、何时、意见）",
    ),
    "codegen": NodeDesign(
        title="写代码",
        kind="model",
        summary="模型按确认的方案为每张目标表写转换代码（PySpark 函数体或 SQL 查询）。代码检查、"
        "dev 试跑或上线审批发现问题时，带着问题回到这里重写。",
        inputs="确认的方案、源表的列；重写时还有上一版代码和问题",
        outputs="每张目标表的代码",
        model_step="codegen",
        prompt=codegen.SYSTEM_PROMPT,
        knowledge="确认的方案和源表的列，没有知识库",
        checks="静态检查：每张目标表都有代码且语法正确；只读方案里列出的源表；不许写数据、"
        "执行任意 SQL、import 或碰系统（如 .write、spark.sql、dbutils）；SQL 只能是一条 SELECT",
        retry=f"检查不通过时把问题告诉模型重写，最多 {CODEGEN_ATTEMPTS} 次",
    ),
    "package": NodeDesign(
        title="打包",
        kind="code",
        summary="把代码套进平台的模板，生成 Databricks bundle：databricks.yml（dev、prod 两个"
        "目标）、管道代码和说明，再整体检查一遍。没配 Databricks 工作区时运行到这里结束。",
        inputs="方案和通过检查的代码",
        outputs="bundle 文件，作为运行结果",
        checks="bundle 文件能解析、彼此对得上",
    ),
    "verify": NodeDesign(
        title="dev 试跑",
        kind="tool",
        summary="把 bundle 部署到 Databricks dev 目标并实际运行，收集每张表的行数、被丢弃的行"
        "和质量规则结果。",
        inputs="bundle 文件",
        outputs="试跑结果：状态、各表行数、质量规则、报错",
        tools=[
            "Databricks REST API（按配置的方式：集群上的 notebook Job、Lakeflow 管道，"
            "或只在集群上跑转换）"
        ],
        retry=f"可修的报错带回“写代码”重写，最多试跑 {DEV_RUN_ATTEMPTS} 次",
    ),
    "publish": NodeDesign(
        title="提交资产",
        kind="tool",
        summary="试跑通过的代码以新版本提交到资产仓库，开一个 PR，PR 里写明需求、方案、试跑结果"
        "和这次运行的链接，沉淀为公司资产。",
        inputs="bundle 文件和试跑结果",
        outputs="资产仓库里的 PR 和版本号",
        tools=["资产仓库（GitHub）：建分支、提交文件、开 PR"],
    ),
    "release": NodeDesign(
        title="上线审批",
        kind="human",
        summary="运行暂停，等审批者决定是否上线到 prod。批准就上线；驳回并写下意见，代码回到"
        "“写代码”重写，重新试跑并开新版本的 PR。决定会写到 PR 评论里。",
        inputs="试跑结果、prod 目标和运行计划",
        outputs="批准或驳回的决定（谁、何时、意见）",
        tools=["资产仓库（GitHub）：在 PR 上记录审批决定"],
    ),
    "deploy_prod": NodeDesign(
        title="上线 prod",
        kind="tool",
        summary="把审批通过的 bundle 部署到 Databricks prod 目标，按计划定时运行，结果写回 PR。",
        inputs="bundle 文件和运行计划",
        outputs="上线结果：作业或管道、计划、链接",
        tools=["Databricks REST API", "资产仓库（GitHub）：在 PR 上记录上线结果"],
    ),
}


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
    approvals: Annotated[list[dict[str, Any]], operator.add]
    result: dict[str, Any] | None


def _plan_message(plan: EtlPlan) -> str:
    lines = [plan.summary, ""]
    for t in plan.targets:
        load = "增量" if t.load.mode == "incremental" else "全量"
        lines.append(f"- {t.name}（{load}，来自 {', '.join(t.sources)}）：{t.description}")
    if plan.assumptions:
        lines += ["", "请确认这些假设："]
        lines += [f"- {a}" for a in plan.assumptions]
    return "\n".join(lines)


def _dev_message(name: str, dev: DevRun, retry: bool) -> str:
    on_cluster = dev.mode == "cluster"
    if dev.status == "passed":
        if on_cluster:
            head = f"已在集群 {dev.cluster_id} 上试跑通过 {name}（只执行了转换逻辑，没有部署管道）"
        elif dev.mode == "job":
            head = f"已在 Databricks dev 目标上部署并运行 Job {name}（集群 {dev.cluster_id}）"
            head += f"：{dev.url}" if dev.url else ""
        else:
            head = f"已在 Databricks dev 目标上跑通 {name}" + (f"：{dev.url}" if dev.url else "")
        lines = [head]
        for t in dev.tables:
            rows = "未知" if t.rows is None else str(t.rows)
            dropped = f"，质量规则丢弃 {t.dropped} 行" if t.dropped else ""
            lines.append(f"- {t.name}：{'得到' if on_cluster else '写入'} {rows} 行{dropped}")
    else:
        stage = {"validate": "校验", "deploy": "部署", "run": "运行"}[dev.stage]
        where = f"在集群 {dev.cluster_id} 上试跑" if on_cluster else f"Databricks dev 目标{stage}"
        if dev.mode == "job":
            where = f"Databricks dev 目标上的 Job {stage}"
        lines = [f"{where}失败："]
        lines += [f"- {e[:500]}" for e in dev.errors[:5]]
        if retry:
            lines.append("带着报错重新生成代码。")
    lines += [f"注意：{w}" for w in dev.warnings]
    return "\n".join(lines)


def _approval(step: str, answer: dict[str, Any]) -> dict[str, Any]:
    return {
        "step": step,
        "decision": answer.get("decision"),
        "comment": answer.get("comment"),
        "actor": answer.get("actor"),
        "at": datetime.now(UTC).isoformat(),
    }


def _release_message(name: str, release: Release) -> str:
    where = f"{release.catalog}.{release.schema}"
    if release.status == "failed":
        return "\n".join(
            [f"上线到 prod 失败（{name} → {where}）：", *[f"- {e[:500]}" for e in release.errors]]
        )
    what = f"Job {release.job_id}" if release.mode == "job" else f"管道 {release.pipeline_id}"
    head = f"已上线到 prod：{name} 部署为 {what}，写入 {where}"
    head += f"，按计划 `{release.schedule}`（{release.timezone}）运行"
    return head + (f"：{release.url}" if release.url else "")


def _shorten(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters at a punctuation mark where it can be."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    stop = max(cut.rfind(mark) for mark in "，。；、,;")
    return (cut[:stop] if stop >= limit // 2 else cut).rstrip() + "…"


def _decision(approval: dict[str, Any]) -> str:
    verdict = "通过" if approval.get("decision") == "approve" else "驳回"
    who = approval.get("actor") or "未署名"
    line = f"{verdict}（{who}，{approval.get('at', '')}）"
    return line + (f"，意见：{approval['comment']}" if approval.get("comment") else "")


def _cell(text: Any) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ")


def _asset_body(state: "EtlState", result: dict[str, Any], run_url: str, trace: str | None) -> str:
    """The PR description: what was asked, what was confirmed, how dev went, who decided."""
    confirmed = EtlPlan.model_validate(result["plan"])
    lines = ["## 需求", "", state["input"].get("request", ""), "", "## 确认后的方案", ""]
    lines.append(confirmed.summary)
    lines.append("")
    for t in confirmed.targets:
        load = "增量" if t.load.mode == "incremental" else "全量"
        lines.append(f"- `{t.name}`（{load}，来自 {', '.join(t.sources)}）：{t.description}")
    if confirmed.assumptions:
        lines += ["", "需要确认的假设：", ""]
        lines += [f"- {a}" for a in confirmed.assumptions]
    dev = result.get("verification") or {}
    lines += ["", "## dev 试跑", ""]
    lines.append(f"状态：{dev.get('status')}，方式：{dev.get('mode')}，第 {dev.get('attempt')} 次")
    if dev.get("url"):
        lines.append(f"Databricks：{dev['url']}")
    for t in dev.get("tables", []):
        rows = "未知" if t.get("rows") is None else t["rows"]
        dropped = f"，质量规则丢弃 {t['dropped']} 行" if t.get("dropped") else ""
        lines.append(f"- `{t.get('name')}`：{rows} 行{dropped}")
    lines += [
        "",
        "## 审批记录",
        "",
        "| 步骤 | 结论 | 审批人 | 时间 | 意见 |",
        "| --- | --- | --- | --- | --- |",
    ]
    steps = {"plan": "方案确认", "release": "上线审批"}
    for a in state.get("approvals", []):
        verdict = "通过" if a.get("decision") == "approve" else "驳回"
        lines.append(
            f"| {steps.get(a.get('step'), a.get('step'))} | {verdict} | {_cell(a.get('actor'))}"
            f" | {_cell(a.get('at'))} | {_cell(a.get('comment'))} |"
        )
    lines += ["", "## 来源", "", f"- 数据源：{state.get('source_name', '')}"]
    lines.append(f"- 平台运行：{run_url}")
    if trace:
        lines.append(f"- Langfuse trace：`{trace}`")
    lines += ["", "上线审批的结论会以评论补在这个 PR 上；是否合并由人决定。"]
    return "\n".join(lines)


def build(ctx: AppContext) -> StateGraph:
    job = ctx.settings.databricks_deploy_mode == "job"
    compute = None
    if ctx.settings.databricks_pipeline_node_type or ctx.settings.databricks_pipeline_policy_id:
        compute = codegen.Compute(
            ctx.settings.databricks_pipeline_node_type,
            ctx.settings.databricks_pipeline_workers,
            ctx.settings.databricks_pipeline_policy_id,
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
        known = known_values(state["profile"])
        for attempt in range(PLAN_ATTEMPTS):
            reply = await llm.chat(ctx.llm_client, prompt, model=model_for(state, "plan"))
            try:
                result = validate_plan(llm.parse_json(reply.text), sources, known, attempt > 0)
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
        approvals = [_approval("plan", answer)]
        if answer.get("decision") == "approve":
            return {
                "messages": [{"role": "user", "content": "确认方案"}],
                "attempts": 0,
                "approvals": approvals,
            }
        comment = answer.get("comment") or "请换一种做法"
        return {
            "messages": [{"role": "user", "content": f"驳回：{comment}"}],
            "feedback": comment,
            "approvals": approvals,
        }

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
        # Without a name the bundle is named after its first target table, so later runs for
        # the same table add versions to the same asset rather than starting new ones.
        name = state["input"].get("name") or confirmed.targets[0].name
        if not IDENTIFIER.match(name):
            name = "etl_" + config["configurable"]["thread_id"].replace("-", "")[:8]
        if not problems:
            bundle = codegen.render(
                name, confirmed, state["code"], state["source_name"], compute, job
            )
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
                "approvals": state.get("approvals", []),
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
        elif dev.status != "passed":
            result["release"] = {"status": "skipped", "reason": "dev 试跑没有通过，不能上线"}
        elif state["input"].get("release") is False:
            result["release"] = {"status": "skipped", "reason": "这次运行只试跑，不上线"}
        elif blocker := ctx.settings.databricks_release_blocker:
            result["release"] = {"status": "skipped", "reason": blocker}
        return update

    def after_verify(state: EtlState) -> str:
        if state.get("problems"):
            return "codegen"
        result = state["result"] or {}
        passed = (result.get("verification") or {}).get("status") == "passed"
        return "publish" if passed else END

    async def publish(state: EtlState, config: RunnableConfig) -> dict[str, Any]:
        result = dict(state["result"] or {})
        name = result["bundle"]
        if state["input"].get("release") is False:
            result["asset"] = {"status": "skipped", "reason": "这次运行只试跑，不提交资产"}
            return {"result": result}
        if ctx.assets is None:
            result["asset"] = {
                "status": "skipped",
                "reason": "没有配置资产仓库（AEP_ASSETS_REPO、AEP_ASSETS_GITHUB_TOKEN）",
            }
            return {"result": result}
        run_id = config["configurable"]["thread_id"]
        s = ctx.settings
        trace = (
            tracing.trace_id(run_id) if s.langfuse_public_key and s.langfuse_secret_key else None
        )
        run_url = f"{s.web_url.rstrip('/')}/runs/{run_id}"
        # The same code after the same decisions is the same revision: running this step
        # again (after a restart) gets the same version, branch and PR.
        revision = hashlib.sha256(
            json.dumps([result["files"], len(state.get("approvals", []))], sort_keys=True).encode()
        ).hexdigest()[:16]
        version = await ctx.asset_store.reserve(name, run_id, revision)
        summary = EtlPlan.model_validate(result["plan"]).summary
        directory = f"pipelines/{name}"
        try:
            published = await ctx.assets.open_pr(
                directory,
                result["files"],
                # The run in the name keeps two runs of one bundle off each other's PRs.
                branch=f"aep/{name}/v{version}-{run_id[:8]}",
                title=f"{name} v{version}：{_shorten(summary, 60)}",
                body=_asset_body(state, result, run_url, trace),
                message=f"{name} v{version}（平台运行 {run_id}）",
            )
        except AssetRepoError as exc:
            result["asset"] = {"status": "failed", "repo": ctx.assets.repo, "errors": [str(exc)]}
            text = f"提交到资产仓库 {ctx.assets.repo} 失败：{exc}"
            return {"result": result, "messages": [{"role": "assistant", "content": text}]}
        await ctx.asset_store.record(name, version, published)
        result["asset"] = {
            "status": "proposed",
            "repo": ctx.assets.repo,
            "path": directory,
            "version": version,
            "pr_number": published.number,
            "pr_url": published.url,
            "branch": published.branch,
            "commit": published.commit,
        }
        text = f"已在资产仓库 {ctx.assets.repo} 开 PR #{published.number}（{name} v{version}）"
        return {
            "result": result,
            "messages": [{"role": "assistant", "content": f"{text}：{published.url}"}],
        }

    def after_publish(state: EtlState) -> str:
        return END if "release" in (state["result"] or {}) else "release"

    async def note_on_pr(result: dict[str, Any], body: str, status: AssetStatus) -> None:
        """Add a decision to the bundle's PR and the index; a failure is kept, not raised."""
        asset = result.get("asset") or {}
        if asset.get("status") != "proposed" or ctx.assets is None:
            return
        await ctx.asset_store.set_status(result["bundle"], asset["version"], status)
        result["asset"] = asset | {"status": status}
        try:
            await ctx.assets.comment(asset["pr_number"], body)
        except AssetRepoError as exc:
            result["asset"]["errors"] = [*asset.get("errors", []), str(exc)]

    def release_target(state: EtlState) -> dict[str, Any]:
        s = ctx.settings
        mode = "job" if s.databricks_deploy_mode == "job" else "pipeline"
        target = {
            "mode": mode,
            "catalog": s.databricks_prod_catalog,
            "schema": s.databricks_prod_schema,
            "schedule": state["input"].get("schedule") or s.databricks_prod_schedule,
            "timezone": s.databricks_prod_timezone,
        }
        if mode == "job":
            target["cluster_id"] = s.databricks_prod_cluster_id or s.databricks_cluster_id
        return target

    async def release(state: EtlState) -> dict[str, Any]:
        result = dict(state["result"] or {})
        target = release_target(state)
        answer = interrupt(
            {
                "kind": "approval",
                "prompt": f"dev 试跑已通过。确认把 {result['bundle']} 上线到 prod"
                f"（{target['catalog']}.{target['schema']}，按计划 {target['schedule']} 运行）吗？"
                "驳回时写下修改意见，代码会按意见重写。",
                "options": ["approve", "reject"],
                "details": {
                    "type": "etl_release",
                    "bundle": result["bundle"],
                    "verification": result.get("verification"),
                    "target": target,
                },
            }
        )
        approval = _approval("release", answer)
        result["approvals"] = [*state.get("approvals", []), approval]
        if answer.get("decision") == "approve":
            return {
                "result": result,
                "approvals": [approval],
                "messages": [{"role": "user", "content": "确认上线"}],
            }
        comment = answer.get("comment") or "请改进代码"
        await note_on_pr(
            result,
            f"**上线审批：{_decision(approval)}**\n\n"
            "代码会按意见重写，dev 试跑通过后另开新版本的 PR。",
            "rejected",
        )
        return {
            "result": result,
            "approvals": [approval],
            "messages": [{"role": "user", "content": f"驳回上线：{comment}"}],
            "problems": [f"审批者驳回了上线，意见：{comment}"],
            "attempts": 0,
            "dev_runs": 0,
        }

    def after_release(state: EtlState) -> str:
        return "codegen" if state.get("problems") else "deploy_prod"

    async def deploy_prod(state: EtlState) -> dict[str, Any]:
        assert ctx.databricks is not None
        result = dict(state["result"] or {})
        bundle = codegen.Bundle(result["bundle"], result["files"])
        shipped = await ctx.databricks.release(bundle, state["input"].get("schedule"))
        result["release"] = shipped.as_dict()
        message = _release_message(result["bundle"], shipped)
        approval = (state.get("approvals") or [{}])[-1]
        await note_on_pr(
            result,
            f"**上线审批：{_decision(approval)}**\n\n{message}",
            "released" if shipped.status == "deployed" else "release_failed",
        )
        return {
            "result": result,
            "messages": [{"role": "assistant", "content": message}],
        }

    def after_review(state: EtlState) -> str:
        return "plan" if state.get("feedback") else "codegen"

    graph = StateGraph(EtlState)
    steps = {
        "profile": profile,
        "plan": plan,
        "review": review,
        "codegen": write_code,
        "package": package,
        "verify": verify,
        "publish": publish,
        "release": release,
        "deploy_prod": deploy_prod,
    }
    for name, step in steps.items():
        graph.add_node(name, step, metadata=NODES[name].metadata())
    graph.add_edge(START, "profile")
    graph.add_edge("profile", "plan")
    graph.add_edge("plan", "review")
    graph.add_conditional_edges("review", after_review, ["plan", "codegen"])
    graph.add_conditional_edges("codegen", after_code, ["codegen", "package"])
    graph.add_conditional_edges("package", after_package, ["verify", END])
    graph.add_conditional_edges("verify", after_verify, ["codegen", "publish", END])
    graph.add_conditional_edges("publish", after_publish, ["release", END])
    graph.add_conditional_edges("release", after_release, ["codegen", "deploy_prod"])
    graph.add_edge("deploy_prod", END)
    return graph
