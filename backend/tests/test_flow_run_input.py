"""A start input of type run: carry on from an earlier run of the same app
(docs/api/flow-apps.md, "接着以前的运行改")."""

import copy
from dataclasses import replace
from typing import Any

import pytest

from aep.app_access import ApiKeys, MemoryApiKeyStore
from aep.main import app
from aep.runtime.flow import nodes
from aep.runtime.flow.schema import input_schema
from tests.test_flow_tools import Platform


def versions_flow(**previous: Any) -> dict:
    """A first version from a note; given an earlier run, a new one from it."""
    field = {"key": "previous", "label": "上一版", "type": "run", "required": False, **previous}
    return {
        "graph": {
            "nodes": [
                {
                    "id": "start",
                    "type": "start",
                    "title": "开始",
                    "inputs": [
                        {"key": "note", "label": "说明", "type": "text", "required": True},
                        field,
                    ],
                },
                {
                    "id": "mode",
                    "type": "condition",
                    "title": "接着改吗",
                    "rules": [
                        {
                            "key": "again",
                            "label": "接着改",
                            "conditions": [{"left": "{{start.previous}}", "op": "not_empty"}],
                        }
                    ],
                },
                {
                    "id": "first",
                    "type": "end",
                    "title": "第一版",
                    "outputs": [{"key": "note", "from": ["{{start.note}}"]}],
                },
                {
                    "id": "next",
                    "type": "end",
                    "title": "新一版",
                    "outputs": [
                        {"key": "note", "from": ["{{start.note}}"]},
                        {"key": "previous", "from": ["{{start.previous}}"]},
                    ],
                },
            ],
            "edges": [
                {"from": "start", "to": "mode"},
                {"from": "mode", "branch": "again", "to": "next"},
                {"from": "mode", "branch": "else", "to": "first"},
            ],
        }
    }


async def problems(p: Platform, config: dict) -> dict[tuple, str]:
    resp = await p.api.put("/apps/versions/draft", json={"config": config})
    assert resp.status_code == 422, resp.text
    return {tuple(d["loc"][2:]): d["msg"] for d in resp.json()["detail"]}


async def test_run_inputs_are_checked_when_saved():
    async with Platform([]) as p:
        resp = await p.api.post("/apps", json={"app": "versions", "title": "版本"})
        assert resp.status_code == 201, resp.text
        good = versions_flow()
        resp = await p.api.put("/apps/versions/draft", json={"config": good})
        assert resp.status_code == 200, resp.text

        where = ("graph", "nodes", "start", "inputs", 1)
        assert await problems(p, versions_flow(default="run_1")) == {
            (*where, "default"): "以前的运行不能设默认值"
        }

        asking = copy.deepcopy(good)
        asking["graph"]["nodes"].insert(
            1,
            {
                "id": "ask",
                "type": "human",
                "title": "再给一版",
                "mode": "form",
                "prompt": "从哪一版改",
                "fields": [{"key": "from_run", "label": "哪一版", "type": "run"}],
            },
        )
        asking["graph"]["edges"][0] = {"from": "start", "to": "ask"}
        asking["graph"]["edges"].append({"from": "ask", "to": "mode"})
        found = await problems(p, asking)
        assert found[("graph", "nodes", "ask", "fields", 0, "type")] == (
            "只有开始节点的输入可以是以前的运行"
        )


async def test_a_run_input_puts_out_the_earlier_run():
    async with Platform([]) as p:
        await p.new_app("versions", versions_flow())
        first = await p.run("versions", {"note": "第一版"})
        assert first["status"] == "done", first["error"]
        assert first["result"] == {"note": "第一版"}

        second = await p.run("versions", {"note": "换第 3 个镜头", "previous": first["run_id"]})
        assert second["status"] == "done", second["error"]
        earlier = second["result"]["previous"]
        assert earlier == {
            "run_id": first["run_id"],
            "app_version": first["app_version"],
            "created_at": earlier["created_at"],
            "input": {"note": "第一版"},
            "result": {"note": "第一版"},
        }
        assert earlier["created_at"].startswith(first["created_at"][:19])
        # The run keeps the id it was given: a chain of runs is a chain of versions.
        assert second["input"] == {"note": "换第 3 个镜头", "previous": first["run_id"]}

        # Versions chain: the next one carries on from the second.
        third = await p.run("versions", {"note": "再换一个", "previous": second["run_id"]})
        assert third["status"] == "done", third["error"]
        assert third["result"]["previous"]["input"]["previous"] == first["run_id"]
        assert third["result"]["previous"]["result"]["note"] == "换第 3 个镜头"

        # Left blank, an optional run input is empty: the first version again.
        for blank in ("", "  ", None):
            run = await p.run("versions", {"note": "重新做", "previous": blank})
            assert run["status"] == "done", run["error"]
            assert run["result"] == {"note": "重新做"}


async def test_what_a_run_input_refuses():
    async with Platform([]) as p:
        await p.new_app("versions", versions_flow())
        await p.new_app("other", versions_flow())
        mine = await p.run("versions", {"note": "第一版"})
        theirs = await p.run("other", {"note": "别的应用"})

        async def refused(value: Any) -> str:
            resp = await p.api.post(
                "/runs", json={"app": "versions", "input": {"note": "改", "previous": value}}
            )
            assert resp.status_code == 422, resp.text
            [detail] = resp.json()["detail"]
            assert detail["loc"] == ["body", "input", "previous"]
            return detail["msg"]

        assert await refused(42) == "上一版要是运行 id"
        assert await refused("run_nope") == "上一版：这个应用没有这次运行"
        assert await refused(theirs["run_id"]) == "上一版：这个应用没有这次运行"

        runs = p.service._store._runs  # type: ignore[attr-defined]
        done = runs[mine["run_id"]]
        for status, message in (
            ("running", "上一版：这次运行还没做完，做完才能接着改"),
            ("waiting", "上一版：这次运行还没做完，做完才能接着改"),
            ("error", "上一版：这次运行没有做成，不能接着改"),
            ("cancelled", "上一版：这次运行没有做成，不能接着改"),
        ):
            runs[mine["run_id"]] = replace(done, status=status)
            assert await refused(mine["run_id"]) == message

        # A draft run (the design page's) may carry on too.
        runs[mine["run_id"]] = done
        await p.api.put("/apps/versions/draft", json={"config": versions_flow()})
        resp = await p.api.post(
            "/runs",
            json={
                "app": "versions",
                "draft": True,
                "input": {"note": "调试", "previous": mine["run_id"]},
            },
        )
        assert resp.status_code == 201, resp.text


async def test_an_earlier_run_gone_before_the_start_fails_it():
    async with Platform([]) as p:
        start = versions_flow()["graph"]["nodes"][0]
        found: dict[str, Any] = {"run_1": {"run_id": "run_1", "result": {"note": "第一版"}}}

        async def earlier(run_id: str) -> dict[str, Any] | None:
            return found.get(run_id)

        rt = nodes.Runtime(p.gateway.client, "mock", {}, {}, earlier_run=earlier)
        state = {"input": {"note": "改", "previous": "run_1"}}
        made = await nodes.run_start(start, state, rt)
        assert made.outputs["previous"]["result"] == {"note": "第一版"}
        # What an earlier run worked out may come from its data: kept as data values.
        assert made.data == ["previous"]

        found.clear()
        with pytest.raises(nodes.NodeFailure) as failed:
            await nodes.run_start(start, state, rt)
        assert failed.value.type == nodes.INPUT_ERROR
        assert failed.value.message == "上一版：那次运行已经不在了"


def test_apps_as_tools_take_run_inputs_as_ids() -> None:
    schema = input_schema(versions_flow()["graph"]["nodes"][0]["inputs"])
    assert schema["properties"]["previous"] == {
        "type": "string",
        "description": "上一版（这个应用以前做完的一次运行的 run_id）",
    }
    assert schema["required"] == ["note"]


async def test_app_keys_carry_on_from_their_app_runs():
    async with Platform([]) as p:
        keys = ApiKeys(MemoryApiKeyStore())
        app.state.api_keys = keys
        app.state.web_url = "https://aep.example.com"
        await p.new_app("versions", versions_flow())
        await p.new_app("other", versions_flow())
        _, secret = await keys.create("versions", "教练页面", "王搭建")
        headers = {"Authorization": f"Bearer {secret}"}

        async def start(input: dict[str, Any]) -> Any:
            return await p.api.post(
                "/v1/apps/versions/runs", json={"input": input, "wait": 5}, headers=headers
            )

        first = await start({"note": "第一版"})
        assert first.status_code == 201, first.text
        assert first.json()["status"] == "done"
        second = await start({"note": "改", "previous": first.json()["run_id"]})
        assert second.status_code == 201, second.text
        assert second.json()["result"]["previous"]["run_id"] == first.json()["run_id"]

        theirs = await p.run("other", {"note": "别的应用"})
        refused = await start({"note": "改", "previous": theirs["run_id"]})
        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"] == "上一版：这个应用没有这次运行"
