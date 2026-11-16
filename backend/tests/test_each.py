"""The each node (逐项处理): one step for every item of a list, results in the list's order
(docs/api/each.md)."""

import asyncio
import copy
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from aep.runtime.flow import each, files, graph
from aep.runtime.flow.nodes import Runtime
from tests.test_flow_tools import Platform

STUDIO_URL = "http://studio.test/mcp"
SHOTS = [
    {"line": "冬天减脂难吗", "start": 0.0},
    {"line": "关键是热量缺口", "start": 2.5},
    {"line": "每天少吃三百大卡", "start": 4.0},
]


class Studio:
    """A connector that plans shots and picks a clip for each; it notes how many picks run
    at once and fails a line a given number of times."""

    def __init__(self, shots: list[Any] | None = None) -> None:
        self.shots = SHOTS if shots is None else shots
        self.running = 0
        self.most = 0
        self.picks: list[str] = []
        self.failing: dict[str, int] = {}
        self.server = MCPServer("fake-studio")

        @self.server.tool()
        def plan(topic: str) -> list:
            """The shots for a topic."""
            return self.shots

        @self.server.tool()
        async def pick(line: str, position: int, token: str, whole: str = "") -> dict:
            """A clip for one line."""
            if token != "s3cret":
                raise ToolError("bad token")
            self.picks.append(line)
            self.running += 1
            self.most = max(self.most, self.running)
            await asyncio.sleep(0.02)
            self.running -= 1
            if self.failing.get(line):
                self.failing[line] -= 1
                raise ToolError(f"没找到「{line}」的素材")
            return {"clip": f"{position}-{line}", "whole": whole}


def flow(**node: Any) -> dict:
    """Plan shots, then pick a clip for each with the each node ``pick``."""
    pick = {
        "id": "pick",
        "type": "each",
        "title": "逐个挑镜头",
        "items": "{{plan.result}}",
        "do": {
            "type": "tool",
            "datasource": "STUDIO",
            "tool": "pick",
            "args": {
                "line": "{{item.line}}",
                "position": "{{item.position}}",
                "token": "{{secret.crm_token}}",
                "whole": "第 {{item.position}} 项：{{item.value}}",
            },
        },
        "concurrency": 2,
        **node,
    }
    return {
        "env": {"secrets": ["crm_token"]},
        "graph": {
            "nodes": [
                {
                    "id": "start",
                    "type": "start",
                    "title": "开始",
                    "inputs": [{"key": "topic", "label": "主题", "type": "text"}],
                },
                {
                    "id": "plan",
                    "type": "tool",
                    "title": "排镜头",
                    "datasource": "STUDIO",
                    "tool": "plan",
                    "args": {"topic": "{{start.topic}}"},
                },
                pick,
                {
                    "id": "end",
                    "type": "end",
                    "title": "结束",
                    "outputs": [
                        {"key": "clips", "from": ["{{pick.results}}"]},
                        {"key": "failed", "from": ["{{pick.failed}}"]},
                        {"key": "count", "from": ["{{pick.count}}"]},
                    ],
                },
            ],
            "edges": [
                {"from": "start", "to": "plan"},
                {"from": "plan", "to": "pick"},
                {"from": "pick", "to": "end"},
            ],
        },
    }


class Each(Platform):
    def __init__(self, replies: list[str] | None = None, shots: list[Any] | None = None) -> None:
        self.studio = Studio(shots)
        super().__init__(replies or ["你好"], servers={STUDIO_URL: self.studio.server})
        self.progress: list[tuple[str, float | None, str | None]] = []
        noted = self.service.report_progress

        async def report(run_id: str, node: str, value: float | None, message: str | None):
            self.progress.append((node, value, message))
            await noted(run_id, node, value, message)

        self.service.report_progress = report  # type: ignore[method-assign]

    async def __aenter__(self) -> "Each":
        await super().__aenter__()
        self.source = (await self.sources.create("剪辑", "mcp", STUDIO_URL)).datasource_id
        return self

    def config(self, **node: Any) -> dict:
        text = repr(flow(**node)).replace("STUDIO", self.source)
        return eval(text)  # noqa: S307 - a literal made just above

    async def app(self, config: dict, name: str = "cut") -> None:
        await self.new_app(name, config)
        resp = await self.api.put(f"/apps/{name}/secrets/crm_token", json={"value": "s3cret"})
        assert resp.status_code == 204, resp.text


async def test_every_item_gets_the_step_and_results_keep_the_order():
    async with Each() as p:
        await p.app(p.config())
        run = await p.run("cut", {"topic": "冬天减脂"})
        assert run["status"] == "done", run["error"]
        clips = run["result"]["clips"]
        assert [c["result"]["clip"] for c in clips] == [
            "1-冬天减脂难吗",
            "2-关键是热量缺口",
            "3-每天少吃三百大卡",
        ]
        # A reference inside text gets the item as JSON text.
        assert clips[0]["result"]["whole"] == ('第 1 项：{"line": "冬天减脂难吗", "start": 0.0}')
        assert run["result"]["failed"] == [] and run["result"]["count"] == 3
        # At most `concurrency` items at once; progress after each item.
        assert p.studio.most == 2
        assert [m for node, _, m in p.progress if node == "pick"] == [
            "已完成 1/3 项",
            "已完成 2/3 项",
            "已完成 3/3 项",
        ]
        assert [v for node, v, _ in p.progress if node == "pick"][-1] == 1.0


async def test_an_empty_list_goes_on_and_a_wrong_value_fails():
    async with Each(shots=[]) as p:
        await p.app(p.config())
        run = await p.run("cut", {"topic": "x"})
        assert run["status"] == "done", run["error"]
        assert run["result"] == {"clips": [], "failed": [], "count": 0}
        assert p.studio.picks == []

    async with Each(shots={"not": "a list"}) as p:  # type: ignore[arg-type]
        await p.app(p.config())
        run = await p.run("cut", {"topic": "x"})
        assert run["status"] == "error"
        assert "「items」取到的不是列表" in run["error"]
        assert run["failed"]["error_type"] == "input_error"

    async with Each(shots=[{"line": str(i)} for i in range(101)]) as p:
        await p.app(p.config())
        run = await p.run("cut", {"topic": "x"})
        assert run["status"] == "error"
        assert "最多处理 100 项，这次有 101 项" in run["error"]
        assert p.studio.picks == []


async def test_a_failed_item_stops_the_node_or_is_skipped():
    async with Each() as p:
        p.studio.failing = {"关键是热量缺口": 9}
        await p.app(p.config())
        run = await p.run("cut", {"topic": "x"})
        assert run["status"] == "error"
        assert run["failed"]["error_type"] == "tool_error"
        assert "第 2 项：工具报错：没找到「关键是热量缺口」的素材" in run["error"]

    async with Each() as p:
        p.studio.failing = {"关键是热量缺口": 9}
        await p.app(p.config(on_item_error="skip"))
        run = await p.run("cut", {"topic": "x"})
        assert run["status"] == "done", run["error"]
        clips = run["result"]["clips"]
        assert clips[1] is None and clips[0] and clips[2]
        assert run["result"]["failed"] == [
            {
                "position": 2,
                "error_type": "tool_error",
                "error_message": "工具报错：没找到「关键是热量缺口」的素材",
            }
        ]
        assert [m for node, _, m in p.progress if node == "pick"][-1] == (
            "已完成 3/3 项，跳过 1 项"
        )

    async with Each() as p:
        p.studio.failing = {s["line"]: 9 for s in SHOTS}
        await p.app(p.config(on_item_error="skip"))
        run = await p.run("cut", {"topic": "x"})
        assert run["status"] == "error"
        assert "每一项都失败了，第 1 项：工具报错：没找到「冬天减脂难吗」的素材" in run["error"]


async def test_a_retry_tries_again_only_the_item_that_failed():
    async with Each() as p:
        p.studio.failing = {"关键是热量缺口": 1}
        await p.app(p.config(on_error={"retry": {"max": 1, "interval_seconds": 0}}))
        run = await p.run("cut", {"topic": "x"})
        assert run["status"] == "done", run["error"]
        assert sorted(p.studio.picks) == sorted([*(s["line"] for s in SHOTS), "关键是热量缺口"])

    # After the retries the node's on_error decides: here, the error branch.
    async with Each() as p:
        p.studio.failing = {"关键是热量缺口": 9}
        config = p.config(on_error={"retry": {"max": 1}, "then": "default", "default": {}})
        await p.app(config)
        run = await p.run("cut", {"topic": "x"})
        assert run["status"] == "done", run["error"]
        assert run["result"]["clips"] is None
        assert p.studio.picks.count("关键是热量缺口") == 2


async def test_a_model_step_reads_the_item():
    async with Each(replies=["卧推侧面.mp4"]) as p:
        config = p.config(
            do={
                "type": "llm",
                "model": "mock",
                "prompt": (
                    "第 {{item.position}} 句配音「{{item.line}}」，"
                    "主题{{start.topic}}，挑一段素材。"
                ),
            }
        )
        await p.app(config)
        run = await p.run("cut", {"topic": "冬天减脂"})
        assert run["status"] == "done", run["error"]
        assert run["result"]["clips"] == [{"text": "卧推侧面.mp4"}] * 3
        prompts = sorted(r["body"]["messages"][-1]["content"] for r in p.gateway.requests)
        assert prompts == [
            "第 1 句配音「冬天减脂难吗」，主题冬天减脂，挑一段素材。",
            "第 2 句配音「关键是热量缺口」，主题冬天减脂，挑一段素材。",
            "第 3 句配音「每天少吃三百大卡」，主题冬天减脂，挑一段素材。",
        ]
        # The step's own messages are not the run's: only the result says what it made.
        assert not [m for m in run["messages"] if m["node"] == "pick"]


async def test_an_item_from_a_data_value_is_a_data_value():
    rt = Runtime(llm_client=None, default_model="mock", skills={}, env={})  # type: ignore[arg-type]
    state = {"vars": {"plan": {"result": SHOTS}}, "data": {"plan": ["result"]}}
    item = each._with_item(state, SHOTS[1], 2, data=True, quoted=False)
    assert item["vars"]["item"] == {**SHOTS[1], "value": SHOTS[1], "position": 2}
    assert rt.holds_data(item, "{{item.line}}") and rt.holds_data(item, "{{item.value}}")
    assert not rt.holds_data(item, "{{item.position}}")
    assert rt.lookup(item)("item", "start") == 2.5
    plain = each._with_item({"vars": {}}, "一句话", 1, data=False, quoted=False)
    assert plain["vars"]["item"] == {"value": "一句话", "position": 1}
    assert not rt.holds_data(plain, "{{item.value}}")


async def test_saving_checks_the_node_and_its_step():
    async with Each() as p:
        resp = await p.api.post("/apps", json={"app": "cut", "title": "cut"})
        assert resp.status_code == 201

        async def problems(config: dict) -> list[tuple[tuple, str]]:
            resp = await p.api.put("/apps/cut/draft", json={"config": config})
            if resp.status_code == 200:
                return []
            assert resp.status_code == 422, resp.text
            return [(tuple(e["loc"][4:]), e["msg"]) for e in resp.json()["detail"]]

        assert await problems(p.config()) == []
        node = ("pick",)
        found = await problems(p.config(items="{{plan.result}} 和别的", concurrency=11))
        assert (node + ("items",), "要插一个变量：对哪个列表逐项处理") in found
        assert (node + ("concurrency",), "同时处理几项要在 1–10 之间") in found
        found = await problems(p.config(do={"type": "human"}, on_item_error="later"))
        assert (
            node + ("do", "type"),
            "每一项只能做这几种：调用模型、调用工具、代码、文本拼接、语音转写",
        ) in found
        assert (
            node + ("on_item_error",),
            "某一项失败时只能是 stop（停下）或 skip（跳过）",
        ) in found
        assert (node + ("do",), "要设置每一项做什么") in await problems(p.config(do=None))
        # The step is checked as a node of its kind, its problems under `do`.
        config = p.config()
        config["graph"]["nodes"][2]["do"] |= {"tool": "", "title": "挑", "on_error": {}}
        found = await problems(config)
        assert (node + ("do", "title"), "每一项做的那一步不写 title") in found
        assert (node + ("do", "on_error"), "每一项做的那一步不写 on_error") in found
        assert any(loc == node + ("do", "tool") for loc, _ in found)
        # {{item.*}} only in a step; `item` is no node's id.
        config = p.config()
        config["graph"]["nodes"][3]["outputs"].append({"key": "x", "from": ["{{item.value}}"]})
        assert (
            ("end", "outputs", 3, "from", 0),
            "{{item.*}} 只能写在逐项处理节点的「每一项做什么」里",
        ) in await problems(config)
        assert (
            node + ("items",),
            "{{item.*}} 只能写在逐项处理节点的「每一项做什么」里",
        ) in await problems(p.config(items="{{item.value}}"))
        config = p.config()
        config["graph"]["nodes"][1]["id"] = "item"
        assert (("item", "id"), "item 是保留的标识") in await problems(config)
        # A secret only in a tool step's arguments.
        found = await problems(
            p.config(do={"type": "template", "template": "{{secret.crm_token}}"})
        )
        assert (node + ("do", "template"), "密钥只能写在工具节点的参数里") in found
        # A code step's code is taken as it is.
        code = "def main(inputs):\n    return {'n': '{{not.a_ref}}'}"
        step = {
            "type": "code",
            "code": code,
            "inputs": {"line": "{{item.line}}"},
            "outputs": [{"key": "n", "type": "string"}],
        }
        assert await problems(p.config(do=step)) == []
        # The checks made with the whole configuration see the step too.
        config = p.config()
        config["graph"]["nodes"][2]["do"]["save_media"] = {"library": "lib-1"}
        assert (
            node + ("do", "save_media", "library"),
            "存进的媒体库要以“写”权限绑定到应用",
        ) in await problems(config)
        step = {"type": "llm", "model": "mock", "prompt": "{{item.line}}", "history": True}
        assert (
            node + ("do", "history"),
            "应用没有打开对话，不能带会话历史",
        ) in await problems(p.config(do=step))


async def test_what_the_step_uses_counts_as_what_the_app_uses():
    config = flow(
        do={
            "type": "llm",
            "model": "company-vision",
            "skills": [{"name": "coach-style", "version": 2}],
            "prompt": "{{item.line}}",
        }
    )
    assert files.model_refs(config) == (["company-vision"], False)
    assert graph.skill_refs(config) == [("coach-style", 2)]
    # The data outbound policy checks the step's model before the run starts.
    async with Each() as p:
        used = await p.service._models("cut", config, {})  # noqa: SLF001
        assert ("company-vision", "逐个挑镜头") in used
    tooled = flow()
    assert files.datasource_tools(tooled) == {"STUDIO": ["plan", "pick"]}
    assert files.datasource_ids(tooled) == ["STUDIO"]
    coded = flow(do={"type": "code", "code": "def main(inputs):\n    return {}"})
    steps = graph.steps(coded)
    assert [s["type"] for s in steps] == ["start", "tool", "each", "code", "end"]
    assert "do" not in steps[2] and steps[3]["id"] == "pick"
    # An unchecked configuration is read without failing.
    broken = copy.deepcopy(tooled)
    broken["graph"]["nodes"][2]["do"] = "no"
    broken["graph"]["nodes"].append("not a node")
    assert files.datasource_tools(broken) == {"STUDIO": ["plan"]}


async def test_one_each_node_can_be_tried_with_a_list():
    async with Each() as p:
        await p.app(p.config())
        tried = (
            await p.api.post(
                "/apps/cut/nodes/pick/test", json={"values": {"plan.result": SHOTS[:2]}}
            )
        ).json()
        assert tried["status"] == "done", tried
        assert [r["result"]["clip"] for r in tried["outputs"]["results"]] == [
            "1-冬天减脂难吗",
            "2-关键是热量缺口",
        ]
        listed = (await p.api.get("/node-types")).json()
        found = next(t for t in listed if t["type"] == "each")
        assert found["label"] == "逐项处理" and found["category"] == "logic"
        assert found["defaults"]["do"]["type"] == "llm"
        assert found["error_handling"] is True
