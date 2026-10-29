"""Flow apps the platform ships (``hello``) and the templates new apps start from."""

import copy
from dataclasses import dataclass
from typing import Any

HELLO_SYSTEM = "你是一个乐于助人的助手，用中文简洁地回答。"


def _qa_approval() -> dict[str, Any]:
    """Answer, ask a person, rewrite with their comment when they reject."""
    return {
        "schema_version": 1,
        "env": {"vars": [], "secrets": []},
        "graph": {
            "nodes": [
                {
                    "id": "start",
                    "type": "start",
                    "title": "开始",
                    "inputs": [
                        {"key": "text", "label": "问题", "type": "textarea", "required": True}
                    ],
                },
                {
                    "id": "draft",
                    "type": "llm",
                    "title": "回答",
                    "model": None,
                    "skills": [],
                    "system": HELLO_SYSTEM,
                    "prompt": (
                        "问题：{{start.text}}\n上一版回答：{{draft.text}}\n"
                        "修改意见：{{review.comment}}\n"
                        "（没有上一版时直接回答问题；有修改意见时按意见重写上一版。）"
                    ),
                    "output": {"kind": "text"},
                    "max_visits": 5,
                },
                {
                    "id": "review",
                    "type": "human",
                    "title": "确认",
                    "mode": "approval",
                    "prompt": "确认这个回答吗？驳回时可以写下修改意见。",
                    "show": "{{draft.text}}",
                    "options": [
                        {"key": "approve", "label": "确认"},
                        {"key": "reject", "label": "驳回"},
                    ],
                    "comment_required": [],
                    "max_visits": 5,
                },
                {
                    "id": "end",
                    "type": "end",
                    "title": "结束",
                    "outputs": [{"key": "answer", "from": ["{{draft.text}}"]}],
                },
            ],
            "edges": [
                {"from": "start", "to": "draft"},
                {"from": "draft", "to": "review"},
                {"from": "review", "branch": "approve", "to": "end"},
                {"from": "review", "branch": "reject", "to": "draft"},
            ],
        },
    }


def _intent_routing() -> dict[str, Any]:
    """After-sales questions get a drafted reply a person confirms; product questions
    are answered directly; anything else is handed to a person."""
    return {
        "schema_version": 1,
        "env": {"vars": [], "secrets": []},
        "graph": {
            "nodes": [
                {
                    "id": "start",
                    "type": "start",
                    "title": "开始",
                    "inputs": [
                        {
                            "key": "question",
                            "label": "客户问题",
                            "type": "textarea",
                            "required": True,
                        }
                    ],
                },
                {
                    "id": "route",
                    "type": "classifier",
                    "title": "判断问题类型",
                    "model": None,
                    "skills": [],
                    "text": "{{start.question}}",
                    "classes": [
                        {
                            "key": "after_sales",
                            "label": "售后",
                            "description": "退换货、维修、投诉",
                        },
                        {"key": "product", "label": "产品咨询", "description": "功能、价格、规格"},
                    ],
                },
                {
                    "id": "draft",
                    "type": "llm",
                    "title": "起草售后回复",
                    "model": None,
                    "skills": [],
                    "system": "你是售后客服，语气礼貌，先致歉再给出处理办法。",
                    "prompt": "客户问：{{start.question}}\n上次的修改意见：{{review.comment}}",
                    "output": {"kind": "text"},
                },
                {
                    "id": "review",
                    "type": "human",
                    "title": "客服确认",
                    "mode": "approval",
                    "prompt": "确认这条回复吗？",
                    "show": "{{draft.text}}",
                    "options": [
                        {"key": "approve", "label": "确认"},
                        {"key": "reject", "label": "驳回"},
                    ],
                    "comment_required": ["reject"],
                },
                {
                    "id": "answer",
                    "type": "llm",
                    "title": "回答咨询",
                    "model": None,
                    "skills": [],
                    "prompt": "{{start.question}}",
                    "output": {"kind": "text"},
                },
                {
                    "id": "handoff",
                    "type": "template",
                    "title": "提示转人工",
                    "template": "这个问题已转给人工客服。",
                },
                {
                    "id": "end",
                    "type": "end",
                    "title": "结束",
                    "outputs": [
                        {
                            "key": "reply",
                            "from": ["{{draft.text}}", "{{answer.text}}", "{{handoff.text}}"],
                        }
                    ],
                },
            ],
            "edges": [
                {"from": "start", "to": "route"},
                {"from": "route", "branch": "after_sales", "to": "draft"},
                {"from": "route", "branch": "product", "to": "answer"},
                {"from": "route", "branch": "other", "to": "handoff"},
                {"from": "draft", "to": "review"},
                {"from": "review", "branch": "approve", "to": "end"},
                {"from": "review", "branch": "reject", "to": "draft"},
                {"from": "answer", "to": "end"},
                {"from": "handoff", "to": "end"},
            ],
        },
    }


def _datasource_qa() -> dict[str, Any]:
    """An agent answers a question from a data source picked when the run starts."""
    return {
        "schema_version": 1,
        "env": {"vars": [], "secrets": []},
        "graph": {
            "nodes": [
                {
                    "id": "start",
                    "type": "start",
                    "title": "开始",
                    "inputs": [
                        {"key": "question", "label": "问题", "type": "textarea", "required": True},
                        {
                            "key": "datasource",
                            "label": "数据源",
                            "type": "datasource",
                            "required": True,
                        },
                    ],
                },
                {
                    "id": "agent",
                    "type": "agent",
                    "title": "查数据并回答",
                    "model": None,
                    "skills": [],
                    "task": (
                        "根据数据源里的数据回答问题：{{start.question}}\n"
                        "先看有哪些表和字段，再查需要的数据；回答时说明用到了哪些表和数据。"
                    ),
                    "tools": [{"datasource": "{{start.datasource}}", "tools": None}],
                    "max_steps": 8,
                },
                {
                    "id": "end",
                    "type": "end",
                    "title": "结束",
                    "outputs": [{"key": "answer", "from": ["{{agent.text}}"]}],
                },
            ],
            "edges": [{"from": "start", "to": "agent"}, {"from": "agent", "to": "end"}],
        },
    }


def blank() -> dict[str, Any]:
    """A new app: start → model → end, runnable as it is."""
    return {
        "schema_version": 1,
        "env": {"vars": [], "secrets": []},
        "graph": {
            "nodes": [
                {
                    "id": "start",
                    "type": "start",
                    "title": "开始",
                    "inputs": [
                        {"key": "question", "label": "问题", "type": "textarea", "required": True}
                    ],
                },
                {
                    "id": "answer",
                    "type": "llm",
                    "title": "回答",
                    "model": None,
                    "skills": [],
                    "prompt": "{{start.question}}",
                    "output": {"kind": "text"},
                },
                {
                    "id": "end",
                    "type": "end",
                    "title": "结束",
                    "outputs": [{"key": "answer", "from": ["{{answer.text}}"]}],
                },
            ],
            "edges": [{"from": "start", "to": "answer"}, {"from": "answer", "to": "end"}],
        },
    }


def _knowledge_qa() -> dict[str, Any]:
    config = blank()
    config["graph"]["nodes"].insert(
        1,
        {
            "id": "kb",
            "type": "knowledge",
            "title": "检索资料",
            "knowledge_bases": [],
            "query": "{{start.question}}",
            "top_k": 5,
            "min_score": 0.3,
        },
    )
    config["graph"]["nodes"][2]["prompt"] = (
        "根据下面的资料回答问题，用 [n] 标注出处。资料里没有答案就直说，不要编造。\n\n"
        "资料：\n{{kb.context}}\n\n问题：{{start.question}}"
    )
    config["graph"]["edges"] = [
        {"from": "start", "to": "kb"},
        {"from": "kb", "to": "answer"},
        {"from": "answer", "to": "end"},
    ]
    return config


def _knowledge_agent() -> dict[str, Any]:
    config = blank()
    config["graph"]["nodes"][1] = {
        "id": "agent",
        "type": "agent",
        "title": "检索资料并回答",
        "model": None,
        "skills": [],
        "task": (
            "先检索知识库里的资料，再回答问题：{{start.question}}\n"
            "用 [n] 标注出处；资料里没有答案就直说，不要编造。"
        ),
        "tools": [],
        "knowledge": {"knowledge_bases": [], "top_k": 5, "min_score": 0.3},
        "max_steps": 6,
    }
    config["graph"]["nodes"][-1]["outputs"] = [{"key": "answer", "from": ["{{agent.text}}"]}]
    config["graph"]["edges"] = [
        {"from": "start", "to": "agent"},
        {"from": "agent", "to": "end"},
    ]
    return config


PLAN_PROMPT = """需求：{{start.request}}
期望的代码语言：{{start.language}}

源表（只能使用这些）：
{{profile.description}}

公司数据口径（参考资料）：
{{glossary.context}}

数仓开发规范（参考资料）：
{{standards.context}}

需求里的指标和口径资料有关时按口径定义；目标表的分层和命名、主键、字段类型、质量规则和加载方式\
按开发规范。在 summary、assumptions、目标表和字段说明里用 [n] 标注依据；没有资料依据的内容不要\
编号。资料和需求冲突时以需求为准，并写进 assumptions。

上一版方案（第一次出方案时为空，忽略）：{{check_plan.plan}}
平台校验发现的问题（为空时忽略）：{{check_plan.problems}}
用户的修改意见（为空时忽略）：{{review.comment}}
有上一版方案时，在它的基础上按问题和意见修改，输出完整方案。"""

CODEGEN_PROMPT = """已确认的方案：
{{check_plan.plan}}

源表：
{{profile.description}}

开发规范（参考资料）：
{{standards.context}}

上一版代码（第一次写时为空，忽略）：{{check_code.code}}
静态检查发现的问题（为空时忽略）：{{check_code.problems}}
Databricks dev 运行的报错（为空时忽略）：{{dev_run.errors}}
有上一版代码时，按问题和报错修改，输出全部目标表的完整代码。"""

# The standards are needed in full every time, whatever the request says: the query
# is the documents' subjects and top_k takes nearly every chunk.
STANDARDS_QUERY = "分层与命名、主键与字段类型、数据质量规则、加载策略、PySpark 编码规范"


def _retry(times: int, interval_seconds: int) -> dict[str, Any]:
    """Failures that may pass by themselves (a connector or the cluster busy, the model
    gateway timing out) are tried again before the run stops."""
    return {"retry": {"max": times, "interval_seconds": interval_seconds}, "then": "stop"}


def _etl_lakehouse() -> dict[str, Any]:
    """Profile a source, plan by the company's metric definitions, have a person
    confirm, copy the sources into Databricks, write code by the warehouse standards
    and run it on dev (docs/api/data-nodes.md)."""
    # Imported here: the ETL modules are not needed to list the other templates.
    from aep.etl import codegen, plan

    retrieval = {"mode": "hybrid", "vector_weight": 0.7, "rerank": None}
    nodes = [
        {
            "id": "start",
            "type": "start",
            "title": "开始",
            "note": "运行时填写：选数据源，用业务语言写需求（不用提表名和字段），"
            "表范围写成 schema.* 或 schema.table，留空就是全部表。",
            "inputs": [
                {"key": "datasource", "label": "数据源", "type": "datasource", "required": True},
                {"key": "request", "label": "需求", "type": "textarea", "required": True},
                {"key": "tables", "label": "表范围", "type": "text"},
                {
                    "key": "language",
                    "label": "代码语言",
                    "type": "select",
                    "default": "pyspark",
                    "choices": [
                        {"value": "pyspark", "label": "PySpark"},
                        {"value": "sql", "label": "SQL"},
                    ],
                },
                {"key": "name", "label": "bundle 名", "type": "text"},
            ],
        },
        {
            "id": "profile",
            "type": "etl_profile",
            "title": "探查数据",
            "note": "先看真实数据再出方案。平台经只读的 MCP 连接器读每张表的结构、主外键、"
            "行数、空值率、取值分布和样本行；后面出方案和校验都以它为准，"
            "这里没出现的表和列，方案里就不能用。",
            "datasource": "{{start.datasource}}",
            "tables": "{{start.tables}}",
            "sample_rows": 5,
            "on_error": _retry(2, 5),
        },
        {
            "id": "glossary",
            "type": "knowledge",
            "title": "查业务口径",
            "note": "按需求检索「零售数据口径」知识库。“有效订单”“销售额”“城市怎么归一”"
            "这些规则只写在业务部门的口径文档里，模型自己猜不出来；"
            "检索到的片段带 [n] 编号，方案引用时会标出出处。",
            "knowledge_bases": [],
            "query": "{{start.request}}",
            "top_k": 12,
            "min_score": 0.2,
            "retrieval": retrieval,
        },
        {
            "id": "standards",
            "type": "knowledge",
            "title": "查开发规范",
            "note": "检索「数仓开发规范」知识库：分层和命名、主键和字段类型、数据质量规则、"
            "加载策略、PySpark 写法。出方案时按它定表名、主键、质量规则和加载方式，"
            "写代码时按它的写法。规范每次都要查全，所以检索词是固定的，不随需求变。",
            "knowledge_bases": [],
            "query": STANDARDS_QUERY,
            "top_k": 20,
            "min_score": 0.2,
            "retrieval": retrieval,
        },
        {
            "id": "plan",
            "type": "llm",
            "title": "出方案",
            "note": "模型按 Skill etl-plan 的做法，结合需求、探查结果、口径和开发规范写 ETL 方案："
            "目标表、列映射、清洗和质量规则、需要人确认的假设。"
            "系统提示词是平台规定的输出格式，Skill 管怎么做，两者分开维护。",
            "model": None,
            "skills": [{"name": "etl-plan", "version": 1}],
            "system": plan.OUTPUT_FORMAT,
            "prompt": PLAN_PROMPT,
            "output": {"kind": "text"},
            "max_visits": 10,
            "on_error": _retry(1, 2),
        },
        {
            "id": "check_plan",
            "type": "etl_plan_check",
            "title": "校验方案",
            "note": "模型可能编出数据里没有的表、列或取值，平台在这里逐条对照探查结果，"
            "不过就带着问题让模型重写，连续 3 次不过才停下。"
            "通过后把方案整理成给人看的格式，列出引用的资料。",
            "reply": "{{plan.text}}",
            "profile": "{{profile.tables}}",
            "hits": ["{{glossary.hits}}", "{{standards.hits}}"],
            "language": "{{start.language}}",
            "max_attempts": 3,
            "max_visits": 10,
        },
        {
            "id": "review",
            "type": "human",
            "title": "确认方案",
            "note": "人工确认点：看目标表、口径依据和假设，没问题就确认；不满意就驳回并写意见，"
            "模型按意见重写方案。确认之前不会动 Databricks 里的任何数据。",
            "mode": "approval",
            "prompt": "请检查方案：目标表和字段、带 [n] 的口径依据、清洗规则和假设。"
            "确认后平台会把方案用到的源表抽取到 Databricks；驳回时写下修改意见。",
            "show": "{{check_plan.message}}",
            "options": [
                {"key": "approve", "label": "确认"},
                {"key": "reject", "label": "驳回"},
            ],
            "comment_required": ["reject"],
            "max_visits": 5,
        },
        {
            "id": "ingest",
            "type": "databricks_ingest",
            "title": "抽取到湖仓",
            "note": "确认后才搬数据：只把方案用到的源表整表复制到 Databricks，"
            "经 Unity Catalog Volume 落地，再在已有集群上建表，写到源表 catalog 下同名的 schema。"
            "表注释里记着来源和这次运行，方便追溯。",
            "datasource": "{{start.datasource}}",
            "tables": "{{check_plan.sources}}",
            "max_rows": 200000,
            "on_error": _retry(2, 5),
        },
        {
            "id": "codegen",
            "type": "llm",
            "title": "写代码",
            "note": "模型按 Skill etl-codegen 和开发规范，为每张目标表写 PySpark 转换代码"
            "（函数体）。后面的检查或 dev 运行不通过时，会带着问题和报错回到这里改。",
            "model": None,
            "skills": [{"name": "etl-codegen", "version": 1}],
            "system": codegen.OUTPUT_FORMAT,
            "prompt": CODEGEN_PROMPT,
            "output": {"kind": "text"},
            "max_visits": 10,
            "on_error": _retry(1, 2),
        },
        {
            "id": "check_code",
            "type": "etl_code_check",
            "title": "检查并打包",
            "note": "平台静态检查代码：只能读方案里列出的源表，不许写表、不许 collect、"
            "不许执行任意 SQL 或访问文件系统。通过后生成 Databricks bundle："
            "管道代码、Job 配置和按方案生成的质量规则。",
            "reply": "{{codegen.text}}",
            "plan": "{{check_plan.plan}}",
            "source_name": "{{profile.source_name}}",
            "name": "{{start.name}}",
            "max_attempts": 3,
            "max_visits": 10,
        },
        {
            "id": "dev_run",
            "type": "databricks_dev_run",
            "title": "dev 运行",
            "note": "把 bundle 部署成已有集群上的 Job，在 dev 目标运行，收集每张表的行数和"
            "质量规则结果，读出结果表的前 20 行。能修的报错带回去重写代码，最多运行 3 次。",
            "bundle": "{{check_code.bundle}}",
            "max_runs": 3,
            "preview_rows": 20,
            "max_visits": 3,
            "on_error": _retry(2, 5),
        },
        {
            "id": "end",
            "type": "end",
            "title": "结束",
            "note": "输出运行报告：抽取了哪些表、写出哪些表和行数、质量规则结果、"
            "Databricks 链接和结果数据预览，以及最终确认的方案。",
            "outputs": [
                {"key": "report", "from": ["{{dev_run.report}}"]},
                {"key": "plan", "from": ["{{check_plan.plan}}"]},
                {"key": "ingested", "from": ["{{ingest.summary}}"]},
            ],
        },
    ]
    edges = [
        ("start", None, "profile"),
        ("profile", None, "glossary"),
        ("glossary", None, "standards"),
        ("standards", None, "plan"),
        ("plan", None, "check_plan"),
        ("check_plan", "ok", "review"),
        ("check_plan", "retry", "plan"),
        ("review", "approve", "ingest"),
        ("review", "reject", "plan"),
        ("ingest", None, "codegen"),
        ("codegen", None, "check_code"),
        ("check_code", "ok", "dev_run"),
        ("check_code", "retry", "codegen"),
        ("dev_run", "passed", "end"),
        ("dev_run", "retry", "codegen"),
        ("dev_run", "failed", "end"),
    ]
    return {
        "schema_version": 1,
        "env": {"vars": [], "secrets": []},
        "graph": {
            "nodes": nodes,
            "edges": [
                {"from": a, "to": b} | ({"branch": branch} if branch else {})
                for a, branch, b in edges
            ],
        },
    }


@dataclass(frozen=True)
class Template:
    id: str
    title: str
    summary: str
    config: dict[str, Any]
    needs: tuple[str, ...] = ()

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "needs": list(self.needs),
            "config": copy.deepcopy(self.config),
        }


TEMPLATES = {
    t.id: t
    for t in (
        Template(
            "qa-approval",
            "问答加确认",
            "模型回答问题，请人确认；驳回时按意见重写。",
            _qa_approval(),
        ),
        Template(
            "intent-routing",
            "意图分类路由",
            "按问题类型分流：售后问题起草回复并请客服确认，产品咨询直接回答，其他问题转人工。",
            _intent_routing(),
        ),
        Template(
            "datasource-qa",
            "数据源问答",
            "智能体调用所选数据源的只读工具查数据，再回答问题。",
            _datasource_qa(),
            needs=("datasource",),
        ),
        Template(
            "knowledge-qa",
            "知识问答",
            "检索知识库里的资料，由模型回答问题并标注出处。",
            _knowledge_qa(),
            needs=("knowledge_base",),
        ),
        Template(
            "knowledge-agent",
            "知识库智能体",
            "智能体按需检索知识库里的资料，回答问题并标注出处。",
            _knowledge_agent(),
            needs=("knowledge_base",),
        ),
        Template(
            "etl-lakehouse",
            "数据 ETL（湖仓）",
            "探查数据源、按口径知识库出方案、经人确认后抽取到 Databricks，"
            "按开发规范写 PySpark 代码并在 dev 运行，预览结果数据。",
            _etl_lakehouse(),
            needs=("datasource", "knowledge_base"),
        ),
    )
}


@dataclass(frozen=True)
class BuiltinFlow:
    """A flow app the platform ships; its config is the app's version 1."""

    title: str
    summary: str
    config: dict[str, Any]

    def upgrade(self, config: dict[str, Any]) -> dict[str, Any]:
        """A configuration saved before the app was a flow app (a code app's
        ``{"nodes": {...}}``), carried over: model and skills per node."""
        if "graph" in config or "nodes" not in config:
            return config
        out = copy.deepcopy(self.config)
        for node in out["graph"]["nodes"]:
            old = (config.get("nodes") or {}).get(node["id"])
            if old and node["type"] in ("llm", "classifier"):
                node["model"] = old.get("model")
                node["skills"] = list(old.get("skills") or [])
        return out


HELLO = BuiltinFlow("问答示例", "用模型回答一个问题，请人确认；驳回时按意见重写。", _qa_approval())
