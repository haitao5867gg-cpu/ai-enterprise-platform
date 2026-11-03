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
不要编号。资料和需求不一致时不要自己选：需求明确说这次要改掉资料里的做法的，按需求写，写进 rules \
并说明和资料哪里不同；否则写成 questions，列出资料的写法和需求的说法，方案先按资料写。\
资料里写明还没定、暂不实现或要另行确认的事，即使需求明确要做，也写成 questions，\
选项至少有“按需求做”和“按资料，先不做”，方案先按资料写。\
需求没说清、要人拍板的地方写成 questions，方案按建议的选项写。\
需求里要的东西（一条检查、一列、一种算法）平台不支持、校验不通过时，不要悄悄删掉或换掉：\
写成 questions，说明需求要的是什么、平台能怎么做、建议怎么替代，方案按建议的替代做法写。

上一版方案（第一次出方案时为空，忽略）：{{check_plan.plan}}
平台校验发现的问题（为空时忽略）：{{check_plan.problems}}
校验连续没通过、交给人时人的意见（为空时忽略）：{{plan_stuck.feedback}}
确认方案时的意见（为空时忽略）：{{review.feedback}}
审核试算结果时的意见（为空时忽略）：{{data_review.feedback}}
意见开头写着第几轮、哪一步、什么决定。校验连续没通过时人的意见是针对上一版方案和这些问题写的，\
按它和问题一起改，只改格式和写法，不要为了通过校验改口径。两段意见都有时，\
确认方案时的意见是最新的，两者冲突时以它为准；审核试算结果时的意见上一版方案已经按它改过。只处理最新一轮、和方案有关的意见；\
上一版方案里已经按它改过的，不要重复处理。有上一版方案时，在它的基础上按问题和意见修改，\
输出完整方案。"""

CODEGEN_PROMPT = """已确认的方案：
{{check_plan.plan}}

源表：
{{profile.description}}

公司数据口径（参考资料）：
{{glossary.context}}

开发规范（参考资料）：
{{standards.context}}

代码按已确认的方案写；方案里的口径、判断的先后顺序和边界情况没写全时，按口径资料里的定义写，\
不要自己发挥。

上一版代码（第一次写时为空，忽略）：{{check_code.code}}
静态检查发现的问题（为空时忽略）：{{check_code.problems}}
检查连续没通过、交给人时人的意见（为空时忽略）：{{code_stuck.feedback}}
Databricks 开发环境试算的报错（为空时忽略）：{{dev_run.errors}}
审核试算结果时的意见（为空时忽略）：{{data_review.feedback}}
意见开头写着第几轮、哪一步、什么决定。只处理和代码有关的意见；上一版代码里已经按它改过的，\
不要重复处理。有上一版代码时，按问题、报错和意见修改，输出全部目标表的完整代码。"""

# The standards are needed in full every time, whatever the request says: the query
# is the documents' subjects and top_k takes nearly every chunk.
STANDARDS_QUERY = "分层与命名、主键与字段类型、数据质量规则、加载策略、PySpark 编码规范"


def _stuck_node(
    node_id: str, title: str, what: str, show: str, data: dict[str, str]
) -> dict[str, Any]:
    """The person a check hands over to when the model's replies failed it too often in a
    row (the check's ``on_stuck: branch``): rewrite with their comment, or stop."""
    return {
        "id": node_id,
        "type": "human",
        "title": title,
        "note": f"人工兜底：模型连续 3 次没写出能通过平台检查的{what}时，不直接报错终止，"
        f"把最后一版{what}和问题交给人。写意见让模型按意见重写（重写次数重新算），或者终止。",
        "mode": "approval",
        "prompt": f"模型连续几次没写出能通过平台检查的{what}。请看问题和最后一版{what}："
        f"写明怎么改，让模型按意见重写；或者终止这次运行并写明原因。",
        "show": show,
        "data": data,
        "options": [
            {"key": "rewrite", "label": "按意见重写"},
            {"key": "stop", "label": "终止"},
        ],
        "comment_required": ["rewrite", "stop"],
        "max_visits": 3,
    }


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
            "不过就带着问题让模型重写，连续 3 次不过就交给人：按意见重写或终止。"
            "平台还会先自动整理：去掉业务文字里的代码和英文前缀，第 6 题起和已经答过的题改成规则。"
            "通过后把方案整理成给人看的格式，列出引用的资料。",
            "reply": "{{plan.text}}",
            "profile": "{{profile.tables}}",
            "hits": ["{{glossary.hits}}", "{{standards.hits}}"],
            "language": "{{start.language}}",
            "format": 2,
            "answered": "{{review.record}}",
            "max_attempts": 3,
            "on_stuck": "branch",
            "max_visits": 10,
        },
        _stuck_node(
            "plan_stuck",
            "方案校验没通过",
            "方案",
            "{{check_plan.message}}",
            {"problems": "{{check_plan.problems}}", "plan": "{{check_plan.plan}}"},
        ),
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
                "dropped": "{{check_plan.dropped}}",
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
            "on_stuck": "branch",
            "max_visits": 10,
        },
        _stuck_node(
            "code_stuck",
            "代码检查没通过",
            "代码",
            "{{check_code.summary}}",
            {"problems": "{{check_code.problems}}", "code": "{{check_code.code}}"},
        ),
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
            "id": "release_review",
            "type": "human",
            "title": "上线审批",
            "note": "人工审批点：试算结果审核通过后，决定要不要把代码上线到 prod，"
            "以及上线后多久自动刷新一次。上线失败时回到这里，看了原因再试，或者先不上线。",
            "mode": "approval",
            "prompt": "试算结果已审核通过。上线后，prod 环境按选的时间自动刷新结果表；"
            "先不上线就只交付开发环境的结果。",
            "show": "{{check_code.summary}}\n\n{{release.report}}",
            "fields": [
                {
                    "key": "schedule",
                    "label": "上线后什么时候自动刷新？",
                    "type": "select",
                    "choices": [
                        {"value": "0 0 2 * * ?", "label": "每天 02:00"},
                        {"value": "0 0 * * * ?", "label": "每小时整点"},
                        {"value": "0 0 2 ? * MON", "label": "每周一 02:00"},
                        {"value": "0 0 2 1 * ?", "label": "每月 1 日 02:00"},
                    ],
                    "suggested": "0 0 2 * * ?",
                    # Only asked of someone releasing (jadeflow retest I-19).
                    "only_for": ["release"],
                }
            ],
            "options": [
                {"key": "release", "label": "上线到 prod"},
                {"key": "skip", "label": "先不上线，交付"},
            ],
            "max_visits": 3,
        },
        {
            "id": "release",
            "type": "databricks_release",
            "title": "上线到 prod",
            "note": "把审核过的 bundle 部署成 prod 上的 Job（绑定项目时是项目的 prod 环境），"
            "按上线审批选的时间由 Databricks 自动刷新；不会马上运行。",
            "bundle": "{{check_code.bundle}}",
            "schedule": "{{release_review.values}}",
            "environment": "prod",
        },
        {
            "id": "end",
            "type": "end",
            "title": "交付",
            "note": "交付审核通过的结果：答案、业务说明、方案、代码、运行报告和质量小结，"
            "以及确认方案和审核试算结果的记录。选了上线时代码已部署到 prod，按选的时间自动刷新；"
            "没选时数据还在开发环境。",
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
                {"key": "release", "from": ["{{release.summary}}"]},
                {"key": "release_url", "from": ["{{release.url}}"]},
            ],
        },
        {
            "id": "stopped",
            "type": "end",
            "title": "终止",
            "note": "审核时，或者校验连续没通过、交给人时选了终止：运行停在这里，记下原因、"
            "业务说明、方案、运行报告和人工处理的记录。没通过审核的数字和代码不作为结果交付。",
            "outcome": "stopped",
            "outputs": [
                {
                    "key": "reason",
                    "from": [
                        "{{data_review.comment}}",
                        "{{data_review.feedback}}",
                        "{{code_stuck.comment}}",
                        "{{plan_stuck.comment}}",
                    ],
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
        ("check_plan", "stuck", "plan_stuck"),
        ("plan_stuck", "rewrite", "plan"),
        ("plan_stuck", "stop", "stopped"),
        ("review", "approve", "ingest"),
        ("review", "revise", "plan"),
        ("ingest", None, "codegen"),
        ("codegen", None, "check_code"),
        ("check_code", "ok", "dev_run"),
        ("check_code", "retry", "codegen"),
        ("check_code", "stuck", "code_stuck"),
        ("code_stuck", "rewrite", "codegen"),
        ("code_stuck", "stop", "stopped"),
        ("dev_run", "passed", "data_review"),
        ("dev_run", "failed", "data_review"),
        ("dev_run", "retry", "codegen"),
        ("data_review", "approve", "release_review"),
        ("release_review", "release", "release"),
        ("release_review", "skip", "end"),
        ("release", "deployed", "end"),
        ("release", "failed", "release_review"),
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


PROJECT_PLAN_PROMPT = PLAN_PROMPT.replace(
    "期望的代码语言：{{start.language}}\n",
    "\n项目说明（环境、表名前缀、引用表、运行参数、约定）：\n{{project.brief}}\n",
).replace(
    "{{profile.description}}\n\n",
    "{{profile.description}}\n\n本项目在环境里已有的表（不是源表）：\n"
    "{{profile.existing_description}}\n\n",
)
PROJECT_PLAN_PROMPT = PROJECT_PLAN_PROMPT.replace(
    "平台校验发现的问题（为空时忽略）：{{check_plan.problems}}\n",
    "平台校验发现的问题（为空时忽略）：{{check_plan.problems}}\n"
    "模型审查发现的问题（为空时忽略；要逐条改掉，要写成问题的写进 questions）："
    "{{review_plan.problems}}\n",
)
PROJECT_CODEGEN_PROMPT = (
    "项目说明（表名前缀、引用表、运行参数、约定）：\n{{project.brief}}\n\n"
    + CODEGEN_PROMPT.replace(
        "静态检查发现的问题（为空时忽略）：{{check_code.problems}}\n",
        "静态检查发现的问题（为空时忽略）：{{check_code.problems}}\n"
        "模型审查发现的问题（为空时忽略，要逐条改掉）：{{review_code.problems}}\n",
    )
)
# What the plan of a project's run is reviewed against (jadeflow 4th retest I-12, S-22,
# I-10), besides the project's own conventions for plans. Prompt rules alone were not
# followed; a review that asks only these is.
PLAN_REVIEW_RULES = [
    {
        "key": "open_items",
        "title": "资料写明还没定的事要写成问题",
        "instruction": "参考资料里写明还没定、暂不实现、试点不做、要另行确认或开发时再确认的事，"
        "方案的规则、指标、筛选或技术方案对它做了决定（不管依据写的是需求、资料还是数据），"
        "questions 里却没有对应的问题，就是违反。where 写方案里做决定的位置（如 规则 r5），"
        "quote 写资料里说它还没定的原话。",
    },
    {
        "key": "data_rules",
        "title": "按数据自己加的处理规则要写成问题",
        "instruction": "依据（basis）是 data 的规则，会删掉行、改掉值或改变口径（不只是描述数据"
        "现状），资料和需求里都没有这样要求，questions 里又没有对应的问题，就是违反。",
    },
    {
        "key": "checks_hold",
        "title": "建议的检查在结果正确时要能通过",
        "instruction": "方案建议的每条数据检查（checks），在结果完全正确时也要能通过。资料里写明的"
        "例外（不分摊的行、允许的负数、只在部分月份适用的组合等）检查没有排除，比较的两边按资料"
        "本来就不相等，或者标题和条件说的不是一回事，就是违反。where 写检查的 id。",
    },
]


def _etl_lakehouse_project() -> dict[str, Any]:
    """Template etl-lakehouse version 3 (docs/api/data-nodes.md, 第三批): version 2 for a
    data team working in a project. The run is bound to a project; the plan follows its
    table prefix, reference tables and run parameters and says how each table is written
    and what to check; after the dev run the data is checked through the Databricks
    source's read-only connector, against the old system's result when one is named."""
    from aep.etl import plan

    config = _etl_lakehouse()
    nodes = config["graph"]["nodes"]
    found = {node["id"]: node for node in nodes}
    found["start"] |= {
        "project": "required",
        "note": "运行时选项目，再选 Databricks 数据源（数据检查也经它读表），用业务语言写需求。"
        "表范围空着时探查项目的引用表；填了老系统的结果表（catalog.schema.table），"
        "数据检查时会和它逐行对比。",
        "inputs": [
            {"key": "datasource", "label": "数据源", "type": "datasource", "required": True},
            {"key": "request", "label": "需求", "type": "textarea", "required": True},
            {"key": "tables", "label": "表范围", "type": "text", "advanced": True},
            {"key": "golden", "label": "老系统结果表", "type": "text", "advanced": True},
        ],
    }
    found["profile"]["note"] = (
        "先看真实数据再出方案。表范围空着时探查项目登记的引用表，带读取筛选的表只看筛选后的行；"
        "平台经只读的 MCP 连接器读每张表的结构、行数、空值率、取值分布和样本行，"
        "这里没出现的表和列，方案里就不能用。"
    )
    found["plan"] |= {
        "note": "模型按 Skill etl-plan-project 的做法写方案：业务说明、目标表和列映射、规则、"
        "要人拍板的问题和答案之外，目标表名按项目的表名前缀，每张表写明写表方式（整表覆盖、"
        "按条件替换某个月、追加或按键合并），并建议试算后要做的数据检查。",
        "skills": [{"name": "etl-plan-project", "version": 1}],
        "system": plan.OUTPUT_FORMAT_V3,
        "prompt": PROJECT_PLAN_PROMPT,
    }
    found["check_plan"] |= {
        "note": "平台逐条对照探查结果校验方案，另外检查表名前缀、写表方式、运行参数"
        "和建议的数据检查；写到已有的表时，列出要在表里加哪些列、加在哪。"
        "不过就带着问题让模型重写，连续 3 次不过就交给人：按意见重写或终止。",
        "format": 3,
        "existing": "{{profile.existing}}",
    }
    del found["check_plan"]["language"]
    found["review"] |= {
        "note": "人工确认点：看业务说明、技术方案、写表方式、已按口径处理的规则和建议的数据检查，"
        "回答每道问题。都按建议答、没写意见就确认，开始写代码；改了建议答案或写了意见，"
        "模型按意见重写方案，再确认一次。确认之前不会写 Databricks 里的任何表。",
        "prompt": "请检查方案：业务说明（指标、维度、筛选、时间范围）、技术方案、写表方式、"
        "已按口径处理的规则和建议的数据检查，并回答每道问题。确认后平台写代码，在项目的"
        "开发环境试算；改了建议答案或写了意见，平台会按意见重写方案，再请你确认一次。",
    }
    found["ingest"]["note"] = (
        "Databricks 上的表直接读，不复制；数据库或上传文件的表才整表复制到 Databricks。"
    )
    found["codegen"] |= {
        "note": "模型按 Skill etl-codegen-project 和开发规范写 PySpark 转换代码：用 source() "
        "读源表（项目的读取筛选平台自动加上），用 param() 取运行参数。",
        "skills": [{"name": "etl-codegen-project", "version": 1}],
        "prompt": PROJECT_CODEGEN_PROMPT,
    }
    del found["check_code"]["name"]
    # The plan and the code are reviewed by another model call before a person or the
    # cluster sees them (jadeflow 4th retest I-12, I-18): problems go back to the model;
    # after 2 reviews in a row with problems the run goes on and the person sees them.
    review_plan = {
        "id": "review_plan",
        "type": "llm_review",
        "title": "审查方案",
        "note": "另调一次模型，只按清单审查方案：资料写明还没定的事有没有写成问题，按数据自己加的"
        "会删行改值的规则有没有写成问题，建议的检查在结果正确时能不能通过，以及项目写方案时的约定。"
        "有问题就让模型带着问题重写；连续 2 次没改过来就放行，把问题列在确认页上。",
        "model": None,
        "skills": [],
        "subject": "{{check_plan.plan}}",
        "context": "需求：{{start.request}}\n\n{{project.brief}}\n\n公司数据口径（参考资料）：\n"
        "{{glossary.context}}\n\n源表：\n{{profile.description}}",
        "rules": [*PLAN_REVIEW_RULES, "{{project.plan_rules}}"],
        "max_attempts": 2,
        "on_stuck": "pass",
        "max_visits": 20,
        "on_error": _retry(1, 2),
    }
    nodes.insert(nodes.index(found["check_plan"]) + 1, review_plan)
    review_code = {
        "id": "review_code",
        "type": "llm_review",
        "title": "审查代码",
        "note": "按项目写代码时的约定审查代码（项目没写约定时跳过，不调模型）。有问题就让模型带着"
        "问题重写；连续 2 次没改过来就照常试算，把问题列在审核页上。",
        "model": None,
        "skills": [],
        "subject": "{{check_code.code}}",
        "context": "已确认的方案：\n{{check_plan.plan}}\n\n{{project.brief}}",
        "rules": ["{{project.code_rules}}"],
        "max_attempts": 2,
        "on_stuck": "pass",
        "max_visits": 20,
        "on_error": _retry(1, 2),
    }
    nodes.insert(nodes.index(found["check_code"]) + 1, review_code)
    # A rewrite for a reviewer is one more visit of the step it goes back to.
    for node in (found["plan"], found["check_plan"], found["codegen"], found["check_code"]):
        node["max_visits"] = 20
    found["review"]["show"] = "{{review_plan.report}}\n\n{{check_plan.message}}"
    found["review"]["data"]["review"] = "{{review_plan.findings}}"
    # What the rewrite changed since the person last saw the plan, and what they did not
    # ask for (jadeflow 4th retest I-3).
    found["review"]["changes"] = {
        "of": "{{check_plan.plan}}",
        "lists": [
            {"path": "checks", "key": "id", "label": "label", "name": "检查"},
            {"path": "targets", "key": "name", "label": "name", "name": "目标表"},
            {"path": "brief.metrics", "key": "name", "label": "name", "name": "指标"},
            {"path": "rules", "key": "text", "label": "text", "name": "规则"},
        ],
    }
    found["dev_run"]["note"] = (
        "把 bundle 部署成已有集群上的 Job，在项目的开发环境运行：结果写到环境的 catalog 和 schema，"
        "只写项目自己的表，按方案的写表方式写。能修的报错带回去重写代码，每轮最多运行 3 次。"
    )
    checks = {
        "id": "checks",
        "type": "data_check",
        "title": "数据检查",
        "note": "试算通过后经 Databricks 只读连接器核对数据：项目的验收检查和方案建议的检查"
        "（对账、键不重复、不该有的行），填了老系统结果表时再逐行对比。试算没通过时不检查。",
        "datasource": "{{start.datasource}}",
        "include": ["{{project.checks}}", "{{check_plan.checks}}"],
        "golden": {
            "expected": "{{start.golden}}",
            "table": "{{check_plan.result_table}}",
            "keys": "{{check_plan.result_keys}}",
        },
        "only_if": "{{dev_run.ok}}",
        "max_visits": 9,
        "on_error": _retry(2, 5),
    }
    nodes.insert(nodes.index(found["data_review"]), checks)
    review = found["data_review"]
    review["note"] = (
        "人工审核点：看试算结果、数据检查、代码和方案。试算和数据检查都通过才能选“通过”；"
        "数字或代码不对时选改代码或改方案并写明意见；不做了就终止并写明原因。"
    )
    review["prompt"] = (
        "请看开发环境试算的结果、数据检查、代码和方案。通过后交付；改代码、改方案时写明意见，"
        "可以挂在某一列、某条质量规则、某条数据检查或某个文件上；终止时写明原因。"
    )
    review["show"] = "{{review_code.report}}\n\n{{dev_run.report}}\n\n{{checks.report}}"
    review["data"] |= {
        "checks": "{{checks.results}}",
        "checks_summary": "{{checks.summary}}",
        "code_review": "{{review_code.findings}}",
    }
    review["options"][0]["requires"] = ["{{dev_run.ok}}", "{{checks.ok}}"]
    for end in (found["end"], found["stopped"]):
        end["outputs"].append({"key": "checks", "from": ["{{checks.report}}"]})
    # Releasing (jadeflow retest I-19): only asked when the project has a prod environment
    # the run is not in; it says where it goes, the parameters of the scheduled runs and
    # how to take it back, and is offered only when every parameter can be filled in.
    can_release = {
        "id": "can_release",
        "type": "condition",
        "title": "能不能上线",
        "note": "项目配了 prod 环境、这次不是在 prod 里试算时，才请人决定要不要上线；"
        "否则直接交付开发环境的结果。",
        "rules": [
            {
                "key": "yes",
                "label": "项目有 prod 环境",
                "match": "all",
                "conditions": [{"left": "{{project.can_release}}", "op": "eq", "right": True}],
            }
        ],
    }
    nodes.insert(nodes.index(found["release_review"]), can_release)
    release_review = found["release_review"]
    release_review["note"] = (
        "人工审批点：试算结果审核通过、项目有 prod 环境时，决定要不要上线到项目的 prod 环境，"
        "以及多久自动刷新一次（只在选上线时要答）。上线说明写明去哪、定时运行时的参数怎么取、"
        "怎么撤销；有参数取不到时不能上线。上线失败时回到这里，看了原因再试，或者先不上线。"
    )
    release_review["prompt"] = (
        "试算结果已审核通过。请看上线说明：上线后 prod 按选的时间自动运行，每次的参数按说明里的"
        "规则取；先不上线就只交付开发环境的结果，不用选刷新时间。"
    )
    release_review["show"] = (
        "{{check_code.summary}}\n\n{{project.release_brief}}\n\n{{release.report}}"
    )
    release_review["options"][0]["requires"] = "{{project.release_ready}}"
    found["release"]["note"] = (
        "把审核过的 bundle 部署成项目 prod 环境上的 Job，按上线审批选的时间由 Databricks 自动"
        "运行，不会马上运行；每次运行的参数按项目写的定时取法算，算出的值不符合参数格式时"
        "那次不运行。"
    )
    # The dev run goes on to the checks, which go on to the review, in reading order.
    edges: list[dict[str, Any]] = []
    for edge in config["graph"]["edges"]:
        if edge["from"] == "dev_run" and edge["to"] == "data_review":
            edges.append(edge | {"to": "checks"})
            if edge.get("branch") == "failed":
                edges += [
                    {"from": "checks", "to": "data_review", "branch": "passed"},
                    {"from": "checks", "to": "data_review", "branch": "failed"},
                ]
        elif edge["from"] == "check_plan" and edge.get("branch") == "ok":
            edges += [
                edge | {"to": "review_plan"},
                {"from": "review_plan", "to": "review", "branch": "ok"},
                {"from": "review_plan", "to": "plan", "branch": "retry"},
            ]
        elif edge["from"] == "check_code" and edge.get("branch") == "ok":
            edges += [
                edge | {"to": "review_code"},
                {"from": "review_code", "to": "dev_run", "branch": "ok"},
                {"from": "review_code", "to": "codegen", "branch": "retry"},
            ]
        elif edge["from"] == "data_review" and edge["to"] == "release_review":
            edges += [
                edge | {"to": "can_release"},
                {"from": "can_release", "to": "release_review", "branch": "yes"},
                {"from": "can_release", "to": "end", "branch": "else"},
            ]
        else:
            edges.append(edge)
    config["graph"]["edges"] = edges
    return config


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
            "按开发规范写 PySpark 代码，在开发环境试算并算出答案，"
            "审核通过后可以上线到 prod 定时刷新。",
            _etl_lakehouse(),
            needs=("datasource", "knowledge_base"),
        ),
        Template(
            "etl-lakehouse-project",
            "数据 ETL（湖仓，绑定项目）",
            "在数据项目里做 ETL：探查项目的引用表，按口径和开发规范出方案并写明写表方式，"
            "确认后写代码，在项目的开发环境试算，核对数据（可以和老系统逐行对比），"
            "审核通过后可以上线到 prod 定时刷新。",
            _etl_lakehouse_project(),
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
