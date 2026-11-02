"""Schedules through the API and with the run service (docs/api/schedules.md): saving
checks them like POST /runs, "立即跑一次" and due schedules start runs marked with the
schedule, deleting an app deletes its schedules, and only builders change them."""

from datetime import UTC, datetime, timedelta

import pytest

from aep.api.schedules import starter
from aep.main import app
from aep.schedules import MemoryScheduleStore, Scheduler
from tests.test_auth import Platform as SignedPlatform
from tests.test_auth import signed
from tests.test_flow_tools import Platform
from tests.test_projects import bound_flow, project

DAILY = {"title": "每天早上的日报", "cron": "0 8 * * *", "input": {"text": "今天怎么样"}}


def scheduled(state, service) -> tuple[MemoryScheduleStore, Scheduler]:
    store = MemoryScheduleStore()
    scheduler = Scheduler(store, starter(service), service.status_of)
    state.schedules, state.scheduler = store, scheduler
    return store, scheduler


async def test_create_change_run_now_and_delete():
    async with Platform(["回答一", "回答二"]) as p:
        scheduled(app.state, p.service)
        made = await p.api.post(
            "/apps/hello/schedules",
            json={**DAILY, "cron": " 0  8 * * 1-5 ", "requester": "张三"},
        )
        assert made.status_code == 201, made.text
        s = made.json()
        assert s["schedule_id"].startswith("sch_") and s["app"] == "hello"
        assert (s["cron"], s["timezone"], s["created_by"]) == (
            "0 8 * * 1-5",
            "Asia/Shanghai",
            "张三",
        )
        assert s["enabled"] and s["last_run"] is None and s["last_error"] is None
        times = [datetime.fromisoformat(t) for t in s["next_runs"]]
        assert len(times) == 3 and times[0] == datetime.fromisoformat(s["next_run_at"])
        for t in times:
            local = t.astimezone(UTC) + timedelta(hours=8)
            assert (local.hour, local.minute) == (8, 0) and local.weekday() < 5
        sid = s["schedule_id"]

        listed = (await p.api.get("/apps/hello/schedules")).json()
        assert [x["schedule_id"] for x in listed] == [sid]
        assert (await p.api.get("/apps/nope/schedules")).status_code == 404

        changed = await p.api.patch(
            f"/schedules/{sid}", json={"cron": "30 6 1 * *", "timezone": "UTC"}
        )
        assert changed.status_code == 200, changed.text
        nxt = datetime.fromisoformat(changed.json()["next_run_at"])
        assert (nxt.day, nxt.hour, nxt.minute) == (1, 6, 30)
        assert changed.json()["title"] == DAILY["title"]  # Only what was given changes.

        off = (await p.api.patch(f"/schedules/{sid}", json={"enabled": False})).json()
        assert (off["enabled"], off["next_run_at"], off["next_runs"]) == (False, None, [])

        # Run now works on a schedule that is off and does not move its next time.
        ran = await p.api.post(f"/schedules/{sid}/run")
        assert ran.status_code == 201, ran.text
        await p.service.wait_idle()
        run = (await p.api.get(f"/runs/{ran.json()['run_id']}")).json()
        assert run["status"] == "waiting", run["error"]  # hello asks a person to confirm
        assert run["trigger"] == {"kind": "schedule", "schedule_id": sid, "title": "每天早上的日报"}
        assert run["created_by"] == "张三" and run["input"] == DAILY["input"]
        assert (await p.api.get("/runs")).json()[0]["trigger"]["schedule_id"] == sid
        got = (await p.api.get(f"/schedules/{sid}")).json()
        assert got["last_run"]["run_id"] == run["run_id"]
        assert got["last_run"]["status"] == "waiting" and got["next_run_at"] is None

        assert (await p.api.delete(f"/schedules/{sid}")).status_code == 204
        missing = await p.api.get(f"/schedules/{sid}")
        assert missing.status_code == 404 and missing.json()["detail"] == f"没有定时 {sid}"
        assert (await p.api.delete(f"/schedules/{sid}")).status_code == 404
        # The run it started stays.
        assert (await p.api.get(f"/runs/{run['run_id']}")).status_code == 200


@pytest.mark.parametrize(
    ("body", "status", "detail"),
    [
        ({"cron": "0 24 * * *"}, 422, "定时写法不对：第 2 段（时）的 24 超出 0–23"),
        ({"timezone": "Mars/Olympus"}, 422, "时区 Mars/Olympus 不认识"),
        ({"project": {"project_id": "jade-ibf"}}, 422, "应用「问答示例」不使用项目"),
    ],
)
async def test_what_cannot_run_is_not_saved(body, status, detail):
    async with Platform([]) as p:
        scheduled(app.state, p.service)
        resp = await p.api.post("/apps/hello/schedules", json={**DAILY, **body})
        assert resp.status_code == status, resp.text
        assert resp.json()["detail"] == detail


async def test_the_input_and_project_are_checked_as_for_runs():
    async with Platform([]) as p:
        store, _ = scheduled(app.state, p.service)
        missing = await p.api.post("/apps/hello/schedules", json={**DAILY, "input": {}})
        assert missing.status_code == 422
        assert missing.json()["detail"][0]["loc"] == ["body", "input", "text"]

        await p.api.post("/projects", json=project())
        await p.new_app("bound", bound_flow())
        body = {**DAILY, "input": {"question": "多少？"}}
        needs = await p.api.post("/apps/bound/schedules", json=body)
        assert needs.status_code == 422
        assert needs.json()["detail"] == "应用「bound」需要选择一个项目"
        wrong = await p.api.post(
            "/apps/bound/schedules",
            json={**body, "project": {"project_id": "jade-ibf", "environment": "uat"}},
        )
        assert wrong.status_code == 422
        bound = await p.api.post(
            "/apps/bound/schedules",
            json={**body, "project": {"project_id": "jade-ibf", "environment": "prod"}},
        )
        assert bound.status_code == 201, bound.text
        assert bound.json()["project"]["environment"] == "prod"
        # Changing it is checked the same way.
        sid = bound.json()["schedule_id"]
        cleared = await p.api.patch(f"/schedules/{sid}", json={"project": None})
        assert cleared.status_code == 422
        assert (await store.get(sid)).project is not None  # type: ignore[union-attr]

        for n in range(19):
            ok = await p.api.post("/apps/bound/schedules", json={**bound.json(), "title": f"{n}"})
            assert ok.status_code == 201, ok.text
        full = await p.api.post(
            "/apps/bound/schedules", json={**body, "project": bound.json()["project"]}
        )
        assert full.status_code == 409 and full.json()["detail"] == "每个应用最多 20 个定时"


async def test_due_schedules_run_and_an_app_takes_its_schedules_with_it():
    async with Platform(["回答"]) as p:
        store, scheduler = scheduled(app.state, p.service)
        await p.new_app("daily", bound_flow(uses=None, prompt="{{start.question}}"))
        made = await p.api.post(
            "/apps/daily/schedules",
            json={**DAILY, "input": {"question": "多少？"}, "requester": "张三"},
        )
        assert made.status_code == 201, made.text
        sid = made.json()["schedule_id"]
        due = datetime.fromisoformat(made.json()["next_run_at"])
        scheduler.clock = lambda: due + timedelta(seconds=10)
        assert await scheduler.tick() == 1
        await p.service.wait_idle()
        found = await store.get(sid)
        assert found is not None and found.last_run_id and found.last_error is None
        assert found.next_run_at == due + timedelta(days=1)
        run = (await p.api.get(f"/runs/{found.last_run_id}")).json()
        assert run["status"] == "done", run["error"]
        assert run["trigger"]["schedule_id"] == sid and run["created_by"] == "张三"

        # Deleting the app deletes its schedules.
        assert (await p.api.delete("/apps/daily")).status_code == 204
        assert await store.get(sid) is None


async def test_only_builders_change_schedules():
    p = SignedPlatform(enabled=True)
    scheduled(p.app.state, p.service)
    await p.user("builder", ["builder"], "王搭建")
    await p.user("lilei", ["user"], "李雷")
    async with signed(await p.signed_in("builder")) as api:
        made = await api.post("/apps/hello/schedules", json={**DAILY, "requester": "冒名"})
        assert made.status_code == 201, made.text
        assert made.json()["created_by"] == "王搭建"
    sid = made.json()["schedule_id"]
    async with signed(await p.signed_in("lilei")) as api:
        assert (await api.get("/apps/hello/schedules")).status_code == 200
        assert (await api.get(f"/schedules/{sid}")).status_code == 200
        for method, path in (
            ("POST", "/apps/hello/schedules"),
            ("PATCH", f"/schedules/{sid}"),
            ("DELETE", f"/schedules/{sid}"),
            ("POST", f"/schedules/{sid}/run"),
        ):
            resp = await api.request(method, path, json=DAILY)
            assert resp.status_code == 403, (method, path)
            assert resp.json()["detail"] == "没有权限：这项操作需要搭建者"
    async with p.client() as api:
        assert (await api.get("/apps/hello/schedules")).status_code == 401


async def test_preview_before_saving():
    async with Platform([]) as p:
        got = await p.api.get(
            "/schedules/preview", params={"cron": "0  8 * * 1-5", "timezone": "UTC"}
        )
        assert got.status_code == 200, got.text
        body = got.json()
        assert (body["cron"], body["timezone"]) == ("0 8 * * 1-5", "UTC")
        times = [datetime.fromisoformat(t) for t in body["next_runs"]]
        assert len(times) == 3 and all((t.hour, t.weekday() < 5) == (8, True) for t in times)
        default = (await p.api.get("/schedules/preview", params={"cron": "0 8 * * *"})).json()
        assert default["timezone"] == "Asia/Shanghai"
        bad = await p.api.get("/schedules/preview", params={"cron": "0 8 * *"})
        assert bad.status_code == 422
        assert bad.json()["detail"] == "定时写法不对：要写 5 段（分 时 日 月 周），用空格分开"
