"""Notifications when runs settle (docs/api/notifications.md), on in-memory storage with
a fake mail server and fake webhooks. Postgres-backed tests: test_notify_postgres.py."""

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from aep.notify import (
    MemoryNotificationStore,
    Notice,
    Notifier,
    Recipients,
    url_hint,
    webhook_body,
)
from aep.runtime.service import RunSettled
from aep.runtime.store import RunRecord
from tests.conftest import SETTINGS
from tests.test_auth import PASSWORD, Platform, signed


class FakeMailer:
    configured = True
    sender = "aep@example.com"

    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    async def send(self, to: str, subject: str, text: str) -> None:
        self.sent.append((to, subject, text))


class FakeHooks:
    """Answers each post with ``answer`` (status, body) or raises it."""

    def __init__(self, answer: Any = (200, {"errcode": 0})) -> None:
        self.answer = answer
        self.posts: list[tuple[str, dict[str, Any]]] = []

    async def __call__(self, url: str, body: dict[str, Any]) -> tuple[int, Any]:
        self.posts.append((url, body))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def record(status: str = "done", **kwargs: Any) -> RunRecord:
    values: dict[str, Any] = {
        "run_id": "r1",
        "app": "retail-etl",
        "status": status,
        "input": {"request": "整理客户表"},
        "app_version": 1,
        "created_by": "李雷",
    }
    return RunRecord(**(values | kwargs))


def settled(status: str = "done", pending: dict[str, Any] | None = None, **kwargs: Any):
    return RunSettled(record(status, **kwargs), pending, "零售 ETL", "整理客户表")


async def accounts(platform: Platform) -> dict[str, str]:
    ids = {}
    for username, roles, name in (
        ("lilei", ["user"], "李雷"),
        ("hanmeimei", ["approver"], "韩梅梅"),
        ("boss", ["admin"], "王管理"),
        ("builder", ["builder"], "王搭建"),
        ("gone", ["approver"], "离职的"),
    ):
        ids[username] = await platform.user(username, roles, name)
    await platform.auth.update(ids["lilei"], email="lilei@example.com")
    await platform.auth.update(ids["hanmeimei"], email="han@example.com")
    await platform.auth.update(ids["gone"], disabled=True, email="gone@example.com")
    return ids


def with_notifier(platform: Platform, hooks: FakeHooks | None = None) -> Notifier:
    notifier = Notifier(
        MemoryNotificationStore(),
        SETTINGS.model_copy(update={"web_url": "https://aep.example.com/"}),
        platform.auth,
        mailer=FakeMailer(),
        post=hooks or FakeHooks(),
    )
    platform.app.state.notifier = notifier
    platform.service.on_settled = notifier.run_settled
    return notifier


async def rows(notifier: Notifier) -> list[dict[str, Any]]:
    assert isinstance(notifier.store, MemoryNotificationStore)
    return notifier.store.rows


async def test_approvals_go_to_approvers_forms_and_results_to_the_requester():
    platform = Platform()
    ids = await accounts(platform)
    notifier = with_notifier(platform)
    mailer = notifier.mailer
    assert isinstance(mailer, FakeMailer)

    await notifier.run_settled(settled("waiting", {"kind": "approval", "prompt": "请确认方案"}))
    waiting = await rows(notifier)
    # Approvers and admins, not the disabled approver.
    assert {r["user_id"] for r in waiting} == {ids["hanmeimei"], ids["boss"]}
    assert waiting[0]["title"] == "「零售 ETL」等你确认"
    assert waiting[0]["body"] == "整理客户表\n请确认方案"
    assert waiting[0]["link"] == "/runs/r1"
    assert mailer.sent == [
        (
            "han@example.com",
            "[AEP] 「零售 ETL」等你确认",
            "整理客户表\n请确认方案\n\n打开：https://aep.example.com/runs/r1\n",
        )
    ]

    notifier.store = MemoryNotificationStore()
    mailer.sent.clear()
    await notifier.run_settled(settled("waiting", {"kind": "form", "prompt": "补充信息"}))
    await notifier.run_settled(settled("done"))
    await notifier.run_settled(settled("error", error="连接超时"))
    found = await rows(notifier)
    assert [(r["user_id"], r["event"], r["title"]) for r in found] == [
        (ids["lilei"], "run.waiting", "「零售 ETL」等你填写"),
        (ids["lilei"], "run.done", "「零售 ETL」运行完成"),
        (ids["lilei"], "run.error", "「零售 ETL」运行失败"),
    ]
    assert found[2]["body"] == "整理客户表\n原因：连接超时"
    assert [to for to, _, _ in mailer.sent] == ["lilei@example.com"] * 3


async def test_drafts_cancelled_runs_and_unknown_requesters():
    platform = Platform()
    await accounts(platform)
    notifier = with_notifier(platform)
    # A draft's trial or eval run belongs to no version; a cancelled run is someone's own doing.
    await notifier.run_settled(settled("done", app_version=None, config={"nodes": []}))
    await notifier.run_settled(settled("cancelled"))
    assert await rows(notifier) == []
    # A name nobody signs in with (typed in while sign-in was off): everyone's, no email.
    await notifier.run_settled(settled("done", created_by="路人"))
    assert [r["user_id"] for r in await rows(notifier)] == [None]
    assert notifier.mailer.sent == []


async def test_sign_in_off_sends_everyone_the_notice_and_no_email():
    platform = Platform(enabled=False)
    notifier = with_notifier(platform)
    await notifier.run_settled(settled("waiting", {"kind": "approval", "prompt": "确认"}))
    assert [r["user_id"] for r in await rows(notifier)] == [None]
    assert notifier.mailer.sent == []


def notice(event: str = "run.done") -> Notice:
    title = "「零售 ETL」运行完成"
    return Notice(event, "r1", "retail-etl", "零售 ETL", title, "整理客户表", "/runs/r1")


def test_webhook_formats():
    link = "https://aep.example.com/runs/r1"
    assert webhook_body("generic", notice(), link) | {"at": ""} == {
        "event": "run.done",
        "run_id": "r1",
        "app": "retail-etl",
        "app_title": "零售 ETL",
        "title": "「零售 ETL」运行完成",
        "body": "整理客户表",
        "link": link,
        "at": "",
    }
    wecom = webhook_body("wecom", notice(), link)
    assert wecom["msgtype"] == "markdown" and link in wecom["markdown"]["content"]
    ding = webhook_body("dingtalk", notice(), link)
    assert ding["markdown"]["title"] == "「零售 ETL」运行完成"
    assert "AEP" in ding["markdown"]["text"]
    assert webhook_body("feishu", notice(), link) == {
        "msg_type": "text",
        "content": {"text": f"[AEP] 「零售 ETL」运行完成\n整理客户表\n{link}"},
    }
    assert url_hint("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=abcd1234") == (
        "https://qyapi.weixin.qq.com/…1234"
    )


async def test_webhooks_take_their_events_and_apps_and_keep_the_last_result():
    platform = Platform()
    hooks = FakeHooks()
    notifier = with_notifier(platform, hooks)
    every = await notifier.create_webhook(
        "全部", "generic", "https://hooks.example.com/a", ["run.done", "run.error"], None, True
    )
    other = await notifier.create_webhook(
        "别的应用", "wecom", "https://hooks.example.com/b", ["run.done"], ["hello"], True
    )
    off = await notifier.create_webhook(
        "停用的", "feishu", "https://hooks.example.com/c", ["run.done"], None, False
    )
    await notifier.deliver(notice(), Recipients([None], []))
    await notifier.deliver(notice("run.waiting"), Recipients([None], []))
    assert [url for url, _ in hooks.posts] == ["https://hooks.example.com/a"]
    saved = await notifier.store.webhook(every.webhook_id)
    assert saved and saved.last_status == "ok" and saved.last_error is None
    for hook in (other, off):
        assert (await notifier.store.webhook(hook.webhook_id)).last_status is None

    # Bots answer 200 with their own error code; other failures say what went wrong.
    for answer, error in (
        ((200, {"errcode": 93000, "errmsg": "invalid webhook url"}), "机器人返回错误 93000"),
        ((200, {"code": 19021, "msg": "sign match fail"}), "机器人返回错误 19021"),
        ((500, None), "对方返回 HTTP 500"),
        (httpx.ConnectTimeout("slow"), "10 秒内没有响应"),
        (httpx.ConnectError("refused"), "发送失败：ConnectError"),
    ):
        hooks.answer = answer
        result = await notifier.test_webhook(every)
        assert result.last_status == "error" and result.last_error.startswith(error), answer
    # The test message is a notice like any other, in the hook's own format.
    assert hooks.posts[-1][1]["title"] == "这是一条测试消息"


async def test_in_app_notifications_through_the_api():
    platform = Platform()
    ids = await accounts(platform)
    notifier = with_notifier(platform)
    async with signed(await platform.signed_in("lilei")) as api:
        run_id = (await api.post("/runs", json={"app": "hello", "input": {"text": "你好"}})).json()[
            "run_id"
        ]
        await platform.service.wait_idle()
        page = (await api.get("/notifications")).json()
        assert page == {"items": [], "unread": 0, "next": None}
    async with signed(await platform.signed_in("hanmeimei")) as api:
        page = (await api.get("/notifications")).json()
        assert page["unread"] == 1
        [item] = page["items"]
        assert item["event"] == "run.waiting" and item["link"] == f"/runs/{run_id}"
        assert item["read"] is False
        assert (await api.post(f"/runs/{run_id}/resume", json={"decision": "approve"})).is_success
        await platform.service.wait_idle()
        assert (await api.post("/notifications/read", json={})).json() == {"unread": 0}
        assert (await api.get("/notifications")).json()["items"][0]["read"] is True
    async with signed(await platform.signed_in("lilei")) as api:
        page = (await api.get("/notifications?unread=true")).json()
        assert [i["title"] for i in page["items"]] == ["「问答示例」运行完成"]
        # Someone else's notice is not one's to mark.
        others = [r["id"] for r in await rows(notifier) if r["user_id"] == ids["hanmeimei"]]
        mine = page["items"][0]["id"]
        assert (await api.post("/notifications/read", json={"ids": others})).json() == {"unread": 1}
        assert (await api.post("/notifications/read", json={"ids": [mine]})).json() == {"unread": 0}
    # Marking notices read is not a change worth recording.
    assert not [e for e in platform.store.audit if "notifications" in e["action"]]


async def test_notification_pages():
    platform = Platform(enabled=False)
    notifier = with_notifier(platform)
    for n in range(5):
        await notifier.deliver(
            Notice("run.done", f"r{n}", "hello", "Hello", f"第 {n} 条", "", f"/runs/r{n}"),
            Recipients([None], []),
        )
    async with platform.client() as api:
        first = (await api.get("/notifications?limit=2")).json()
        assert [i["title"] for i in first["items"]] == ["第 4 条", "第 3 条"]
        assert first["unread"] == 5 and first["next"] == first["items"][-1]["id"]
        rest = (await api.get(f"/notifications?limit=10&before={first['next']}")).json()
        assert [i["title"] for i in rest["items"]] == ["第 2 条", "第 1 条", "第 0 条"]
        assert rest["next"] is None
        assert (await api.get("/notifications?limit=0")).status_code == 422


async def test_old_notices_are_pruned():
    platform = Platform()
    notifier = with_notifier(platform)
    old = notice()
    old.at = datetime.now(UTC) - timedelta(days=91)
    await notifier.deliver(old, Recipients([None], []))
    await notifier.deliver(notice(), Recipients([None], []))
    await notifier.setup()
    assert len(await rows(notifier)) == 1


async def test_admins_manage_webhooks():
    platform = Platform()
    await accounts(platform)
    hooks = FakeHooks()
    with_notifier(platform, hooks)
    secret = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=s3cr3t-key-9f2e"
    async with signed(await platform.signed_in("hanmeimei")) as api:
        assert (await api.get("/notification-channels")).status_code == 403
        assert (await api.post("/webhooks", json={})).status_code == 403
    async with signed(await platform.signed_in("boss")) as api:
        channels = (await api.get("/notification-channels")).json()
        assert channels == {
            "email": {"configured": True, "from": "aep@example.com"},
            "web_url": "https://aep.example.com/",
            "webhooks": [],
        }
        bad = await api.post(
            "/webhooks",
            json={"name": "群", "kind": "wecom", "url": "ftp://x", "events": ["run.done"]},
        )
        assert bad.status_code == 422 and bad.json()["detail"].startswith("「地址」")
        created = await api.post(
            "/webhooks",
            json={
                "name": "数据组群",
                "kind": "wecom",
                "url": secret,
                "events": ["run.error", "run.waiting"],
            },
        )
        assert created.status_code == 201, created.text
        hook = created.json()
        assert hook["url_hint"] == "https://qyapi.weixin.qq.com/…9f2e"
        assert hook["events"] == ["run.waiting", "run.error"] and hook["apps"] is None
        assert "s3cr3t" not in created.text
        hook_id = hook["webhook_id"]

        changed = await api.patch(
            f"/webhooks/{hook_id}", json={"apps": ["retail-etl"], "enabled": False}
        )
        assert changed.json()["apps"] == ["retail-etl"] and changed.json()["enabled"] is False
        assert changed.json()["url_hint"] == hook["url_hint"]
        cleared = await api.patch(f"/webhooks/{hook_id}", json={"apps": None})
        assert cleared.json()["apps"] is None
        assert (await api.patch(f"/webhooks/{hook_id}", json={"name": None})).status_code == 422

        tested = (await api.post(f"/webhooks/{hook_id}/test")).json()
        assert tested["last_status"] == "ok" and hooks.posts[-1][0] == secret
        listed = (await api.get("/notification-channels")).json()["webhooks"]
        assert [h["webhook_id"] for h in listed] == [hook_id]
        assert "s3cr3t" not in str(listed)

        assert (await api.delete(f"/webhooks/{hook_id}")).status_code == 204
        missing = await api.delete(f"/webhooks/{hook_id}")
        assert missing.status_code == 404
        assert missing.json()["detail"] == "没有找到这个 Webhook"
    labels = [e["label"] for e in platform.store.audit if "webhooks" in e["action"]]
    assert "新建 Webhook" in labels and "删除 Webhook" in labels
    # The address never reaches the record of changes.
    assert "s3cr3t" not in str(platform.store.audit)


async def test_accounts_have_an_email():
    platform = Platform()
    await platform.user("boss", ["admin"], "王管理")
    async with signed(await platform.signed_in("boss")) as api:
        created = await api.post(
            "/users",
            json={
                "username": "lilei",
                "display_name": "李雷",
                "roles": ["user"],
                "password": PASSWORD,
                "email": "lilei@example.com",
            },
        )
        assert created.status_code == 201 and created.json()["email"] == "lilei@example.com"
        user_id = created.json()["user_id"]
        bad = await api.patch(f"/users/{user_id}", json={"email": "not-an-address"})
        assert bad.status_code == 422
        assert bad.json()["detail"] == "「邮箱」的格式、长度或范围不正确"
        cleared = await api.patch(f"/users/{user_id}", json={"email": ""})
        assert cleared.json()["email"] is None


@pytest.mark.parametrize("security", ["starttls", "ssl", "none"])
def test_mailer_speaks_smtp(monkeypatch, security):
    from aep import notify

    seen: list[tuple[str, Any]] = []

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: int, **kwargs: Any) -> None:
            seen.append(("connect", (host, port, "context" in kwargs)))

        def __enter__(self) -> "FakeSMTP":
            return self

        def __exit__(self, *exc: Any) -> None:
            seen.append(("quit", None))

        def starttls(self, context: Any) -> None:
            seen.append(("starttls", None))

        def login(self, user: str, password: str) -> None:
            seen.append(("login", (user, password)))

        def send_message(self, message: Any) -> None:
            seen.append(("send", (message["From"], message["To"], message["Subject"])))

    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(notify.smtplib, "SMTP_SSL", FakeSMTP)
    settings = SETTINGS.model_copy(
        update={
            "smtp_host": "smtp.example.com",
            "smtp_port": 465 if security == "ssl" else 587,
            "smtp_user": "aep@example.com",
            "smtp_password": "pw",
            "smtp_security": security,
        }
    )
    mailer = notify.Mailer(settings)
    assert mailer.configured and mailer.sender == "aep@example.com"
    mailer._send("lilei@example.com", "主题", "正文")
    steps = [step for step, _ in seen]
    assert steps == (["connect", "starttls"] if security == "starttls" else ["connect"]) + [
        "login",
        "send",
        "quit",
    ]
    assert seen[0][1][2] is (security == "ssl")
    assert ("send", ("aep@example.com", "lilei@example.com", "主题")) in seen
    assert not notify.Mailer(SETTINGS).configured
