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
按开发规范。在 brief、rules、questions、目标表和字段说明里用 [n] 标注依据；没有资料依据的内容\
不要编号。资料和需求冲突时以需求为准，并写进 rules。需求没说清、要人拍板的地方写成 questions，\
方案按建议的选项写。

上一版方案（第一次出方案时为空，忽略）：{{check_plan.plan}}
平台校验发现的问题（为空时忽略）：{{check_plan.problems}}
确认方案时的意见（为空时忽略）：{{review.feedback}}
审核试算结果时的意见（为空时忽略）：{{data_review.feedback}}
意见开头写着第几轮、哪一步、什么决定。两段意见都有时，确认方案时的意见是最新的，两者冲突时\
以它为准；审核试算结果时的意见上一版方案已经按它改过。只处理最新一轮、和方案有关的意见；\
上一版方案里已经按它改过的，不要重复处理。有上一版方案时，在它的基础上按问题和意见修改，\
输出完整方案。"""

CODEGEN_PROMPT = """已确认的方案：
{{check_plan.plan}}

源表：
{{profile.description}}

开发规范（参考资料）：
{{standards.context}}

上一版代码（第一次写时为空，忽略）：{{check_code.code}}
静态检查发现的问题（为空时忽略）：{{check_code.problems}}
Databricks 开发环境试算的报错（为空时忽略）：{{dev_run.errors}}
审核试算结果时的意见（为空时忽略）：{{data_review.feedback}}
意见开头写着第几轮、哪一步、什么决定。只处理和代码有关的意见；上一版代码里已经按它改过的，\
不要重复处理。有上一版代码时，按问题、报错和意见修改，输出全部目标表的完整代码。"""

# The standards are needed in full every time, whatever the request says: the query
# is the documents' subjects and top_k takes nearly every chunk.
STANDARDS_QUERY = "分层与命名、主键与字段类型、数据质量规则、加载策略、PySpark 编码规范"


def _retry(times: int, interval_seconds: int) -> dict[str, Any]:
    """Failures that may pass by themselves (a connector or the cluster busy, the model
    gateway timing out) are tried again before the run stops."""
    return {"retry": {"max": times, "interval_seconds": interval_seconds}, "then": "stop"}


def _etl_lakehouse() -> dict[str, Any]:
    """Profile a source, plan by the company's metric definitions, have the data team
    confirm the plan and answer its questions, copy the sources into Databricks, write
    code by the warehouse standards, try it on dev and have the result reviewed before
    it is delivered (docs/api/data-nodes.md, template version 2)."""
    # Imported here: the ETL modules are not needed to list the other templates.
    from aep.etl import codegen, plan

    retrieval = {"mode": "hybrid", "vector_weight": 0.7, "rerank": None}
    nodes = [
        {
            "id": "start",
            "type": "start",
            "title": "开始",
            "note": "运行时填写：选数据源，用业务语言写需求（不用提表名和字段）。"
            "表范围写成 schema.* 或 schema.table，留空就是全部表；"
            "表范围、代码语言和 bundle 名一般不用改。",
            "inputs": [
                {"key": "datasource", "label": "数据源", "type": "datasource", "required": True},
                {
                    "key": "request",
                    "label": "需求",
                    "type": "textarea",
                    "required": True,
                    "examples": [],
                },
                {"key": "tables", "label": "表范围", "type": "text", "advanced": True},
                {
                    "key": "language",
                    "label": "代码语言",
                    "type": "select",
                    "default": "pyspark",
                    "choices": [
                        {"value": "pyspark", "label": "PySpark"},
                        {"value": "sql", "label": "SQL"},
                    ],
                    "advanced": True,
                },
                {"key": "name", "label": "bundle 名", "type": "text", "advanced": True},
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
            "note": "模型按 Skill etl-plan-brief 的做法，结合需求、探查结果、口径和开发规范写方案："
            "先写给人看的业务说明（指标怎么算、维度、筛选、时间范围），再写目标表和列映射、"
            "已按口径处理的规则、要人拍板的问题和最后给出的答案。确认方案或审核试算结果时"
            "提了意见，就带着意见回到这里重写。系统提示词是平台规定的输出格式，"
            "Skill 管怎么做，两者分开维护。",
            "model": None,
            "skills": [{"name": "etl-plan-brief", "version": 1}],
            "system": plan.OUTPUT_FORMAT_V2,
            "prompt": PLAN_PROMPT,
            "output": {"kind": "text"},
            # The reply is JSON for the check after it (so is codegen's), not for the
            # run's messages.
            "quiet": True,
            "max_visits": 10,
            "on_error": _retry(1, 2),
        },
        {
            "id": "check_plan",
            "type": "etl_plan_check",
            "title": "校验方案",
            "note": "模型可能编出数据里没有的表、列或取值，平台在这里逐条对照探查结果，"
            "不过就带着问题让模型重写，连续 3 次不过才停下。平台还会先自动整理："
            "去掉业务文字里的代码和英文前缀，第 6 题起和已经答过的题改成规则。"
            "通过后把方案整理成给人看的格式，列出引用的资料。",
            "reply": "{{plan.text}}",
            "profile": "{{profile.tables}}",
            "hits": ["{{glossary.hits}}", "{{standards.hits}}"],
            "language": "{{start.language}}",
            "format": 2,
            "answered": "{{review.record}}",
            "max_attempts": 3,
            "max_visits": 10,
        },
        {
            "id": "review",
            "type": "human",
            "title": "确认方案",
            "note": "人工确认点：看业务说明、技术方案和已按口径处理的规则，回答每道问题。"
            "都按建议答、没写意见就确认，开始抽取数据；改了建议答案或写了意见，"
            "模型按意见重写方案，再确认一次。确认之前不会动 Databricks 里的任何数据。",
            "mode": "approval",
            "prompt": "请检查方案：业务说明（指标、维度、筛选、时间范围）、技术方案和已按口径"
            "处理的规则，并回答每道问题。确认后平台把用到的源表抽取到 Databricks；"
            "改了建议答案或写了意见，平台会按意见重写方案，再请你确认一次。",
            "view": "etl_plan_review",
            "show": "{{check_plan.message}}",
            "data": {
                "brief": "{{check_plan.brief}}",
                "plan": "{{check_plan.plan}}",
                "answer": "{{check_plan.answer}}",
                "uncited": "{{check_plan.uncited}}",
                "cleaned": "{{check_plan.cleaned}}",
                "returned": "{{data_review.record}}",
                "previous": "{{review.record}}",
            },
            "fields": "{{check_plan.questions}}",
            "changed_branch": "revise",
            "options": [
                {"key": "approve", "label": "确认方案，开始做"},
                {"key": "revise", "label": "按我的意见修改方案"},
            ],
            "comment_required": ["revise"],
            "max_visits": 5,
        },
        {
            "id": "ingest",
            "type": "databricks_ingest",
            "title": "抽取到湖仓",
            "note": "确认后才搬数据：只把方案用到的源表整表复制到 Databricks，"
            "经 Unity Catalog Volume 落地，再在已有集群上建表，写到源表 catalog 下同名的 schema。"
            "表注释里记着来源和这次运行，方便追溯。改方案后再确认时，"
            "要的表这次运行都抽取过就沿用，不再复制。",
            "datasource": "{{start.datasource}}",
            "tables": "{{check_plan.sources}}",
            "max_rows": 200000,
            "reuse": True,
            "max_visits": 6,
            "on_error": _retry(2, 5),
        },
        {
            "id": "codegen",
            "type": "llm",
            "title": "写代码",
            "note": "模型按 Skill etl-codegen 和开发规范，为每张目标表写 PySpark 转换代码"
            "（函数体）。后面的检查或试算不通过、审核要求改代码时，"
            "会带着问题、报错和意见回到这里改。",
            "model": None,
            "skills": [{"name": "etl-codegen", "version": 1}],
            "system": codegen.OUTPUT_FORMAT,
            "prompt": CODEGEN_PROMPT,
            "output": {"kind": "text"},
            "quiet": True,
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
            "title": "开发环境试算",
            "note": "把 bundle 部署成已有集群上的 Job，在开发环境运行，收集每张表的行数和"
            "质量规则结果，读出结果表的前 20 行。能修的报错带回去重写代码，每轮最多运行 3 次；"
            "试算没通过也交给人审核。",
            "bundle": "{{check_code.bundle}}",
            "plan": "{{check_plan.plan}}",
            "max_runs": 3,
            "preview_rows": 20,
            "max_visits": 9,
            "on_error": _retry(2, 5),
        },
        {
            "id": "data_review",
            "type": "human",
            "title": "审核试算结果",
            "note": "人工审核点：看试算结果、代码和方案。没问题就通过，交付结果；"
            "数字或代码不对时选改代码或改方案并写明意见，平台带着意见重做；"
            "不做了就终止并写明原因。试算没通过时没有“通过”。",
            "mode": "approval",
            "prompt": "请看开发环境试算的结果、代码和方案。通过后交付；改代码、改方案时写明意见，"
            "可以挂在某一列、某条质量规则或某个文件上；终止时写明原因。",
            "view": "etl_dev_review",
            "show": "{{dev_run.report}}",
            "data": {
                "brief": "{{check_plan.brief}}",
                "plan": "{{check_plan.plan}}",
                "uncited": "{{check_plan.uncited}}",
                "confirmed": "{{review.record}}",
                "status": "{{dev_run.status}}",
                "tables": "{{dev_run.tables}}",
                "preview": "{{dev_run.preview}}",
                "answer": "{{dev_run.answer}}",
                "quality": "{{dev_run.quality}}",
                "url": "{{dev_run.url}}",
                "errors": "{{dev_run.errors}}",
                "files": "{{check_code.files}}",
            },
            "options": [
                {"key": "approve", "label": "通过", "requires": "{{dev_run.ok}}"},
                {"key": "fix_code", "label": "改代码"},
                {"key": "fix_plan", "label": "改方案"},
                {"key": "stop", "label": "终止"},
            ],
            "comment_required": ["fix_code", "fix_plan", "stop"],
            "max_visits": 4,
        },
        {
            "id": "end",
            "type": "end",
            "title": "交付",
            "note": "交付审核通过的结果：答案、业务说明、方案、代码、运行报告和质量小结，"
            "以及确认方案和审核试算结果的记录。数据还在开发环境，没有上线。",
            "outcome": "delivered",
            "outputs": [
                {"key": "report", "from": ["{{dev_run.report}}"]},
                {"key": "plan", "from": ["{{check_plan.plan}}"]},
                {"key": "ingested", "from": ["{{ingest.summary}}"]},
                {"key": "brief", "from": ["{{check_plan.brief}}"]},
                {"key": "answer", "from": ["{{dev_run.answer}}"]},
                {"key": "code", "from": ["{{check_code.bundle}}"]},
                {"key": "quality", "from": ["{{dev_run.quality}}"]},
                {"key": "dev_url", "from": ["{{dev_run.url}}"]},
                {"key": "confirmed", "from": ["{{review.record}}"]},
                {"key": "reviewed", "from": ["{{data_review.record}}"]},
            ],
        },
        {
            "id": "stopped",
            "type": "end",
            "title": "终止",
            "note": "审核时选了终止：运行停在这里，记下原因、业务说明、方案、运行报告和两次人工处理"
            "的记录。没通过审核的数字和代码不作为结果交付。",
            "outcome": "stopped",
            "outputs": [
                {
                    "key": "reason",
                    "from": ["{{data_review.comment}}", "{{data_review.feedback}}"],
                },
                {"key": "brief", "from": ["{{check_plan.brief}}"]},
                {"key": "plan", "from": ["{{check_plan.plan}}"]},
                {"key": "report", "from": ["{{dev_run.report}}"]},
                {"key": "confirmed", "from": ["{{review.record}}"]},
                {"key": "reviewed", "from": ["{{data_review.record}}"]},
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
        ("review", "revise", "plan"),
        ("ingest", None, "codegen"),
        ("codegen", None, "check_code"),
        ("check_code", "ok", "dev_run"),
        ("check_code", "retry", "codegen"),
        ("dev_run", "passed", "data_review"),
        ("dev_run", "failed", "data_review"),
        ("dev_run", "retry", "codegen"),
        ("data_review", "approve", "end"),
        ("data_review", "fix_code", "codegen"),
        ("data_review", "fix_plan", "plan"),
        ("data_review", "stop", "stopped"),
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
            "探查数据源、按口径知识库出方案，确认方案后抽取到 Databricks，"
            "按开发规范写 PySpark 代码，在开发环境试算并算出答案，审核通过后交付。",
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
