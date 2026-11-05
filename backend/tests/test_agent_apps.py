"""Agent apps: a form-configured agent run as a generated graph (docs/api/agent-apps.md),
and the agent node settings they bring (docs/api/flow-apps.md, 智能体节点的新设置)."""

import copy
import json
from typing import Any

import yaml

from aep import policy
from aep.runtime import agent_app
from aep.runtime.flow import builtin
from aep.runtime.flow.nodes import UNGROUNDED
from tests.test_conversations import EMAIL, agent_chat, llm_chat, open_chat, say, sent
from tests.test_flow_tools import Platform, answer, call
from tests.test_policy import with_policy


async def new_agent(p: Platform, app: str, source: dict | None = None) -> dict:
    resp = await p.api.post(
        "/apps", json={"app": app, "title": "报销助手", "from": source or {"blank_agent": True}}
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def save(p: Platform, app: str, config: dict) -> Any:
    return await p.api.put(f"/apps/{app}/draft", json={"config": config})


async def publish(p: Platform, app: str, config: dict) -> None:
    saved = await save(p, app, config)
    assert saved.status_code == 200, saved.text
    done = await p.api.post(f"/apps/{app}/draft/publish", json={"note": "测试"})
    assert done.status_code == 200, done.text


def data_agent(datasource: str, **agent: Any) -> dict:
    config = agent_app.data_template()
    config["agent"]["tools"][0]["datasource"] = datasource
    config["agent"].update(agent)
    return config


async def test_a_blank_agent_chats_with_its_instructions():
    async with Platform(["你好，我是报销助手。", "住宿按城市分三档。"]) as p:
        made = await new_agent(p, "helper")
        assert made["kind"] == "agent" and made["current_version"] == 1
        assert made["inputs"] == [
            {"key": "message", "label": "消息", "type": "textarea", "required": True}
        ]
        assert made["chat"] == {"opening": "你好，有什么可以帮你？", "suggestions": []}
        assert [(n["id"], n["title"]) for n in made["nodes"] if n["type"]] == [
            ("start", "收到消息"),
            ("agent", "智能体"),
            ("end", "回答"),
        ]
        # The version keeps the form's configuration, not the generated graph.
        kept = made["current"]["config"]
        assert set(kept) == {"schema_version", "env", "agent", "chat"}
        assert kept["agent"]["grounded_only"] is True and kept["agent"]["history_turns"] == 10
        listed = (await p.api.get("/apps")).json()
        assert next(a for a in listed if a["app"] == "helper")["kind"] == "agent"

        cid = await open_chat(p, "helper")
        first = await say(p, cid, "你是谁？")
        assert first["status"] == "done", first["error"]
        assert first["result"] == {
            "reply": "你好，我是报销助手。",
            "steps": [],
            "hits": [],
            "grounded": True,
        }
        system = p.gateway.requests[0]["body"]["messages"][0]
        assert system == {"role": "system", "content": kept["agent"]["instructions"]}
        await say(p, cid, "住宿标准呢？")
        # No tools: a plain chat, the history as plain messages.
        assert sent(p, 1) == [
            ("user", "你是谁？"),
            ("assistant", "你好，我是报销助手。"),
            ("user", "住宿标准呢？"),
        ]
        # It runs once too, like any app.
        run = await p.run("helper", {"message": "单次问一下"})
        assert run["status"] == "done" and run["result"]["reply"] == "住宿按城市分三档。"
        assert run["conversation"] is None


async def test_shipped_agent_configs_pass_the_check():
    async with Platform([]) as p:
        for config in (agent_app.blank(), agent_app.knowledge_template(), data_agent(p.shop)):
            if config["agent"]["knowledge"] is not None:
                continue  # its knowledge base is chosen when the app is set up
            checked = await p.catalog.check_agent(config)
            assert checked["agent"]["instructions"] == config["agent"]["instructions"]


async def test_agent_templates_start_as_drafts_to_set_up():
    async with Platform([]) as p:
        templates = {t["id"]: t for t in (await p.api.get("/app-templates")).json()}
        assert {t["kind"] for t in templates.values()} == {"flow", "agent"}
        assert templates["agent-blank"]["kind"] == "agent"
        assert templates["agent-knowledge"]["needs"] == ["knowledge_base"]
        assert templates["agent-data"]["needs"] == ["datasource"]
        assert templates["datasource-qa"]["kind"] == "flow"

        made = await new_agent(p, "ask-data", {"template": "agent-data"})
        assert made["kind"] == "agent"
        # Version 1 is a blank agent; the template waits in the draft for its data source.
        assert made["current"]["config"]["agent"]["tools"] == []
        assert made["draft"]["config"]["agent"]["tools"] == [{"datasource": "", "tools": None}]
        await publish(p, "ask-data", data_agent(p.shop))
        got = (await p.api.get("/apps/ask-data")).json()
        assert got["current_version"] == 2
        assert got["current"]["config"]["agent"]["tools"][0]["datasource"] == p.shop

        copied = await new_agent(p, "ask-data-2", {"app": "ask-data"})
        assert copied["kind"] == "agent"
        assert copied["current"]["config"]["agent"]["tools"][0]["datasource"] == p.shop


async def test_an_agent_with_tools_follows_the_protocol_and_answers_only_from_them():
    async with Platform([]) as p:
        await new_agent(p, "ask-data", {"template": "agent-data"})
        await publish(p, "ask-data", data_agent(p.shop))

        # It answered without looking anything up: the answer is held back.
        p.gateway.replies = [answer("大概有一百个客户吧")]
        guessed = await p.run("ask-data", {"message": "有多少客户？"})
        assert guessed["status"] == "done", guessed["error"]
        assert guessed["result"]["reply"] == UNGROUNDED
        assert guessed["result"]["grounded"] is False and guessed["result"]["steps"] == []
        system = p.gateway.requests[0]["body"]["messages"][0]["content"]
        assert "只能依据这一轮工具返回的数据" in system and "可以用的工具" in system
        assert system.index("你是数据问答助手") < system.index("可以用的工具")
        flow = (await p.api.get(f"/runs/{guessed['run_id']}/flow")).json()
        step = next(s for s in flow["steps"] if s["node"] == "agent")
        assert step["output"]["vars"]["agent"]["ungrounded_text"] == "大概有一百个客户吧"
        assert "ungrounded_text" not in guessed["result"]

        # It read the data first: the answer stands.
        p.gateway.replies = [
            call("t4", schema="sales", table="customers"),
            answer("有 3 个客户"),
        ]
        looked = await p.run("ask-data", {"message": "有多少客户？"})
        assert looked["result"]["reply"] == "有 3 个客户"
        assert looked["result"]["grounded"] is True
        assert [s["tool"] for s in looked["result"]["steps"]] == ["sample_rows"]

        # Answering from anything goes when grounded_only is off.
        await publish(p, "ask-data", data_agent(p.shop, grounded_only=False))
        p.gateway.replies = [answer("大概有一百个客户吧")]
        free = await p.run("ask-data", {"message": "有多少客户？"})
        assert free["result"] == {
            "reply": "大概有一百个客户吧",
            "steps": [],
            "hits": [],
            "grounded": True,
        }


async def test_an_agent_app_converses_with_its_tools():
    async with Platform([]) as p:
        await new_agent(p, "ask-data", {"template": "agent-data"})
        await publish(p, "ask-data", data_agent(p.shop))
        cid = await open_chat(p, "ask-data")
        p.gateway.replies = [call("t4", schema="sales", table="customers"), answer("有 3 个客户")]
        await say(p, cid, "有多少客户？")
        p.gateway.replies = [call("t4", schema="sales", table="customers"), answer("都在上海")]
        second = await say(p, cid, "他们在哪？")
        assert second["result"]["reply"] == "都在上海"
        # The history is the earlier message and answer, as the protocol has answers.
        assert sent(p, 2) == [
            ("user", "有多少客户？"),
            (
                "assistant",
                json.dumps({"action": "answer", "text": "有 3 个客户"}, ensure_ascii=False),
            ),
            ("user", "他们在哪？"),
        ]


async def problems(p: Platform, config: dict) -> dict[tuple, str]:
    resp = await save(p, "helper", config)
    assert resp.status_code == 422, resp.text
    return {tuple(d["loc"][2:]): d["msg"] for d in resp.json()["detail"]}


async def test_an_agent_apps_settings_are_checked():
    async with Platform([]) as p:
        await new_agent(p, "helper")
        blank = agent_app.blank()

        def changed(**agent: Any) -> dict:
            config = copy.deepcopy(blank)
            config["agent"].update(agent)
            return config

        assert await problems(p, changed(instructions=" ")) == {
            ("agent", "instructions"): "指令不能为空"
        }
        assert await problems(p, changed(instructions="长" * 20001)) == {
            ("agent", "instructions"): "指令最多 20000 个字符"
        }
        assert await problems(p, changed(history_turns=51)) == {
            ("agent", "history_turns"): "带的历史轮数要在 0–50 之间"
        }
        assert await problems(p, changed(grounded_only="yes")) == {
            ("agent", "grounded_only"): "grounded_only 必须是 true 或 false"
        }
        assert await problems(p, changed(colour="red")) == {
            ("agent", "colour"): "智能体没有 colour 这一项"
        }
        assert await problems(p, changed(apps=[{"app": "hello", "when": "总是"}])) == {
            ("agent", "apps", 0, "app"): "应用「问答示例」里有人工节点，智能体等不了人"
        }
        assert await problems(
            p, changed(tools=[{"datasource": "{{env.source}}", "tools": None}])
        ) == {("agent", "tools", 0, "datasource"): "智能体应用里要直接选数据源，不能写引用"}
        # What the agent node checks is reported where it is in the form.
        assert await problems(p, changed(max_steps=0)) == {
            ("agent", "max_steps"): "最多步数要在 1–20 之间"
        }
        unknown_model = await problems(p, changed(model="nope"))
        assert list(unknown_model) == [("agent", "model")]
        secret = changed(instructions="用 {{secret.crm_token}} 登录")
        secret["env"]["secrets"] = ["crm_token"]
        assert list(await problems(p, secret)) == [("agent", "instructions")]

        chat = copy.deepcopy(blank)
        chat["chat"] = {"opening": "长" * 501, "suggestions": ["一", "二", "三", "四", "五", "六"]}
        assert await problems(p, chat) == {
            ("chat", "opening"): "开场白最多 500 个字符",
            ("chat", "suggestions"): "推荐问题最多 5 个",
        }
        # A flow configuration is not an agent's.
        flowy = await problems(p, builtin.blank())
        assert flowy == {("agent",): "智能体应用的配置要有 agent"}

        fine = changed(instructions="你好 {{env.company}}", history_turns=0)
        fine["env"]["vars"] = [{"key": "company", "type": "string", "value": "示例公司"}]
        fine["chat"] = {"opening": "欢迎", "suggestions": ["怎么报销？"]}
        saved = await save(p, "helper", fine)
        assert saved.status_code == 200, saved.text
        kept = saved.json()["draft"]["config"]
        assert kept["agent"]["history_turns"] == 0 and kept["chat"]["suggestions"] == ["怎么报销？"]


async def test_trying_out_the_agent_node():
    async with Platform(["试一下的回答"]) as p:
        await new_agent(p, "helper")
        tried = await p.api.post(
            "/apps/helper/nodes/agent/test", json={"values": {"start.message": "你好"}}
        )
        assert tried.status_code == 200, tried.text
        assert tried.json()["status"] == "done", tried.text
        assert tried.json()["outputs"]["text"] == "试一下的回答"
        for node in ("start", "end"):
            refused = await p.api.post(f"/apps/helper/nodes/{node}/test", json={"values": {}})
            assert refused.status_code in (409, 422), refused.text


# The agent node's new settings in a flow app


def flow_agent(**settings: Any) -> dict:
    config = copy.deepcopy(builtin.TEMPLATES["datasource-qa"].config)
    config["graph"]["nodes"][1].update(settings)
    return config


async def test_a_flow_agent_without_tools_is_a_plain_chat_with_instructions():
    async with Platform(["直接回答"]) as p:
        await p.new_app("ask", flow_agent(tools=[], instructions="你是客服"))
        run = await p.run("ask", {"question": "在吗？", "datasource": p.shop})
        assert run["status"] == "done", run["error"]
        assert run["result"] == {"answer": "直接回答"}
        messages = p.gateway.requests[0]["body"]["messages"]
        assert messages[0] == {"role": "system", "content": "你是客服"}
        assert messages[1]["content"].startswith("根据数据源里的数据回答问题：在吗？")


async def test_a_flow_agent_answers_freely_unless_told_otherwise():
    async with Platform([answer("不查也知道")]) as p:
        await p.new_app("ask", flow_agent())
        run = await p.run("ask", {"question": "有多少客户？", "datasource": p.shop})
        assert run["result"] == {"answer": "不查也知道"}  # grounded_only is off on nodes
        await p.new_app("strict", flow_agent(grounded_only=True))
        p.gateway.replies = [answer("不查也知道")]
        held = await p.run("strict", {"question": "有多少客户？", "datasource": p.shop})
        assert held["result"] == {"answer": UNGROUNDED}


# Files and turning into a flow app


async def test_an_agent_app_exports_and_imports_as_an_agent_app():
    async with Platform([]) as p:
        await new_agent(p, "helper")
        config = agent_app.config("你是报销助手。", opening="你好", suggestions=["住宿标准？"])
        await publish(p, "helper", config)
        kept = (await p.api.get("/apps/helper")).json()["current"]["config"]
        exported = await p.api.get("/apps/helper/versions/2/export")
        assert exported.status_code == 200, exported.text
        data = yaml.safe_load(exported.text)
        assert data["kind"] == "agent" and data["config"] == kept
        assert data["requires"] == {
            "skills": [],
            "datasources": [],
            "knowledge_bases": [],
            "secrets": [],
            "apps": [],
        }
        made = await p.api.post("/apps/import", json={"app": "helper-copy", "yaml": exported.text})
        assert made.status_code == 201, made.text
        assert made.json()["problems"] == []
        copied = made.json()["app"]
        assert copied["kind"] == "agent" and copied["current"]["config"] == kept
        assert copied["chat"] == {"opening": "你好", "suggestions": ["住宿标准？"]}

        # A flow app's file names no kind, as before, and stays a flow app's.
        resp = await p.api.post("/apps", json={"app": "plain", "title": "普通应用"})
        assert resp.status_code == 201, resp.text
        flow_file = (await p.api.get("/apps/plain/versions/1/export")).text
        assert "kind" not in yaml.safe_load(flow_file)
        imported = await p.api.post("/apps/import", json={"app": "plain-copy", "yaml": flow_file})
        assert imported.status_code == 201 and imported.json()["app"]["kind"] == "flow"

        # A draft comes from a file of the app's own kind.
        wrong = await p.api.post("/apps/helper/draft/import", json={"yaml": flow_file})
        assert wrong.status_code == 422
        assert wrong.json()["detail"] == "这是编排应用的文件，不能导入成智能体应用的草稿"
        wrong = await p.api.post("/apps/plain/draft/import", json={"yaml": exported.text})
        assert wrong.status_code == 422
        assert wrong.json()["detail"] == "这是智能体应用的文件，不能导入成编排应用的草稿"
        right = await p.api.post("/apps/helper/draft/import", json={"yaml": exported.text})
        assert right.status_code == 200, right.text
        assert right.json()["app"]["draft"]["config"] == kept
        odd = await p.api.post(
            "/apps/import",
            json={"app": "odd", "yaml": exported.text.replace("kind: agent", "kind: bot")},
        )
        assert odd.status_code == 422 and "不认识的应用类型" in odd.json()["detail"]


async def test_an_agent_app_turns_into_a_flow_app_that_answers_the_same():
    async with Platform(["住宿按城市分三档。", "住宿按城市分三档。"]) as p:
        await new_agent(p, "helper")
        cases = [
            {
                "id": "stay",
                "input": {"message": "住宿标准？"},
                "expect": [{"kind": "contains", "path": "reply", "values": ["三档"]}],
            }
        ]
        saved = await p.api.put("/apps/helper/eval-set", json={"cases": cases})
        assert saved.status_code == 200, saved.text
        made = await p.api.post("/apps/helper/convert-to-flow", json={"app": "helper-flow"})
        assert made.status_code == 201, made.text
        flow = made.json()
        assert (flow["kind"], flow["title"], flow["current_version"]) == ("flow", "报销助手", 1)
        config = flow["current"]["config"]
        assert config["conversation"] == {"enabled": True, "history_turns": 10, "reply": "reply"}
        assert [n["id"] for n in config["graph"]["nodes"]] == ["start", "agent", "end"]
        assert config["env"] == {"vars": [], "secrets": []}
        copied = (await p.api.get("/apps/helper-flow/eval-set")).json()
        assert [c["id"] for c in copied["cases"]] == ["stay"]

        one = await p.run("helper", {"message": "住宿标准？"})
        other = await p.run("helper-flow", {"message": "住宿标准？"})
        assert one["result"] == other["result"]
        first, second = (r["body"]["messages"] for r in p.gateway.requests)
        assert first == second
        assert (await p.api.get("/apps/helper")).json()["kind"] == "agent"

        taken = await p.api.post("/apps/helper/convert-to-flow", json={"app": "helper-flow"})
        assert taken.status_code == 409 and taken.json()["detail"] == "已经有应用 helper-flow"
        for app in ("helper-flow", "etl"):
            refused = await p.api.post(f"/apps/{app}/convert-to-flow", json={"app": "x-flow"})
            assert refused.status_code == 409
            assert refused.json()["detail"] == "只有智能体应用能转成编排应用"
        missing = await p.api.post(
            "/apps/helper/convert-to-flow", json={"app": "x-flow", "version": 9}
        )
        assert missing.status_code == 404
        nobody = await p.api.post("/apps/nobody/convert-to-flow", json={"app": "x-flow"})
        assert nobody.status_code == 404


# Eval cases with history (docs/api/evals.md)

SAME = "意思和参考答案一致"
JUDGED = json.dumps({"pass": True, "reason": "意思一样"})


async def eval_draft(p: Platform, app: str) -> dict:
    started = await p.api.post(f"/apps/{app}/draft/eval", json={})
    assert started.status_code == 202, started.text
    await p.catalog.wait_idle()
    return (await p.api.get(f"/apps/{app}")).json()["draft"]["eval"]


async def test_a_turn_saved_as_a_case_replays_as_that_turn():
    async with Platform(["一线城市 600 元。", "二线城市 450 元。"]) as p:
        made = await new_agent(p, "helper")
        cid = await open_chat(p, "helper")
        await say(p, cid, "出差住宿标准是多少？")
        second = await say(p, cid, "那二线城市呢？")
        saved = await p.api.post(
            "/apps/helper/eval-set/cases/from-run", json={"run_id": second["run_id"]}
        )
        assert saved.status_code == 201, saved.text
        case = saved.json()
        assert case["input"] == {"message": "那二线城市呢？"}
        assert case["history"] == [
            {"message": "出差住宿标准是多少？", "reply": "一线城市 600 元。"}
        ]
        assert case["turn"] == 2 and case["restored"] == {}
        # How the turn went (steps, hits) changes every run: no checks for it. The reply is
        # what the model wrote, worded differently every run: a judge, however short.
        assert case["expect"] == [
            {"kind": "judge", "path": "reply", "criteria": SAME, "reference": "二线城市 450 元。"},
            {"kind": "equals", "path": "grounded", "value": True},
        ]

        await save(p, "helper", made["current"]["config"])
        p.gateway.replies = ["二线城市是 450 元。", JUDGED]
        result = await eval_draft(p, "helper")
        assert (result["status"], result["score"]) == ("passed", 1.0), result
        judged = json.dumps(p.gateway.requests[-1]["body"], ensure_ascii=False)
        assert "二线城市是 450 元。" in judged and "二线城市 450 元。" in judged
        assert sent(p, -2) == [
            ("user", "出差住宿标准是多少？"),
            ("assistant", "一线城市 600 元。"),
            ("user", "那二线城市呢？"),
        ]
        # The eval keeps no conversation.
        run = (await p.api.get(f"/runs/{result['cases'][0]['run_id']}")).json()
        assert run["conversation"] is None


async def test_a_case_runs_as_its_turn_with_its_history_as_it_is():
    token = policy.token("conv_elsewhere", "张三")  # worked out from another conversation
    async with Platform(["他叫 " + token]) as p:
        await p.new_app("chat", llm_chat("第 {{conversation.turn}} 轮：{{start.message}}"))
        history = [{"message": f"{token} 的工号？", "reply": "A-1"}] * 12
        case = {
            "id": "who",
            "input": {"message": "他叫什么？"},
            "history": history,
            "turn": 40,
            "restored": {token: "张三"},
            "expect": [{"kind": "equals", "path": "reply", "value": "他叫 张三"}],
        }
        saved = await p.api.put("/apps/chat/eval-set", json={"cases": [case]})
        assert saved.status_code == 200, saved.text
        await p.api.put(
            "/apps/chat/draft",
            json={"config": llm_chat("第 {{conversation.turn}} 轮：{{start.message}}")},
        )
        result = await eval_draft(p, "chat")
        assert (result["status"], result["score"]) == ("passed", 1.0), result
        request = sent(p, 0)
        # All twelve turns go as they are (no turn limit), and the token, not the name.
        assert len(request) == 25 and request[-1] == ("user", "第 40 轮：他叫什么？")
        assert "张三" not in json.dumps(p.gateway.requests[0]["body"], ensure_ascii=False)


async def test_a_run_outside_a_conversation_replays_as_one():
    prompt = "第 {{conversation.turn}} 轮：{{start.message}}"
    async with Platform(["答"]) as p:
        await p.new_app("chat", llm_chat(prompt))
        run = await p.run("chat", {"message": "单次"})
        saved = await p.api.post(
            "/apps/chat/eval-set/cases/from-run", json={"run_id": run["run_id"]}
        )
        assert saved.status_code == 201, saved.text
        case = saved.json()
        assert (case["history"], case["turn"], case["restored"]) == ([], None, {})
        assert [c["kind"] for c in case["expect"]] == ["judge"]
        await p.api.put("/apps/chat/draft", json={"config": llm_chat(prompt)})
        p.gateway.replies = ["答", JUDGED]
        result = await eval_draft(p, "chat")
        assert (result["status"], result["score"]) == ("passed", 1.0), result
        assert sent(p, 0) == sent(p, 1) == [("user", "第 0 轮：单次")]


async def test_a_turn_without_a_reply_stays_a_message_in_the_case():
    async with Platform(["", "答二"]) as p:
        await p.new_app("chat", llm_chat())
        cid = await open_chat(p, "chat")
        await say(p, cid, "一")
        second = await say(p, cid, "二")
        saved = await p.api.post(
            "/apps/chat/eval-set/cases/from-run", json={"run_id": second["run_id"]}
        )
        assert saved.status_code == 201, saved.text
        assert saved.json()["history"] == [{"message": "一", "reply": None}]
        await p.api.put("/apps/chat/draft", json={"config": llm_chat()})
        p.gateway.replies = ["答二", JUDGED]
        result = await eval_draft(p, "chat")
        assert (result["status"], result["score"]) == ("passed", 1.0), result
        assert sent(p, -2) == sent(p, 1) == [("user", "一"), ("user", "二")]


async def test_case_history_is_checked():
    async with Platform([]) as p:
        await p.new_app("chat", llm_chat())
        resp = await p.api.post("/apps", json={"app": "plain", "title": "普通应用"})
        assert resp.status_code == 201, resp.text
        turn = {"message": "问", "reply": "答"}

        async def put(app: str, **case: Any) -> Any:
            body = {"cases": [{"id": "c", "input": {"message": "再问"}, **case}]}
            return await p.api.put(f"/apps/{app}/eval-set", json=body)

        refused = await put("plain", history=[turn])
        assert refused.status_code == 422
        [problem] = refused.json()["detail"]
        assert problem["loc"] == ["body", "cases", 0, "history"]
        assert problem["msg"] == "这个应用没有开对话，用例不能带历史"
        assert (await put("plain", turn=3)).status_code == 422
        assert (await put("plain")).status_code == 200

        long = [{"message": "问" * 8000, "reply": "答" * 6000}] * 2
        refused = await put("chat", history=long)
        assert refused.status_code == 422
        assert refused.json()["detail"][0]["msg"] == "历史的消息和回答合计最多 20000 字"
        token = policy.token("conv_x", "张三")
        refused = await put("chat", history=[turn], restored={token: "张三"})
        assert refused.status_code == 422
        [problem] = refused.json()["detail"]
        assert problem["loc"] == ["body", "cases", 0, "restored"]
        assert problem["msg"] == "还原表里有历史里没有的标记"
        assert (await put("chat", history=[turn] * 51)).status_code == 422
        assert (await put("chat", history=[turn], turn=0)).status_code == 422
        ok = await put("chat", history=[{"message": f"{token} 是谁"}], restored={token: "张三"})
        assert ok.status_code == 200, ok.text
        [kept] = ok.json()["cases"]
        assert kept["history"] == [{"message": f"{token} 是谁", "reply": None}]
        assert kept["turn"] is None and kept["restored"] == {token: "张三"}


async def test_a_masked_turn_saved_as_a_case_keeps_what_its_tokens_stand_for():
    async with Platform([], settings=with_policy(data_samples="masked")) as p:
        await p.new_app("ask-data", agent_chat())
        cid = await open_chat(p, "ask-data", input={"datasource": p.shop})
        token = policy.token(cid, EMAIL)
        p.gateway.replies = [
            call("t4", schema="sales", table="customers"),
            answer(f"第一个客户的邮箱是 {token}"),
            answer(f"他的邮箱是 {token}"),
        ]
        await say(p, cid, "第一个客户是谁？")
        second = await say(p, cid, "他的邮箱再说一遍")
        saved = await p.api.post(
            "/apps/ask-data/eval-set/cases/from-run", json={"run_id": second["run_id"]}
        )
        assert saved.status_code == 201, saved.text
        case = saved.json()
        assert case["history"] == [
            {"message": "第一个客户是谁？", "reply": f"第一个客户的邮箱是 {token}"}
        ]
        assert case["restored"] == {token: EMAIL} and case["turn"] == 2
        # Sent masked: a judge would be shown the address the model never saw, so the
        # model's short reply stays with equals.
        assert case["expect"] == [
            {"kind": "equals", "path": "reply", "value": f"他的邮箱是 {EMAIL}"}
        ]

        # The eval run's own tokens differ; the case's turn back as they are.
        await p.api.put("/apps/ask-data/draft", json={"config": agent_chat()})
        p.gateway.replies = [answer(f"他的邮箱是 {token}")]
        result = await eval_draft(p, "ask-data")
        assert (result["status"], result["score"]) == ("passed", 1.0), result
        assert EMAIL not in json.dumps(p.gateway.requests[-1]["body"], ensure_ascii=False)

        # Once the conversation is cleared away there is nothing left to save.
        await p.service.delete_conversation(cid)
        gone = await p.api.post(
            "/apps/ask-data/eval-set/cases/from-run", json={"run_id": second["run_id"]}
        )
        assert gone.status_code == 409
        assert gone.json()["detail"] == "这次运行属于已过期的会话，内容已清除"
