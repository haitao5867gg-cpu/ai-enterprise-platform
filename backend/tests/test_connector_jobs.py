"""Connector long-running jobs and run progress (docs/api/connector-jobs.md)."""

import asyncio
import re
from typing import Any

import pytest

from aep.app_access import ApiKeys, MemoryApiKeyStore, run_result
from aep.main import app
from aep.runtime.store import MemoryRunStore, RunRecord, current_progress
from aep.tools import jobs
from tests import fake_connector
from tests.conftest import SETTINGS
from tests.test_flow_tools import Platform, answer, call


@pytest.fixture(autouse=True)
def quick_polls(monkeypatch):
    monkeypatch.setattr(jobs, "FAST_SECONDS", 0.01)
    monkeypatch.setattr(jobs, "SLOW_SECONDS", 0.01)
    monkeypatch.setattr(jobs, "UNREACHABLE_SECONDS", 0.3)


class Progress(MemoryRunStore):
    """Notes every progress a run reports."""

    def __init__(self) -> None:
        super().__init__()
        self.reported: list[dict[str, Any]] = []

    async def set_progress(self, run_id: str, progress: dict[str, Any]) -> None:
        self.reported.append(progress)
        await super().set_progress(run_id, progress)


class Jobs(Platform):
    def __init__(self, settings=SETTINGS, replies: list[str] | None = None) -> None:
        self.connector = fake_connector.JobConnector()
        server = self.connector.build()
        super().__init__(replies or [], settings=settings)
        self.store = Progress()
        self.service._store = self.store
        tools = self.service._ctx.tools
        resolve = tools._resolve
        tools._resolve = lambda url: (
            self.connector.resolve(server) if url == fake_connector.JOBS_URL else resolve(url)
        )

    async def __aenter__(self) -> "Jobs":
        await super().__aenter__()
        app.state.mcp_tools = self.service._ctx.tools
        self.render = (
            await self.sources.create("渲染", "mcp", fake_connector.JOBS_URL)
        ).datasource_id
        return self


def render_flow(source: str, tool: str = "render", args: dict | None = None) -> dict:
    return {
        "graph": {
            "nodes": [
                {
                    "id": "start",
                    "type": "start",
                    "title": "开始",
                    "inputs": [{"key": "name", "label": "名字", "type": "text", "required": True}],
                },
                {
                    "id": "render",
                    "type": "tool",
                    "title": "剪辑渲染",
                    "datasource": source,
                    "tool": tool,
                    "args": args or {"name": "{{start.name}}"},
                },
                {
                    "id": "end",
                    "type": "end",
                    "title": "结束",
                    "outputs": [{"key": "out", "from": ["{{render.result}}"]}],
                },
            ],
            "edges": [{"from": "start", "to": "render"}, {"from": "render", "to": "end"}],
        }
    }


def failure(run: dict) -> tuple[str | None, str | None]:
    """The run's error type and message, without the node the message names first."""
    error = run["error"] and run["error"].removeprefix("节点「剪辑渲染」(render)：")
    return run["failed"]["error_type"] if run["failed"] else None, error


async def test_a_tool_node_follows_a_job_and_shows_its_progress():
    async with Jobs() as p:
        await p.new_app("video", render_flow(p.render))
        run = await p.run("video", {"name": "a"})
        assert run["status"] == "done", run["error"]
        assert run["result"]["out"] == {"file": "a.mp4"}
        assert p.connector.status_calls == 3
        [key] = p.connector.keys
        assert key is not None and re.fullmatch(r"[0-9a-f]{64}", key)
        assert key == jobs.job_key(run["run_id"], "render", 0, "render", {"name": "a"})
        assert [(r["node"], r["percent"], r["message"]) for r in p.store.reported] == [
            ("render", 0, "渲染第 0 段，共 3 段"),
            ("render", 33, "渲染第 1 段，共 3 段"),
            ("render", 67, "渲染第 2 段，共 3 段"),
            ("render", 100, "渲染第 3 段，共 3 段"),
        ]
        # Shown only while running: the done run shows none.
        assert run["progress"] is None


async def test_quick_jobs_and_ordinary_tools_need_no_following():
    async with Jobs() as p:
        await p.new_app("quick", render_flow(p.render, "quick"))
        await p.new_app("plain", render_flow(p.render, "plain"))
        quick = await p.run("quick", {"name": "a"})
        plain = await p.run("plain", {"name": "b"})
        assert quick["result"]["out"] == {"file": "a"}
        assert plain["result"]["out"] == {"file": "b"}
        assert p.connector.status_calls == 0
        assert p.store.reported == []


async def test_failed_jobs_fail_the_node_and_a_retry_starts_a_new_job():
    async with Jobs() as p:
        args = {"name": "{{start.name}}", "fail": True}
        await p.new_app("video", render_flow(p.render, args=args))
        run = await p.run("video", {"name": "a"})
        assert failure(run) == ("tool_error", "工具报错：ffmpeg 退出码 1")
        assert run["failed"]["retryable"] is True
        resp = await p.api.post(f"/runs/{run['run_id']}/retry")
        assert resp.status_code == 202
        await p.service.wait_idle()
        # Same key: the failed job is not reused, a new one starts.
        assert len(set(p.connector.keys)) == 1 and p.connector.started == 2


async def test_jobs_the_connector_forgets_cancels_or_garbles():
    async with Jobs() as p:
        await p.new_app("video", render_flow(p.render))
        p.connector.forget = True
        run = await p.run("video", {"name": "a"})
        assert failure(run) == ("tool_error", "工具报错：查任务进度失败：没有这个任务")

        p.connector.forget, p.connector.bad = False, True
        run = await p.run("video", {"name": "b"})
        garbled = "工具报错：工具返回的任务格式不对：不认识状态 'sleeping'"
        assert failure(run) == ("tool_error", garbled)


async def test_an_unreachable_connector_is_asked_again_until_it_answers():
    async with Jobs() as p:
        await p.new_app("video", render_flow(p.render))

        async def run_with_outage(name: str, calls: int) -> dict:
            # The connector goes away right after handing the job back.
            p.connector.keys.clear()
            started = asyncio.create_task(p.run("video", {"name": name}))
            while not p.connector.keys:
                await asyncio.sleep(0.001)
            p.connector.down = calls
            run = await started
            p.connector.down = 0
            return run

        run = await run_with_outage("a", 2)
        assert run["status"] == "done", run["error"]
        run = await run_with_outage("b", 10**6)
        error_type, error = failure(run)
        assert error_type == "tool_error"
        assert error.startswith("连不上数据源「渲染」")


async def test_a_job_past_its_time_is_cancelled():
    settings = SETTINGS.model_copy(update={"tool_job_seconds": 0.05})
    async with Jobs(settings) as p:
        args = {"name": "{{start.name}}", "steps": 10**6}
        await p.new_app("video", render_flow(p.render, args=args))
        run = await p.run("video", {"name": "a"})
        assert failure(run) == ("timeout", "长任务超过 0.05 秒还没做完，已经取消")
        assert p.connector.cancelled == ["j1"]


async def test_a_status_answer_after_the_jobs_time_does_not_count():
    settings = SETTINGS.model_copy(update={"tool_job_seconds": 0.1})
    async with Jobs(settings) as p:
        # Done at the first status call, but that call answers only after the job's time.
        p.connector.slow = 1.0
        args = {"name": "{{start.name}}", "steps": 1}
        await p.new_app("video", render_flow(p.render, args=args))
        run = await p.run("video", {"name": "a"})
        assert failure(run) == ("timeout", "长任务超过 0.1 秒还没做完，已经取消")
        assert p.connector.cancelled == ["j1"]


async def test_cancelling_the_run_cancels_its_job():
    async with Jobs() as p:
        args = {"name": "{{start.name}}", "steps": 10**6}
        await p.new_app("video", render_flow(p.render, args=args))
        resp = await p.api.post("/runs", json={"app": "video", "input": {"name": "a"}})
        run_id = resp.json()["run_id"]
        while p.connector.status_calls < 2:
            await asyncio.sleep(0.005)
        shown = (await p.api.get(f"/runs/{run_id}")).json()
        assert shown["step"]["node"] == "render"
        assert shown["progress"]["node"] == "render"
        assert shown["progress"]["percent"] == 0
        assert shown["progress"]["message"].startswith("渲染第")
        assert (await p.api.post(f"/runs/{run_id}/cancel")).status_code == 202
        await p.service.wait_idle()
        assert p.connector.cancelled == ["j1"]
        assert (await p.api.get(f"/runs/{run_id}")).json()["status"] == "cancelled"


async def test_a_run_cancelled_elsewhere_stops_its_job():
    async with Jobs() as p:
        args = {"name": "{{start.name}}", "steps": 10**6}
        await p.new_app("video", render_flow(p.render, args=args))
        resp = await p.api.post("/runs", json={"app": "video", "input": {"name": "a"}})
        run_id = resp.json()["run_id"]
        while p.connector.status_calls < 2:
            await asyncio.sleep(0.005)
        # Another process cancelled it: only the record says so.
        await p.store.set_status(run_id, "cancelled", "不要了", expected=("running",))
        await p.service.wait_idle()
        assert p.connector.cancelled == ["j1"]
        assert (await p.api.get(f"/runs/{run_id}")).json()["status"] == "cancelled"


async def started_run(p: Jobs, app_name: str) -> str:
    """A run of the app whose job has been asked about twice."""
    resp = await p.api.post("/runs", json={"app": app_name, "input": {"name": "a"}})
    run_id = resp.json()["run_id"]
    while p.connector.status_calls < 2:
        await asyncio.sleep(0.005)
    return run_id


async def test_a_cancelled_run_is_not_retried_by_the_node():
    async with Jobs() as p:
        flow = render_flow(p.render, args={"name": "{{start.name}}", "steps": 10**6})
        flow["graph"]["nodes"][1]["on_error"] = {
            "retry": {"max": 2, "interval_seconds": 0},
            "then": "default",
            "default": {"result": "没渲染"},
        }
        await p.new_app("video", flow)
        run_id = await started_run(p, "video")
        await p.store.set_status(run_id, "cancelled", "不要了", expected=("running",))
        await p.service.wait_idle()
        run = (await p.api.get(f"/runs/{run_id}")).json()
        assert (run["status"], run["error"]) == ("cancelled", "不要了")
        # No new job for a retry, and the default was not used either.
        assert p.connector.started == 1 and p.connector.cancelled == ["j1"]
        assert run["result"] is None


async def test_an_agent_stops_when_its_run_is_cancelled():
    replies = [call("t1", name="a", steps=10**6), answer("渲染好了")]
    async with Jobs(replies=replies) as p:
        flow = render_flow(p.render)
        flow["graph"]["nodes"][1] = {
            "id": "render",
            "type": "agent",
            "title": "剪辑渲染",
            "model": None,
            "skills": [],
            "task": "把 {{start.name}} 渲染出来",
            "tools": [{"datasource": p.render, "tools": ["render"]}],
            "max_steps": 4,
        }
        flow["graph"]["nodes"][2]["outputs"] = [{"key": "out", "from": ["{{render.text}}"]}]
        await p.new_app("video", flow)
        run_id = await started_run(p, "video")
        await p.store.set_status(run_id, "cancelled", "不要了", expected=("running",))
        await p.service.wait_idle()
        assert (await p.api.get(f"/runs/{run_id}")).json()["status"] == "cancelled"
        assert p.connector.cancelled == ["j1"]
        # The agent did not go back to its model with the cancel as a tool error.
        assert len(p.gateway.requests) == 1


async def test_a_stopping_process_leaves_the_job_for_the_next_start():
    async with Jobs() as p:
        args = {"name": "{{start.name}}", "steps": 6}
        await p.new_app("video", render_flow(p.render, args=args))
        run_id = await started_run(p, "video")
        # The process stops: its runs' tasks are cancelled, the runs stay running.
        p.service._running[run_id].cancel()
        await p.service.wait_idle()
        assert p.connector.cancelled == []
        assert (await p.service.get(run_id)).record.status == "running"

        assert await p.service.recover() == 1
        await p.service.wait_idle()
        run = (await p.api.get(f"/runs/{run_id}")).json()
        assert run["status"] == "done", run["error"]
        assert run["result"]["out"] == {"file": "a.mp4"}
        # The node ran again with the same key and picked its job up.
        assert len(p.connector.keys) == 2 and len(set(p.connector.keys)) == 1
        assert p.connector.started == 1


async def test_job_keys_differ_by_visit_and_arguments():
    one = jobs.job_key("r1", "render", 0, "render", {"name": "a"})
    assert one == jobs.job_key("r1", "render", 0, "render", {"name": "a"})
    assert one != jobs.job_key("r1", "render", 1, "render", {"name": "a"})
    assert one != jobs.job_key("r1", "render", 0, "render", {"name": "b"})
    assert one != jobs.job_key("r2", "render", 0, "render", {"name": "a"})


async def test_job_tools_are_not_listed():
    async with Jobs() as p:
        tools = (await p.api.get(f"/datasources/{p.render}/tools")).json()
        names = {t["name"] for t in tools}
        assert names == {"render", "quick", "plain"}


def test_progress_is_shown_only_for_the_running_step():
    progress = {"node": "render", "percent": 5, "message": None, "updated_at": "t"}
    running = RunRecord(
        "r", "video", "running", {}, step={"node": "render", "title": "渲染"}, progress=progress
    )
    assert current_progress(running) == progress
    assert current_progress(RunRecord("r", "video", "done", {}, step=running.step)) is None
    moved = RunRecord("r", "video", "running", {}, step={"node": "end"}, progress=progress)
    assert current_progress(moved) is None
    waiting = RunRecord("r", "video", "waiting", {}, step=running.step, progress=progress)
    assert current_progress(waiting) is None


async def test_the_app_api_shows_the_step_and_progress():
    async with Jobs() as p:
        args = {"name": "{{start.name}}", "steps": 10**6}
        await p.new_app("video", render_flow(p.render, args=args))
        keys = ApiKeys(MemoryApiKeyStore())
        app.state.api_keys = keys
        app.state.web_url = "https://aep.example.com"
        _, secret = await keys.create("video", "教练页面", "王搭建")
        headers = {"Authorization": f"Bearer {secret}"}
        resp = await p.api.post(
            "/v1/apps/video/runs", json={"input": {"name": "a"}}, headers=headers
        )
        assert resp.status_code == 201, resp.text
        run_id = resp.json()["run_id"]
        while p.connector.status_calls < 2:
            await asyncio.sleep(0.005)
        shown = (await p.api.get(f"/v1/runs/{run_id}", headers=headers)).json()
        assert shown["step"] == {"node": "render", "title": "剪辑渲染"}
        assert shown["progress"]["node"] == "render"
        assert shown["progress"]["percent"] is not None
        await p.api.post(f"/runs/{run_id}/cancel")
        await p.service.wait_idle()
        view = await p.service.get(run_id)
        result = run_result(view, "https://aep.example.com")
        assert result["step"]["node"] == "render" and result["progress"] is None
