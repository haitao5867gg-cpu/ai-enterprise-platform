"""Sign-in, roles and the record of changes (docs/api/auth.md), on in-memory storage.
Postgres-backed tests: test_auth_postgres.py."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from aep.auth import (
    LOCAL_USER,
    AuthService,
    MemoryAuthStore,
    hash_password,
    required,
    verify_password,
)
from aep.datasources import MemoryDataSourceStore
from aep.main import create_app
from aep.runtime.catalog import AppCatalog
from aep.runtime.registry import AppContext, default_apps
from aep.runtime.service import RunService
from aep.runtime.store import MemoryRunStore
from tests.conftest import SETTINGS, FakeGateway, said

PASSWORD = "correct-horse"


class Clock:
    def __init__(self) -> None:
        self.at = datetime(2026, 11, 2, 8, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.at


class Platform:
    """The API with sign-in on (or off), a run service with the ``hello`` app and an
    in-memory account store."""

    def __init__(self, enabled: bool = True) -> None:
        self.gateway = FakeGateway(replies=["回答"])
        self.service = RunService(
            MemoryRunStore(),
            InMemorySaver(),
            default_apps(),
            AppContext(llm_client=self.gateway.client, settings=SETTINGS),
        )
        self.store = MemoryAuthStore()
        self.clock = Clock()
        self.auth = AuthService(self.store, enabled=enabled, now=self.clock)
        self.app = create_app()
        self.app.state.run_service = self.service
        self.app.state.auth = self.auth
        sources = MemoryDataSourceStore()
        self.app.state.datasources = sources
        self.app.state.catalog = AppCatalog(self.service, sources, SETTINGS)
        self.app.state.skills = self.service.skills

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )

    async def user(self, username: str, roles: list[str], name: str | None = None) -> str:
        user = await self.auth.create(username, name or username, roles, PASSWORD)
        return user.user_id

    async def signed_in(self, username: str) -> httpx.AsyncClient:
        client = self.client()
        resp = await client.post("/auth/login", json={"username": username, "password": PASSWORD})
        assert resp.status_code == 200, resp.text
        return client


@asynccontextmanager
async def signed(client: httpx.AsyncClient) -> AsyncIterator[httpx.AsyncClient]:
    """A signed-in client (already open) for a block; closed after it."""
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def platform() -> Platform:
    return Platform()


def test_passwords_are_salted_scrypt():
    stored = hash_password(PASSWORD)
    assert stored.startswith("scrypt$") and PASSWORD not in stored
    assert stored != hash_password(PASSWORD)
    assert verify_password(PASSWORD, stored)
    assert not verify_password("wrong-horse", stored)
    assert not verify_password(PASSWORD, "garbage")


def test_route_rules():
    assert required("GET", "/healthz") is None
    assert required("POST", "/auth/login") is None
    assert required("GET", "/runs") == "user"
    assert required("POST", "/runs") == "user"
    assert required("POST", "/datasources/files") == "user"
    assert required("POST", "/datasources") == "builder"
    assert required("GET", "/apps/{app}") == "user"
    assert required("PUT", "/apps/{app}/draft") == "builder"
    assert required("GET", "/catalog") == "builder"
    assert required("POST", "/catalog/{kind}/{asset_id}/pull-request") == "builder"
    assert required("POST", "/knowledge-bases/{kb}/search") == "builder"
    assert required("GET", "/models") == "builder"
    assert required("POST", "/models") == "admin"
    assert required("PUT", "/models/defaults") == "admin"
    assert required("GET", "/users") == "admin"
    assert required("GET", "/audit") == "admin"
    assert required("POST", "/llm/chat") == "builder"


async def test_off_everyone_is_the_local_user_and_names_come_from_the_client():
    p = Platform(enabled=False)
    async with p.client() as api:
        status = (await api.get("/auth/status")).json()
        assert status["enabled"] is False and status["setup_needed"] is False
        assert status["user"]["display_name"] == "本地用户"
        assert (await api.get("/auth/me")).json()["roles"] == ["admin"]
        assert (await api.get("/users")).status_code == 200
        resp = await api.post(
            "/runs", json={"app": "hello", "input": {"text": "你好"}, "requester": "李雷"}
        )
        assert resp.status_code == 201
        await p.service.wait_idle()
        run = (await api.get(f"/runs/{resp.json()['run_id']}")).json()
        assert said(run["messages"])[0][2] == "李雷"
        assert (
            await api.post("/auth/login", json={"username": "a", "password": "b"})
        ).status_code == 409
    # Changes are recorded without a user.
    events = await p.store.events(10, None, None, None)
    assert events[0]["action"] == "POST /runs" and events[0]["user"] is None
    assert events[0]["label"] == "发起运行"


async def test_first_admin_then_sign_in_and_out(platform):
    async with platform.client() as api:
        status = (await api.get("/auth/status")).json()
        assert status == {"enabled": True, "setup_needed": True, "user": None}
        assert (await api.get("/runs")).status_code == 401
        assert (await api.get("/runs")).json()["detail"] == "请先登录"

        bad = await api.post(
            "/auth/setup",
            json={"username": "Admin!", "display_name": "管理员", "password": PASSWORD},
        )
        assert bad.status_code == 422 and "登录名" in bad.json()["detail"]
        short = await api.post(
            "/auth/setup", json={"username": "admin", "display_name": "管理员", "password": "short"}
        )
        assert short.status_code == 422 and "密码" in short.json()["detail"]

        resp = await api.post(
            "/auth/setup",
            json={"username": "Admin", "display_name": "王管理", "password": PASSWORD},
        )
        assert resp.status_code == 201
        assert resp.json()["username"] == "admin" and resp.json()["roles"] == ["admin"]
        cookie = resp.headers["set-cookie"]
        assert (
            "aep_session=" in cookie and "HttpOnly" in cookie and "samesite=lax" in cookie.lower()
        )
        assert (await api.get("/auth/me")).json()["display_name"] == "王管理"
        status = (await api.get("/auth/status")).json()
        assert status["setup_needed"] is False and status["user"]["username"] == "admin"

        again = await api.post(
            "/auth/setup", json={"username": "evil", "display_name": "x", "password": PASSWORD}
        )
        assert again.status_code == 409 and again.json()["detail"] == "已经有账号了，请直接登录"

        assert (await api.post("/auth/logout")).status_code == 204
        assert (await api.get("/auth/me")).status_code == 401

    # The cookie holds a random token; only its hash is stored.
    assert all(len(digest) == 64 for digest in platform.store.sessions)
    actions = [e["action"] for e in await platform.store.events(10, None, None, None)]
    assert actions == ["logout", "setup"]


async def test_wrong_password_disabled_accounts_and_lockout(platform):
    user_id = await platform.user("lilei", ["user"])
    async with platform.client() as api:
        wrong = await api.post("/auth/login", json={"username": "lilei", "password": "nope-nope"})
        assert wrong.status_code == 401 and wrong.json()["detail"] == "账号或密码不对"
        nobody = await api.post("/auth/login", json={"username": "nobody", "password": PASSWORD})
        assert nobody.json()["detail"] == "账号或密码不对"
        for _ in range(3):
            await api.post("/auth/login", json={"username": "lilei", "password": "nope-nope"})
        # Five failures in 15 minutes lock the name, even for the right password.
        await api.post("/auth/login", json={"username": "LiLei", "password": "nope-nope"})
        locked = await api.post("/auth/login", json={"username": "lilei", "password": PASSWORD})
        assert locked.status_code == 429
        platform.clock.at += timedelta(minutes=16)
        assert (
            await api.post("/auth/login", json={"username": "lilei", "password": PASSWORD})
        ).status_code == 200

        await platform.auth.update(user_id, disabled=True)
        # Disabling ends the account's sessions and blocks signing in.
        assert (await api.get("/auth/me")).status_code == 401
        off = await api.post("/auth/login", json={"username": "lilei", "password": PASSWORD})
        assert off.status_code == 401
    failed = await platform.store.events(50, None, None, "login_failed")
    assert failed[0]["target"] == {"username": "lilei"} and failed[0]["user"] is None
    assert all(PASSWORD not in str(e) for e in platform.store.audit)


async def test_sessions_expire(platform):
    await platform.user("lilei", ["user"])
    api = await platform.signed_in("lilei")
    async with signed(api):
        platform.clock.at += timedelta(hours=169)
        assert (await api.get("/runs")).status_code == 401


async def test_roles_decide_what_each_account_may_do(platform):
    await platform.user("user1", ["user"])
    await platform.user("builder1", ["builder"])
    await platform.user("admin1", ["admin"])
    async with signed(await platform.signed_in("user1")) as api:
        assert (await api.get("/runs")).status_code == 200
        assert (await api.get("/apps")).status_code == 200
        denied = await api.put("/apps/hello/draft", json={"config": {}})
        assert denied.status_code == 403
        assert denied.json()["detail"] == "没有权限：这项操作需要搭建者"
        assert (await api.get("/models")).status_code == 403
        assert (await api.get("/users")).status_code == 403
    async with signed(await platform.signed_in("builder1")) as api:
        resp = await api.post("/skills", json={})
        assert resp.status_code == 422  # through the check, to the body's own validation
        assert (await api.get("/users")).status_code == 403
        assert (await api.get("/audit")).json()["detail"] == "没有权限：这项操作需要管理员"
    async with signed(await platform.signed_in("admin1")) as api:
        assert (await api.get("/users")).status_code == 200
        assert (await api.get("/audit")).status_code == 200
    denied = (await platform.store.events(50, None, "user1", None))[0]
    assert denied["action"] == "PUT /apps/{app}/draft" and denied["status"] == 403
    assert denied["target"] == {"app": "hello"} and denied["label"] == "保存草稿"


async def test_approvals_need_an_approver_and_record_the_signed_in_name(platform):
    await platform.user("user1", ["user"], "李雷")
    await platform.user("approver1", ["approver"], "韩梅梅")
    await platform.user("builder1", ["builder"], "王搭建")
    async with signed(await platform.signed_in("user1")) as api:
        resp = await api.post(
            "/runs", json={"app": "hello", "input": {"text": "你好"}, "requester": "冒名"}
        )
        run_id = resp.json()["run_id"]
        await platform.service.wait_idle()
        run = (await api.get(f"/runs/{run_id}")).json()
        assert run["created_by"] == "李雷"
        assert said(run["messages"])[0][2] == "李雷"
        denied = await api.post(f"/runs/{run_id}/resume", json={"decision": "approve"})
        assert denied.status_code == 403
        assert denied.json()["detail"] == "没有权限：这一步要审批者确认"
    # Builders don't approve: who builds and who approves can be different people.
    async with signed(await platform.signed_in("builder1")) as api:
        assert (
            await api.post(f"/runs/{run_id}/resume", json={"decision": "approve"})
        ).status_code == 403
    async with signed(await platform.signed_in("approver1")) as api:
        resp = await api.post(
            f"/runs/{run_id}/resume", json={"decision": "approve", "actor": "冒名"}
        )
        assert resp.status_code == 202
        await platform.service.wait_idle()
        run = (await api.get(f"/runs/{run_id}")).json()
        assert run["status"] == "done"
        assert said(run["messages"][-1:]) == [("user", "review", "韩梅梅", "选了「确认」")]
        missing = await api.post("/runs/nope/resume", json={"decision": "approve"})
        assert missing.status_code == 404


async def test_admins_manage_accounts(platform):
    admin_id = await platform.user("admin1", ["admin"], "王管理")
    async with signed(await platform.signed_in("admin1")) as api:
        resp = await api.post(
            "/users",
            json={
                "username": "hanmeimei",
                "display_name": "韩梅梅",
                "roles": ["approver", "user"],
                "password": PASSWORD,
            },
        )
        assert resp.status_code == 201
        user = resp.json()
        assert user["roles"] == ["user", "approver"] and "password" not in str(user)
        dup = await api.post(
            "/users",
            json={
                "username": "hanmeimei",
                "display_name": "x",
                "roles": ["user"],
                "password": PASSWORD,
            },
        )
        assert dup.status_code == 409
        assert dup.json()["detail"] == "已经有登录名为「hanmeimei」的账号了"
        bad_role = await api.post(
            "/users",
            json={"username": "x1", "display_name": "x", "roles": ["owner"], "password": PASSWORD},
        )
        assert bad_role.status_code == 422
        no_role = await api.post(
            "/users",
            json={"username": "x1", "display_name": "x", "roles": [], "password": PASSWORD},
        )
        assert no_role.status_code == 422

        listed = (await api.get("/users")).json()
        assert [u["username"] for u in listed] == ["admin1", "hanmeimei"]

        changed = await api.patch(
            f"/users/{user['user_id']}", json={"roles": ["builder"], "display_name": "韩梅梅2"}
        )
        assert changed.json()["roles"] == ["builder"]
        assert changed.json()["display_name"] == "韩梅梅2"
        assert (await api.patch("/users/u_nobody", json={"disabled": True})).status_code == 404

        # The last admin stays.
        last = await api.patch(f"/users/{admin_id}", json={"roles": ["builder"]})
        assert last.status_code == 409 and last.json()["detail"] == "至少要留一个管理员"
        assert (await api.patch(f"/users/{admin_id}", json={"disabled": True})).status_code == 409
        own = await api.delete(f"/users/{admin_id}")
        assert own.status_code == 409 and own.json()["detail"] == "不能删除自己的账号"

        # A reset password ends the account's sessions.
        other = await platform.signed_in("hanmeimei")
        async with signed(other):
            reset = await api.put(
                f"/users/{user['user_id']}/password", json={"password": "another-pass"}
            )
            assert reset.status_code == 204
            assert (await other.get("/auth/me")).status_code == 401
            again = await other.post(
                "/auth/login", json={"username": "hanmeimei", "password": "another-pass"}
            )
            assert again.status_code == 200

        assert (await api.delete(f"/users/{user['user_id']}")).status_code == 204
        assert (await api.delete(f"/users/{user['user_id']}")).status_code == 404

        audit = (await api.get("/audit", params={"limit": 3})).json()
        assert audit["events"][0]["label"] == "删除账号"
        assert audit["events"][0]["user"]["username"] == "admin1"
        assert audit["next"] == audit["events"][-1]["id"]
        rest = (await api.get("/audit", params={"before": audit["next"]})).json()
        assert all(e["id"] < audit["next"] for e in rest["events"])
        found = (await api.get("/audit", params={"q": "重设"})).json()["events"]
        assert [e["action"] for e in found] == ["PUT /users/{user_id}/password"]


async def test_change_own_password_keeps_this_session_only(platform):
    await platform.user("lilei", ["user"])
    first = await platform.signed_in("lilei")
    second = await platform.signed_in("lilei")
    async with signed(first), signed(second):
        wrong = await first.put(
            "/auth/me/password", json={"old_password": "nope", "password": "new-password"}
        )
        assert wrong.status_code == 422 and wrong.json()["detail"] == "原密码不对"
        ok = await first.put(
            "/auth/me/password", json={"old_password": PASSWORD, "password": "new-password"}
        )
        assert ok.status_code == 204
        assert (await first.get("/auth/me")).status_code == 200
        assert (await second.get("/auth/me")).status_code == 401


async def test_records_skip_reads_and_bodies(platform):
    await platform.user("builder1", ["builder"])
    async with signed(await platform.signed_in("builder1")) as api:
        await api.get("/runs")
        await api.post("/skills", json={"name": "x", "body": "secret-in-body"})
    events = await platform.store.events(10, None, None, None)
    assert [e["action"] for e in events] == ["POST /skills", "login"]
    assert "secret-in-body" not in str(events)


def test_local_user_has_every_role():
    assert all(LOCAL_USER.has(role) for role in ("user", "approver", "builder", "admin"))
