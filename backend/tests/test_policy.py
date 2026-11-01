"""The data outbound policy: masking, what goes to models at each level, tokens turned
back and masked again, and the models a run may use (docs/api/projects.md)."""

import dataclasses
import json
from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from aep import llm, policy
from aep.databricks import DevRun
from aep.etl.plan import KnownValues, PlanError, validate_plan
from aep.etl.profile import describe_for_model
from aep.knowledge.store import MemoryKnowledgeStore
from aep.policy import DataPolicy
from aep.runtime.flow import builtin
from aep.runtime.flow.nodes import INPUT_ERROR, NodeFailure, Runtime, run_agent, run_knowledge
from aep.runtime.registry import AppContext, default_apps
from aep.runtime.service import ModelNotAllowed, RunService
from aep.runtime.store import MemoryRunStore
from tests import test_etl
from tests.conftest import SETTINGS, FakeGateway
from tests.test_flow_code import sandbox
from tests.test_flow_tools import Platform, answer, call

RUN = "run-1"
# A run id a test can give a run, to know the tokens its data gets.
RUN0 = "00000000-0000-4000-8000-000000000001"


def tok(text: str, run_id: str = RUN) -> str:
    return policy.token(run_id, text)


# Masking


def test_masking_keeps_keys_rounds_numbers_and_cuts_dates():
    masked = policy.mask(
        {
            "city": "上海",
            "same": "上海",
            "amount": 1385355.07,
            "share": 0.26,
            "quantity": -42,
            "zero": 0,
            "day": "2026-10-15",
            "at": "2026-10-01T08:30:00Z",
            "paid": True,
            "note": None,
            "tags": ["a", 7],
        },
        RUN,
    )
    assert masked == {
        "city": tok("上海"),
        "same": tok("上海"),
        "amount": 1000000,
        "share": 0.3,
        "quantity": -40,
        "zero": 0,
        "day": "2026-10",
        "at": "2026-10",
        "paid": True,
        "note": None,
        "tags": [tok("a"), 7],
    }
    assert isinstance(masked["quantity"], int)
    assert policy.TOKEN.fullmatch(tok("上海"))
    assert tok("上海") != tok("上海", "run-2")  # Stable within a run only.


def test_what_stands_for_data_left_out():
    rows = [{"order_id": 1, "city": "上海"}, {"order_id": 2, "status": "paid"}]
    said = "（这里有 2 行数据，列为 order_id、city、status；按数据外发策略没有发给模型）"
    assert policy.left_out(rows) == said
    assert policy.left_out({"schema": "sales", "rows": rows}) == said
    assert policy.left_out({"total": 3}) == "（这里有数据，按数据外发策略没有发给模型）"
    assert policy.left_out([]) == ""
    assert policy.for_model(rows, "full", RUN) is rows
    assert policy.for_model(rows, "masked", RUN)[0]["city"] == tok("上海")
    assert policy.for_model(rows, "stats", RUN) == said
    assert policy.for_model(rows, "schema", RUN) == said


def test_tokens_turn_back_and_are_masked_again():
    reply = {"filter": f"city = '{tok('上海')}'", "other": "‹文本#0000000000›", "n": 3}
    value, restored = policy.unmask(reply, [[{"city": "上海"}]], RUN)
    assert value == {"filter": "city = '上海'", "other": "‹文本#0000000000›", "n": 3}
    assert restored == {tok("上海"): "上海"}
    assert policy.unmask("没有记号", [[{"city": "上海"}]], RUN) == ("没有记号", {})

    # Chinese has no word boundaries: replaced anywhere.
    again = policy.remask(["city = '上海'", "上海市"], restored)
    assert again == [f"city = '{tok('上海')}'", f"{tok('上海')}市"]
    # ASCII text only where it does not touch a letter, digit or underscore.
    ca = {tok("CA"): "CA"}
    text = "state = 'CA' OR CAT OR CA_1 OR xCA OR (CA)"
    assert policy.remask(text, ca) == (
        f"state = '{tok('CA')}' OR CAT OR CA_1 OR xCA OR ({tok('CA')})"
    )
    assert policy.remask(text, {}) == text


def test_a_token_two_values_share_is_not_turned_back(monkeypatch):
    same = "‹文本#abcdefabcd›"
    monkeypatch.setattr(policy, "token", lambda run_id, text: same)
    value, restored = policy.unmask(f"city = '{same}'", [["上海", "北京"]], RUN)
    assert (value, restored) == (f"city = '{same}'", {})
    value, restored = policy.unmask(f"city = '{same}'", [["上海", "上海"]], RUN)
    assert (value, restored) == ("city = '上海'", {same: "上海"})


def test_the_platform_default_and_model_problems():
    settings = SETTINGS.model_copy(update={"data_samples": "stats", "allowed_models": " qwen, x "})
    rules = policy.platform_default(settings)
    assert rules == DataPolicy("stats", ("qwen", "x"))
    assert rules.as_dict() == {"samples": "stats", "models": ["qwen", "x"]}
    assert policy.platform_default(SETTINGS) == DataPolicy("full", None)
    assert DataPolicy.from_dict(None) == DataPolicy()
    assert DataPolicy.from_dict({"samples": "odd", "models": []}) == DataPolicy()
    assert rules.allows("qwen") and rules.allows("mock-embed") and not rules.allows("mock")
    assert policy.model_problem(rules, "mock", "写条件") == (
        "平台只允许用模型 qwen、x，节点「写条件」用的是 mock"
    )
    assert policy.model_problem(rules, "mock", "出方案", "IBF") == (
        "项目「IBF」只允许用模型 qwen、x，节点「出方案」用的是 mock"
    )
    assert policy.model_problem(rules, "qwen", "写条件") is None


def _profiled() -> list[dict[str, Any]]:
    return [
        {
            "name": "sales.customers",
            "estimated_row_count": 1200,
            "columns": [
                {"name": "customer_id", "data_type": "bigint", "primary_key": True},
                {"name": "city", "data_type": "text"},
            ],
            "column_stats": [
                {"name": "customer_id", "null_fraction": 0, "distinct_count": 1200},
                {
                    "name": "city",
                    "null_fraction": 0.1,
                    "distinct_count": 2,
                    "top_values": [{"value": "上海", "count": 700}],
                },
            ],
            "foreign_keys": [],
            "sample": [{"customer_id": 1385, "city": "上海"}],
        }
    ]


def test_the_profile_description_at_each_level():
    full = describe_for_model(_profiled())
    assert "约 1200 行" in full and '"上海" 700' in full and '"city": "上海"' in full
    assert "数据外发策略" not in full

    masked = describe_for_model(_profiled(), "masked", RUN)
    assert "上海" not in masked
    assert f'"{tok("上海")}" 700' in masked
    assert f'"customer_id": 1000, "city": "{tok("上海")}"' in masked
    assert "按数据外发策略「脱敏样本」" in masked and "照抄记号" in masked

    stats = describe_for_model(_profiled(), "stats", RUN)
    assert (
        "上海" not in stats and "样本：" not in stats and "取值分布" not in stats.split("）", 1)[1]
    )
    assert "约 1200 行" in stats and "空值率 10%" in stats

    schema = describe_for_model(_profiled(), "schema", RUN)
    assert "1200" not in schema and "空值率" not in schema and "上海" not in schema
    assert "  - customer_id：bigint，主键" in schema


def test_plan_problems_list_actual_values_only_when_samples_go_out_as_they_are():
    tables = ["sales.orders", "sales.customers"]
    known = {"status": KnownValues(frozenset({"paid", "cancelled"}), True)}
    with pytest.raises(PlanError, match="取值只有 cancelled、paid"):
        validate_plan(test_etl.returns_plan(), tables, known)
    with pytest.raises(PlanError) as found:
        validate_plan(test_etl.returns_plan(), tables, known, show_values=False)
    assert "cancelled" not in str(found.value)
    assert "status 没有这个取值（按数据外发策略不列出实际取值）" in str(found.value)


# Flow apps


def lookup_flow(datasource: str, model: str | None = None) -> dict:
    """Read sample rows, have a model write a condition from them, have another check it."""
    nodes = [
        {
            "id": "start",
            "type": "start",
            "title": "开始",
            "inputs": [{"key": "city", "label": "城市", "type": "text", "required": True}],
        },
        {
            "id": "rows",
            "type": "tool",
            "title": "读样本",
            "datasource": datasource,
            "tool": "sample_rows",
            "args": {"schema": "sales", "table": "customers"},
        },
        {
            "id": "ask",
            "type": "llm",
            "title": "写条件",
            "model": model,
            "prompt": "数据：{{rows.result}}\n要的城市：{{start.city}}",
        },
        {
            "id": "again",
            "type": "llm",
            "title": "复核",
            "prompt": "条件：{{ask.text}}\n要的城市：{{start.city}}",
        },
        {
            "id": "end",
            "type": "end",
            "title": "结束",
            "outputs": [{"key": "condition", "from": ["{{ask.text}}"]}],
        },
    ]
    edges = [
        {"from": "start", "to": "rows"},
        {"from": "rows", "to": "ask"},
        {"from": "ask", "to": "again"},
        {"from": "again", "to": "end"},
    ]
    return {"graph": {"nodes": nodes, "edges": edges}}


def with_policy(**changes: str) -> Any:
    return SETTINGS.model_copy(update=changes)


def prompt(p: Platform, index: int) -> str:
    return p.gateway.requests[index]["body"]["messages"][-1]["content"]


async def test_data_values_go_to_models_as_they_are_by_default():
    async with Platform(["city = '上海'", "没问题"]) as p:
        await p.new_app("where", lookup_flow(p.shop))
        run = await p.run("where", {"city": "上海"})
        assert run["status"] == "done", run["error"]
        assert run["policy"] == {"samples": "full", "models": None}
        assert "a@example.com" in prompt(p, 0) and "上海" in prompt(p, 0)


@pytest.mark.parametrize("level", ["stats", "schema"])
async def test_data_values_are_left_out(level):
    async with Platform(["city = '上海'", "没问题"], settings=with_policy(data_samples=level)) as p:
        await p.new_app("where", lookup_flow(p.shop))
        run = await p.run("where", {"city": "北京"})
        assert run["status"] == "done", run["error"]
        assert run["policy"] == {"samples": level, "models": None}
        sent = prompt(p, 0)
        assert "a@example.com" not in sent and "上海" not in sent
        assert "（这里有 3 行数据，列为 customer_id、email、city；" in sent
        assert "要的城市：北京" in sent  # What people wrote goes as it is.
        listed = (await p.api.get("/runs")).json()
        assert listed[0]["policy"] == {"samples": level, "models": None}


async def test_masked_tokens_round_trip_through_the_flow():
    settings = with_policy(data_samples="masked")
    async with Platform([], settings=settings) as p:
        await p.new_app("where", lookup_flow(p.shop))
        ids = iter([RUN0])
        p.service._store = PredictableStore(ids)  # noqa: SLF001 - fixed run ids
        p.gateway.replies = [f"city = '{tok('上海', RUN0)}'", "没问题"]
        run = await p.run("where", {"city": "上海"})
        assert run["run_id"] == RUN0
        assert run["status"] == "done", run["error"]
        assert run["policy"] == {"samples": "masked", "models": None}

        first = prompt(p, 0)
        assert "a@example.com" not in first and '上海"' not in first
        assert tok("上海", RUN0) in first and tok("a@example.com", RUN0) in first
        assert "要的城市：上海" in first  # What people wrote goes as it is.
        # The reply's token is turned back: the condition names the real value ...
        assert run["result"] == {"condition": "city = '上海'"}
        # ... and it is a token again in the next model's prompt, but not in the input.
        second = prompt(p, 1)
        assert f"条件：city = '{tok('上海', RUN0)}'" in second
        assert "要的城市：上海" in second


class PredictableStore(MemoryRunStore):
    """Run ids from a list, so a test knows the tokens a run's data gets."""

    def __init__(self, ids: Any) -> None:
        super().__init__()
        self._ids = ids

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        record = await super().create(*args, **kwargs)
        del self._runs[record.run_id]
        record = dataclasses.replace(record, run_id=next(self._ids))
        self._runs[record.run_id] = record
        return record


async def test_code_and_template_outputs_made_from_data_are_data():
    code = 'def main(inputs):\n    return {"first": inputs["rows"]["rows"][0]["city"]}\n'
    nodes = lookup_flow("x")["graph"]["nodes"]
    start, rows = nodes[0], nodes[1]
    config = {
        "graph": {
            "nodes": [
                start,
                rows,
                {
                    "id": "calc",
                    "type": "code",
                    "title": "取第一行",
                    "code": code,
                    "inputs": {"rows": "{{rows.result}}"},
                    "outputs": [{"key": "first", "type": "string"}],
                    "timeout_seconds": 5,
                },
                {"id": "note", "type": "template", "title": "说明", "template": "首个城市"},
                {
                    "id": "ask",
                    "type": "llm",
                    "title": "问",
                    "prompt": "{{calc.first}}|{{note.text}}|{{start.city}}",
                },
                {
                    "id": "end",
                    "type": "end",
                    "title": "结束",
                    "outputs": [{"key": "first", "from": ["{{calc.first}}"]}],
                },
            ],
            "edges": [
                {"from": "start", "to": "rows"},
                {"from": "rows", "to": "calc"},
                {"from": "calc", "to": "note"},
                {"from": "note", "to": "ask"},
                {"from": "ask", "to": "end"},
            ],
        }
    }
    settings = with_policy(data_samples="stats")
    async with Platform(["好"], {SETTINGS.sandbox_url: sandbox()}, settings=settings) as p:
        rows["datasource"] = p.shop
        await p.new_app("first", config)
        run = await p.run("first", {"city": "北京"})
        assert run["status"] == "done", run["error"]
        assert run["result"] == {"first": "上海"}  # People see the real value.
        # The code read data: its output is left out; the template and the input are not.
        assert prompt(p, 0) == "（这里有数据，按数据外发策略没有发给模型）|首个城市|北京"


async def test_an_agent_sees_data_source_results_as_the_policy_lets_them_go():
    replies = [
        call("t4", schema="sales", table="customers"),
        answer(f"第一个客户的邮箱是 {tok('a@example.com', RUN0)}"),
    ]
    async with Platform(replies, settings=with_policy(data_samples="masked")) as p:
        p.service._store = PredictableStore(iter([RUN0]))  # noqa: SLF001
        await p.new_app("ask-data", builtin.TEMPLATES["datasource-qa"].config)
        run = await p.run("ask-data", {"question": "第一个客户是谁？", "datasource": p.shop})
        assert run["status"] == "done", run["error"]
        flow = (await p.api.get(f"/runs/{run['run_id']}/flow")).json()
        agent = next(s for s in flow["steps"] if s["node"] == "agent")
        assert agent["output"]["vars"]["agent"]["steps"][0]["tool"] == "sample_rows"
        result = prompt(p, 1)
        assert "a@example.com" not in result and tok("a@example.com", RUN0) in result
        assert run["result"] == {"answer": "第一个客户的邮箱是 a@example.com"}


# Models a run may use


async def test_a_run_is_refused_when_a_node_uses_a_model_the_platform_does_not_allow():
    async with Platform(["x"], settings=with_policy(allowed_models="qwen")) as p:
        await p.new_app("where", lookup_flow(p.shop))
        resp = await p.api.post("/runs", json={"app": "where", "input": {"city": "上海"}})
        assert resp.status_code == 422
        assert resp.json()["detail"] == "平台只允许用模型 qwen，节点「写条件」用的是 mock"
        assert p.gateway.requests == []

        await p.new_app("where-qwen", _all_qwen(lookup_flow(p.shop)))
        resp = await p.api.post("/runs", json={"app": "where-qwen", "input": {"city": "上海"}})
        assert resp.status_code == 201, resp.text
        await p.service.wait_idle()
        run = (await p.api.get(f"/runs/{resp.json()['run_id']}")).json()
        assert run["policy"] == {"samples": "full", "models": ["qwen"]}


def _all_qwen(config: dict) -> dict:
    for node in config["graph"]["nodes"]:
        if node["type"] == "llm":
            node["model"] = "qwen"
    return config


async def test_knowledge_models_count_and_local_ones_are_always_allowed():
    knowledge = MemoryKnowledgeStore()
    remote = await knowledge.create("远程", embedding_model="company-embed")
    local = await knowledge.create("本地")
    gateway = FakeGateway()
    service = RunService(
        MemoryRunStore(),
        InMemorySaver(),
        default_apps(),
        AppContext(gateway.client, with_policy(allowed_models="qwen"), knowledge=knowledge),
    )

    def config(kb_id: str, rerank: str | None = None) -> dict:
        kb: dict[str, Any] = {
            "id": "kb",
            "type": "knowledge",
            "title": "检索",
            "knowledge_bases": [kb_id],
            "query": "{{start.q}}",
        }
        if rerank:
            kb["retrieval"] = {"rerank": {"model": rerank, "top_n": 3}}
        return {
            "graph": {
                "nodes": [
                    {"id": "start", "type": "start", "title": "开始", "inputs": []},
                    kb,
                    {"id": "end", "type": "end", "title": "结束", "outputs": []},
                ],
                "edges": [{"from": "start", "to": "kb"}, {"from": "kb", "to": "end"}],
            }
        }

    assert await service._models("x", config(remote.kb_id), {}) == [  # noqa: SLF001
        ("company-embed", "检索")
    ]
    assert await service._models("x", config(local.kb_id, "company-rerank"), {}) == [  # noqa: SLF001
        ("mock-embed", "检索"),
        ("company-rerank", "检索"),
    ]
    rules = policy.platform_default(service._ctx.settings)  # noqa: SLF001
    assert rules.allows("mock-embed") and not rules.allows("company-embed")


async def test_the_platform_checks_the_model_again_before_calling_it():
    gateway = FakeGateway(["不会发出"])
    rt = Runtime(gateway.client, "mock", {}, {})
    node = {"id": "ask", "title": "写条件", "model": None}
    state = {"policy": {"samples": "full", "models": ["qwen"]}, "project": {"name": "IBF"}}
    with pytest.raises(NodeFailure) as found:
        await rt.chat(node, [{"role": "user", "content": "hi"}], state)
    assert found.value.type == INPUT_ERROR
    assert found.value.message == "项目「IBF」只允许用模型 qwen，节点「写条件」用的是 mock"
    assert gateway.requests == []
    assert await rt.chat({**node, "model": "qwen"}, [{"role": "user", "content": "hi"}], state)


# The etl code app


async def test_the_etl_app_describes_the_data_at_the_platform_level():
    settings = with_policy(data_samples="stats")
    service, gateway, ds = await test_etl.make_service(
        [test_etl.reply(test_etl.plan())], settings=settings
    )
    run_id = await test_etl.start(service, ds, tables=["sales.orders", "sales.customers"])
    assert (await service.get(run_id)).record.status == "waiting"
    sent = gateway.requests[0]["body"]["messages"][-1]["content"]
    assert "按数据外发策略「只发统计」" in sent
    assert "样本：" not in sent and "上海" not in sent and "a@example.com" not in sent


async def test_the_etl_app_refuses_a_model_the_platform_does_not_allow():
    service, gateway, ds = await test_etl.make_service(
        [], settings=with_policy(allowed_models="qwen")
    )
    with pytest.raises(ModelNotAllowed, match="平台只允许用模型 qwen，节点「出方案」用的是 mock"):
        await test_etl.start(service, ds)
    assert gateway.requests == []


async def test_the_etl_app_turns_masked_values_back_and_masks_them_again():
    settings = with_policy(data_samples="masked")
    service, gateway, ds = await test_etl.make_service([], settings=settings)
    service._store = PredictableStore(iter([RUN0]))  # noqa: SLF001
    value = test_etl.plan()
    value["targets"][0]["quality_rules"].append(
        {
            "name": "shanghai",
            "condition": f"city = '{tok('上海', RUN0)}'",
            "on_violation": "drop",
        }
    )
    gateway.replies = [test_etl.reply(value)]
    run_id = await test_etl.start(service, ds, tables=["sales.orders", "sales.customers"])
    assert run_id == RUN0
    view = await service.get(run_id)
    assert view.record.status == "waiting", view.record.error
    rules = view.pending["details"]["plan"]["targets"][0]["quality_rules"]
    assert rules[-1]["condition"] == "city = '上海'"
    assert "a@example.com" not in json.dumps(gateway.requests[0]["body"], ensure_ascii=False)

    gateway.replies = [test_etl.reply({"targets": {"order_facts": test_etl.PYSPARK_BODY}})]
    await test_etl.decide(service, run_id, "approve")
    codegen = gateway.requests[1]["body"]["messages"][-1]["content"]
    assert f"city = '{tok('上海', RUN0)}'" in codegen
    assert "city = '上海'" not in codegen


# Error text that may quote the data


ERROR = (
    "[CAST_INVALID_INPUT] The value 'a@example.com' of the type \"STRING\" cannot be cast "
    "to \"DECIMAL(10,2)\"; it doesn't fit. Row(state='CA', n=3)"
)


def test_quoted_values_in_error_text_follow_the_policy():
    assert policy.scrub(ERROR, "full", RUN) is ERROR
    hidden = policy.scrub(ERROR, "stats", RUN)
    assert hidden == (
        "[CAST_INVALID_INPUT] The value '‹已隐去›' of the type \"STRING\" cannot be cast "
        "to \"DECIMAL(10,2)\"; it doesn't fit. Row(state='‹已隐去›', n=3)"
    )
    assert policy.scrub([ERROR], "schema", RUN) == [hidden]
    masked = policy.scrub(ERROR, "masked", RUN)
    assert f"'{tok('a@example.com')}'" in masked and f"'{tok('CA')}'" in masked
    assert '"STRING"' in masked and "doesn't" in masked
    assert policy.scrub(masked, "masked", RUN) == masked  # Tokens stay as they are.
    assert policy.literals(ERROR) == ["a@example.com", "CA"]


def failing_lookup() -> MCPServer:
    server = MCPServer("fake-lookup")

    @server.tool()
    def lookup(code: str) -> dict:
        """Look a code up."""
        raise ToolError(f"invalid input syntax for type integer: '{code}'")

    return server


LOOKUP_URL = "http://lookup.test/mcp"


async def test_an_error_branch_sends_the_error_without_the_values_it_quotes():
    nodes = [
        {
            "id": "start",
            "type": "start",
            "title": "开始",
            "inputs": [{"key": "code", "label": "编号", "type": "text", "required": True}],
        },
        {
            "id": "find",
            "type": "tool",
            "title": "查编号",
            "datasource": "x",
            "tool": "lookup",
            "args": {"code": "{{start.code}}"},
            "on_error": {"then": "branch"},
        },
        {"id": "ask", "type": "llm", "title": "解释", "prompt": "出错了：{{find.error_message}}"},
        {
            "id": "end",
            "type": "end",
            "title": "结束",
            "outputs": [{"key": "error", "from": ["{{find.error_message}}"]}],
        },
    ]
    edges = [
        {"from": "start", "to": "find"},
        {"from": "find", "branch": "error", "to": "ask"},
        {"from": "find", "to": "end"},
        {"from": "ask", "to": "end"},
    ]
    settings = with_policy(data_samples="schema")
    async with Platform(["好"], {LOOKUP_URL: failing_lookup()}, settings=settings) as p:
        source = await p.sources.create("编号库", "postgres", LOOKUP_URL)
        nodes[1]["datasource"] = source.datasource_id
        await p.new_app("codes", {"graph": {"nodes": nodes, "edges": edges}})
        run = await p.run("codes", {"code": "S-42"})
        assert run["status"] == "done", run["error"]
        assert "S-42" in run["result"]["error"]  # People see the whole error.
        sent = prompt(p, 0)
        assert "S-42" not in sent
        assert "invalid input syntax for type integer: '‹已隐去›'" in sent


async def test_an_agent_gets_data_source_errors_without_the_values_they_quote():
    replies = [call("t1", code="S-42"), answer("查不到")]
    settings = with_policy(data_samples="stats")
    async with Platform(replies, {LOOKUP_URL: failing_lookup()}, settings=settings) as p:
        source = await p.sources.create("编号库", "postgres", LOOKUP_URL)
        await p.new_app("ask-codes", builtin.TEMPLATES["datasource-qa"].config)
        run = await p.run(
            "ask-codes", {"question": "S-42 是什么？", "datasource": source.datasource_id}
        )
        assert run["status"] == "done", run["error"]
        said = prompt(p, 1)
        assert "出错了：" in said and "'‹已隐去›'" in said and "S-42" not in said


async def test_the_etl_app_sends_dev_run_errors_without_the_values_they_quote():
    class Dev:
        def __init__(self) -> None:
            self.runs = [
                DevRun("failed", "run", [], errors=[ERROR], fixable=True),
                DevRun("passed", "run", []),
            ]

        async def run(self, bundle: Any) -> DevRun:
            return self.runs.pop(0)

    code = test_etl.reply({"targets": {"order_facts": test_etl.PYSPARK_BODY}})
    service, gateway, ds = await test_etl.make_service(
        [test_etl.reply(test_etl.plan()), code, code],
        settings=with_policy(data_samples="stats"),
        databricks=Dev(),
    )
    run_id = await test_etl.start(
        service, ds, tables=["sales.orders", "sales.customers"], release=False
    )
    await test_etl.decide(service, run_id, "approve")
    retry = gateway.requests[2]["body"]["messages"][-1]["content"]
    assert "运行失败，报错如下" in retry
    assert "a@example.com" not in retry and "The value '‹已隐去›'" in retry
    assert '"STRING"' in retry


async def test_reformatting_an_error_does_not_take_the_values_it_quotes_past_the_policy():
    nodes = [
        {
            "id": "start",
            "type": "start",
            "title": "开始",
            "inputs": [{"key": "code", "label": "编号", "type": "text", "required": True}],
        },
        {
            "id": "find",
            "type": "tool",
            "title": "查编号",
            "datasource": "x",
            "tool": "lookup",
            "args": {"code": "{{start.code}}"},
            "on_error": {"then": "branch"},
        },
        {
            "id": "fmt",
            "type": "template",
            "title": "整理",
            "template": "报错：{{find.error_message}}",
        },
        {
            "id": "calc",
            "type": "code",
            "title": "转写",
            "code": 'def main(inputs):\n    return {"said": inputs["e"].upper()}\n',
            "inputs": {"e": "{{find.error_message}}"},
            "outputs": [{"key": "said", "type": "string"}],
            "timeout_seconds": 5,
        },
        {"id": "ask", "type": "llm", "title": "解释", "prompt": "{{fmt.text}}\n{{calc.said}}"},
        {
            "id": "end",
            "type": "end",
            "title": "结束",
            "outputs": [{"key": "error", "from": ["{{fmt.text}}"]}],
        },
    ]
    edges = [
        {"from": "start", "to": "find"},
        {"from": "find", "branch": "error", "to": "fmt"},
        {"from": "find", "to": "end"},
        {"from": "fmt", "to": "calc"},
        {"from": "calc", "to": "ask"},
        {"from": "ask", "to": "end"},
    ]
    servers = {LOOKUP_URL: failing_lookup(), SETTINGS.sandbox_url: sandbox()}
    async with Platform(["好"], servers, settings=with_policy(data_samples="schema")) as p:
        source = await p.sources.create("编号库", "postgres", LOOKUP_URL)
        nodes[1]["datasource"] = source.datasource_id
        await p.new_app("codes", {"graph": {"nodes": nodes, "edges": edges}})
        run = await p.run("codes", {"code": "S-42"})
        assert run["status"] == "done", run["error"]
        assert "S-42" in run["result"]["error"]  # People see the whole error.
        sent = prompt(p, 0)
        assert "S-42" not in sent
        assert sent.startswith("报错：工具报错：")
        assert "invalid input syntax for type integer: '‹已隐去›'" in sent
        assert "INVALID INPUT SYNTAX FOR TYPE INTEGER: '‹已隐去›'" in sent


async def test_knowledge_models_are_checked_again_right_before_retrieval():
    # The base's embedding model can change after the run was created (an empty base
    # may change it), so the check at creation is not enough.
    knowledge = MemoryKnowledgeStore()
    remote = await knowledge.create("远程", embedding_model="company-embed")
    local = await knowledge.create("本地")
    gateway = FakeGateway([call("kb", query="退货")])
    rt = Runtime(gateway.client, "mock", {}, {}, knowledge=knowledge)
    state = {"policy": {"samples": "full", "models": ["qwen"]}, "vars": {}}
    node = {
        "id": "kb",
        "type": "knowledge",
        "title": "检索",
        "knowledge_bases": [remote.kb_id],
        "query": "退货",
    }
    with pytest.raises(NodeFailure) as found:
        await run_knowledge(node, state, rt)
    assert found.value.type == INPUT_ERROR
    assert found.value.message == "平台只允许用模型 qwen，节点「检索」用的是 company-embed"
    reranked = {
        **node,
        "knowledge_bases": [local.kb_id],
        "retrieval": {"rerank": {"model": "company-rerank", "candidates": 20}},
    }
    with pytest.raises(NodeFailure) as found:
        await run_knowledge(reranked, state, rt)
    assert found.value.message.endswith("节点「检索」用的是 company-rerank")
    assert gateway.requests == []

    # An agent's knowledge tool fails the node the same way, without the request.
    agent = {
        **builtin.TEMPLATES["knowledge-agent"].config["graph"]["nodes"][1],
        "model": "qwen",
    }
    agent["knowledge"] = {**agent["knowledge"], "knowledge_bases": [remote.kb_id]}
    state["vars"] = {"start": {"question": "多久能退货？"}}
    with pytest.raises(NodeFailure) as found:
        await run_agent(agent, state, rt)
    assert found.value.type == INPUT_ERROR
    assert found.value.message.endswith("用的是 company-embed")
    assert [r["body"]["model"] for r in gateway.requests] == ["qwen"]


async def test_an_agent_keeps_tool_values_to_turn_back_after_searching_knowledge():
    replies = [
        call("t4", schema="sales", table="customers"),
        call("kb", query="客户"),
        answer(f"第一个客户的邮箱是 {tok('a@example.com', RUN0)}"),
    ]
    async with Platform(replies, settings=with_policy(data_samples="masked")) as p:
        p.service._store = PredictableStore(iter([RUN0]))  # noqa: SLF001
        base = await p.service.knowledge.create("资料")
        config = builtin.TEMPLATES["datasource-qa"].config
        config["graph"]["nodes"][1]["knowledge"] = {
            "knowledge_bases": [base.kb_id],
            "top_k": 5,
            "min_score": 0.3,
        }
        await p.new_app("ask-data", config)
        run = await p.run("ask-data", {"question": "第一个客户是谁？", "datasource": p.shop})
        assert run["status"] == "done", run["error"]
        assert run["result"] == {"answer": "第一个客户的邮箱是 a@example.com"}


async def test_the_etl_app_keeps_the_policy_the_run_was_created_with():
    class Dev:
        def __init__(self) -> None:
            self.runs = [
                DevRun("failed", "run", [], errors=[ERROR], fixable=True),
                DevRun("passed", "run", []),
            ]

        async def run(self, bundle: Any) -> DevRun:
            return self.runs.pop(0)

    code = test_etl.reply({"targets": {"order_facts": test_etl.PYSPARK_BODY}})
    service, gateway, ds = await test_etl.make_service(
        [test_etl.reply(test_etl.plan()), code, code],
        settings=with_policy(data_samples="stats"),
        databricks=Dev(),
    )
    run_id = await test_etl.start(
        service, ds, tables=["sales.orders", "sales.customers"], release=False
    )
    # The platform restarts with another setting while the run waits.
    ctx = dataclasses.replace(service._ctx, settings=with_policy(data_samples="full"))  # noqa: SLF001
    restarted = RunService(service._store, service._checkpointer, default_apps(), ctx)  # noqa: SLF001
    view = await test_etl.decide(restarted, run_id, "approve")
    assert view.record.policy == {"samples": "stats", "models": None}
    retry = gateway.requests[2]["body"]["messages"][-1]["content"]
    assert "a@example.com" not in retry and "The value '‹已隐去›'" in retry


def test_long_quoted_values_are_scrubbed_too():
    value = "x" * 250
    text = f"cannot cast '{value}' to INT"
    assert policy.scrub(text, "stats", RUN) == "cannot cast '‹已隐去›' to INT"
    assert policy.scrub(text, "masked", RUN) == f"cannot cast '{tok(value)}' to INT"
    assert policy.literals(text) == [value]


def test_long_sample_values_are_masked_whole_so_their_tokens_turn_back():
    long = "备注" * 60
    tables = _profiled()
    tables[0]["sample"][0]["city"] = long
    sent = describe_for_model(tables, "masked", RUN)
    assert tok(long) in sent
    back, _ = policy.unmask(f"city = '{tok(long)}'", [tables], RUN)
    assert back == f"city = '{long}'"
    assert long[:80] + "…" in describe_for_model(tables, "full", RUN)


def test_quoted_values_holding_the_other_quote_are_scrubbed():
    text = """cannot cast "O'Reilly" to INT; 'say "hi"' is not a date; it doesn't parse"""
    assert policy.scrub(text, "stats", RUN) == (
        "cannot cast \"‹已隐去›\" to INT; '‹已隐去›' is not a date; it doesn't parse"
    )
    assert policy.scrub(text, "masked", RUN) == (
        f"cannot cast \"{tok("O'Reilly")}\" to INT; '{tok('say "hi"')}' is not a date; "
        "it doesn't parse"
    )
    assert policy.literals(text) == ["O'Reilly", 'say "hi"']


def test_quoted_values_across_lines_are_scrubbed():
    text = "cannot cast 'private@example.com\nprivate-address' to INT"
    assert policy.scrub(text, "schema", RUN) == "cannot cast '‹已隐去›' to INT"
    value = "private@example.com\nprivate-address"
    assert policy.scrub(text, "masked", RUN) == f"cannot cast '{tok(value)}' to INT"
    assert policy.literals(text) == [value]


def test_tokens_in_a_json_reply_turn_back_as_json_strings():
    value = 'O"Re\\illy\n二楼'
    data = [[{"name": value}]]
    for reply in [
        f'{{"filter": "name = \'{tok(value)}\'"}}',
        f'方案如下：\n```json\n{{"filter": "name = \'{tok(value)}\'"}}\n```\n请确认。',
    ]:
        back, restored = policy.unmask(reply, data, RUN)
        assert restored == {tok(value): value}
        assert llm.parse_json(back) == {"filter": f"name = '{value}'"}
        # Sent on to a model, the escaped value is a token again.
        assert policy.remask(back, restored) == reply
    # Text that is not a JSON object gets the value as it is.
    back, _ = policy.unmask(f"name = '{tok(value)}'", data, RUN)
    assert back == f"name = '{value}'"
    # A value parsed out of JSON already is not escaped again.
    back, _ = policy.unmask({"filter": f"name = '{tok(value)}'"}, data, RUN)
    assert back == {"filter": f"name = '{value}'"}
