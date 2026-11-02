"""Notifications when a run settles (docs/api/notifications.md).

The run service calls ``Notifier.run_settled`` after a run starts waiting for a person,
finishes or fails; apps and nodes know nothing of it. Each notice goes to the in-app
notification centre, by email to accounts that have an address (when SMTP is set up),
and to every webhook that takes the event. Delivery never fails a run: a failure is
logged, and a webhook keeps its last result.
"""

import asyncio
import logging
import secrets
import smtplib
import ssl
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from typing import TYPE_CHECKING, Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx
from psycopg_pool import AsyncConnectionPool

from aep.config import Settings
from aep.runtime.store import DEFAULT_TENANT

if TYPE_CHECKING:
    from aep.auth import AuthService, User
    from aep.runtime.service import RunSettled

log = logging.getLogger(__name__)

Event = Literal["run.waiting", "run.done", "run.error", "app.budget"]
EVENTS: tuple[Event, ...] = ("run.waiting", "run.done", "run.error", "app.budget")
WebhookKind = Literal["generic", "wecom", "dingtalk", "feishu"]
KINDS: tuple[WebhookKind, ...] = ("generic", "wecom", "dingtalk", "feishu")
KEEP_DAYS = 90
BODY_CHARS = 500
# Group bots that check a keyword (DingTalk, Feishu) are told to look for this one.
KEYWORD = "AEP"


@dataclass
class Notice:
    event: str
    run_id: str
    app: str
    app_title: str
    title: str
    body: str
    link: str  # a path in the workbench, e.g. /runs/…
    at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class Webhook:
    webhook_id: str
    name: str
    kind: str
    url: str
    events: list[str]
    apps: list[str] | None = None
    enabled: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_sent_at: datetime | None = None
    last_status: str | None = None
    last_error: str | None = None

    def takes(self, notice: Notice) -> bool:
        return (
            self.enabled
            and notice.event in self.events
            and (self.apps is None or notice.app in self.apps)
        )

    def out(self) -> dict[str, Any]:
        """What the API shows: everything but the address, which often holds a key."""
        return {
            "webhook_id": self.webhook_id,
            "name": self.name,
            "kind": self.kind,
            "url_hint": url_hint(self.url),
            "events": self.events,
            "apps": self.apps,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "last_sent_at": self.last_sent_at,
            "last_status": self.last_status,
            "last_error": self.last_error,
        }


def url_hint(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname or ''}/…{url[-4:]}"


def _short(text: str, limit: int = BODY_CHARS) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


class NotificationStore(Protocol):
    async def setup(self) -> None: ...
    async def add(self, notice: Notice, user_ids: list[str | None]) -> None: ...
    async def page(
        self, user_id: str | None, limit: int, before: int | None, unread_only: bool
    ) -> tuple[list[dict[str, Any]], int]: ...
    async def mark_read(self, user_id: str | None, ids: list[int] | None, at: datetime) -> int: ...
    async def prune(self, before: datetime) -> None: ...
    async def webhooks(self) -> list[Webhook]: ...
    async def webhook(self, webhook_id: str) -> Webhook | None: ...
    async def save_webhook(self, webhook: Webhook) -> None: ...
    async def delete_webhook(self, webhook_id: str) -> bool: ...


def _row_out(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "event": row["event"],
        "run_id": row["run_id"],
        "app": row["app"],
        "title": row["title"],
        "body": row["body"],
        "link": row["link"],
        "created_at": row["created_at"],
        "read": row["read_at"] is not None,
    }


def _visible(row: dict[str, Any], user_id: str | None) -> bool:
    # user_id None: sign-in is off and everything is everyone's. Otherwise one's own,
    # plus those sent to nobody in particular.
    return user_id is None or row["user_id"] in (user_id, None)


class MemoryNotificationStore:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.hooks: dict[str, Webhook] = {}

    async def setup(self) -> None:
        pass

    async def add(self, notice: Notice, user_ids: list[str | None]) -> None:
        for user_id in user_ids:
            self.rows.append(
                {
                    "id": len(self.rows) + 1,
                    "user_id": user_id,
                    "event": notice.event,
                    "run_id": notice.run_id,
                    "app": notice.app,
                    "title": notice.title,
                    "body": notice.body,
                    "link": notice.link,
                    "created_at": notice.at,
                    "read_at": None,
                }
            )

    async def page(
        self, user_id: str | None, limit: int, before: int | None, unread_only: bool
    ) -> tuple[list[dict[str, Any]], int]:
        mine = [r for r in reversed(self.rows) if _visible(r, user_id)]
        unread = sum(1 for r in mine if r["read_at"] is None)
        rows = [
            r
            for r in mine
            if (before is None or r["id"] < before) and not (unread_only and r["read_at"])
        ]
        return [_row_out(r) for r in rows[:limit]], unread

    async def mark_read(self, user_id: str | None, ids: list[int] | None, at: datetime) -> int:
        for row in self.rows:
            if _visible(row, user_id) and (ids is None or row["id"] in ids):
                row["read_at"] = row["read_at"] or at
        return sum(1 for r in self.rows if _visible(r, user_id) and r["read_at"] is None)

    async def prune(self, before: datetime) -> None:
        self.rows = [r for r in self.rows if r["created_at"] >= before]

    async def webhooks(self) -> list[Webhook]:
        return sorted((replace(w) for w in self.hooks.values()), key=lambda w: w.created_at)

    async def webhook(self, webhook_id: str) -> Webhook | None:
        found = self.hooks.get(webhook_id)
        return replace(found) if found else None

    async def save_webhook(self, webhook: Webhook) -> None:
        self.hooks[webhook.webhook_id] = replace(webhook)

    async def delete_webhook(self, webhook_id: str) -> bool:
        return self.hooks.pop(webhook_id, None) is not None


SCHEMA = [
    """
CREATE TABLE IF NOT EXISTS notifications (
    id          bigserial PRIMARY KEY,
    tenant_id   text NOT NULL DEFAULT 'default',
    user_id     text,
    event       text NOT NULL,
    run_id      text NOT NULL,
    app         text NOT NULL,
    title       text NOT NULL,
    body        text NOT NULL,
    link        text NOT NULL,
    created_at  timestamptz NOT NULL DEFAULT now(),
    read_at     timestamptz
)""",
    "CREATE INDEX IF NOT EXISTS notifications_user ON notifications (tenant_id, user_id, id DESC)",
    """
CREATE TABLE IF NOT EXISTS webhooks (
    webhook_id    text PRIMARY KEY,
    tenant_id     text NOT NULL DEFAULT 'default',
    name          text NOT NULL,
    kind          text NOT NULL,
    url           text NOT NULL,
    events        text[] NOT NULL,
    apps          text[],
    enabled       boolean NOT NULL DEFAULT true,
    created_at    timestamptz NOT NULL DEFAULT now(),
    last_sent_at  timestamptz,
    last_status   text,
    last_error    text
)""",
]
_HOOK_COLUMNS = (
    "webhook_id, name, kind, url, events, apps, enabled, created_at,"
    " last_sent_at, last_status, last_error"
)


def _hook(row: dict[str, Any]) -> Webhook:
    return Webhook(
        row["webhook_id"],
        row["name"],
        row["kind"],
        row["url"],
        list(row["events"]),
        list(row["apps"]) if row["apps"] is not None else None,
        row["enabled"],
        row["created_at"],
        row["last_sent_at"],
        row["last_status"],
        row["last_error"],
    )


class PostgresNotificationStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            for statement in SCHEMA:
                await conn.execute(statement)

    async def add(self, notice: Notice, user_ids: list[str | None]) -> None:
        async with self._pool.connection() as conn:
            for user_id in user_ids:
                await conn.execute(
                    "INSERT INTO notifications (tenant_id, user_id, event, run_id, app, title,"
                    " body, link, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        DEFAULT_TENANT,
                        user_id,
                        notice.event,
                        notice.run_id,
                        notice.app,
                        notice.title,
                        notice.body,
                        notice.link,
                        notice.at,
                    ),
                )

    @staticmethod
    def _whose(user_id: str | None) -> tuple[str, tuple[Any, ...]]:
        if user_id is None:
            return "tenant_id = %s", (DEFAULT_TENANT,)
        return "tenant_id = %s AND (user_id = %s OR user_id IS NULL)", (DEFAULT_TENANT, user_id)

    async def page(
        self, user_id: str | None, limit: int, before: int | None, unread_only: bool
    ) -> tuple[list[dict[str, Any]], int]:
        where, params = self._whose(user_id)
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT count(*) AS n FROM notifications WHERE {where} AND read_at IS NULL",
                params,
            )
            row = await cur.fetchone()
            unread = int(row["n"]) if row else 0
            query = f"SELECT * FROM notifications WHERE {where}"
            args: list[Any] = list(params)
            if before is not None:
                query += " AND id < %s"
                args.append(before)
            if unread_only:
                query += " AND read_at IS NULL"
            cur = await conn.execute(query + " ORDER BY id DESC LIMIT %s", (*args, limit))
            return [_row_out(r) for r in await cur.fetchall()], unread

    async def mark_read(self, user_id: str | None, ids: list[int] | None, at: datetime) -> int:
        where, params = self._whose(user_id)
        async with self._pool.connection() as conn:
            query = f"UPDATE notifications SET read_at = %s WHERE {where} AND read_at IS NULL"
            args: list[Any] = [at, *params]
            if ids is not None:
                query += " AND id = ANY(%s)"
                args.append(ids)
            await conn.execute(query, args)
            cur = await conn.execute(
                f"SELECT count(*) AS n FROM notifications WHERE {where} AND read_at IS NULL",
                params,
            )
            row = await cur.fetchone()
            return int(row["n"]) if row else 0

    async def prune(self, before: datetime) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM notifications WHERE tenant_id = %s AND created_at < %s",
                (DEFAULT_TENANT, before),
            )

    async def webhooks(self) -> list[Webhook]:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {_HOOK_COLUMNS} FROM webhooks WHERE tenant_id = %s ORDER BY created_at",
                (DEFAULT_TENANT,),
            )
            return [_hook(r) for r in await cur.fetchall()]

    async def webhook(self, webhook_id: str) -> Webhook | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                f"SELECT {_HOOK_COLUMNS} FROM webhooks WHERE tenant_id = %s AND webhook_id = %s",
                (DEFAULT_TENANT, webhook_id),
            )
            row = await cur.fetchone()
            return _hook(row) if row else None

    async def save_webhook(self, webhook: Webhook) -> None:
        w = webhook
        async with self._pool.connection() as conn:
            await conn.execute(
                f"INSERT INTO webhooks (tenant_id, {_HOOK_COLUMNS})"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (webhook_id) DO UPDATE SET name = EXCLUDED.name,"
                " kind = EXCLUDED.kind, url = EXCLUDED.url, events = EXCLUDED.events,"
                " apps = EXCLUDED.apps, enabled = EXCLUDED.enabled,"
                " last_sent_at = EXCLUDED.last_sent_at, last_status = EXCLUDED.last_status,"
                " last_error = EXCLUDED.last_error",
                (
                    DEFAULT_TENANT,
                    w.webhook_id,
                    w.name,
                    w.kind,
                    w.url,
                    w.events,
                    w.apps,
                    w.enabled,
                    w.created_at,
                    w.last_sent_at,
                    w.last_status,
                    w.last_error,
                ),
            )

    async def delete_webhook(self, webhook_id: str) -> bool:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "DELETE FROM webhooks WHERE tenant_id = %s AND webhook_id = %s",
                (DEFAULT_TENANT, webhook_id),
            )
            return cur.rowcount > 0


# Sending


class Mailer:
    """Plain-text mail over SMTP (``AEP_SMTP_*``); not configured without a host."""

    def __init__(self, settings: Settings) -> None:
        self._s = settings

    @property
    def configured(self) -> bool:
        return bool(self._s.smtp_host)

    @property
    def sender(self) -> str | None:
        return self._s.smtp_from or self._s.smtp_user

    def _send(self, to: str, subject: str, text: str) -> None:
        s = self._s
        message = EmailMessage()
        message["From"] = self.sender or "aep@localhost"
        message["To"] = to
        message["Subject"] = subject
        message.set_content(text)
        context = ssl.create_default_context()
        host = s.smtp_host or ""
        smtp: smtplib.SMTP = (
            smtplib.SMTP_SSL(host, s.smtp_port, timeout=20, context=context)
            if s.smtp_security == "ssl"
            else smtplib.SMTP(host, s.smtp_port, timeout=20)
        )
        with smtp:
            if s.smtp_security == "starttls":
                smtp.starttls(context=context)
            if s.smtp_user:
                smtp.login(s.smtp_user, s.smtp_password or "")
            smtp.send_message(message)

    async def send(self, to: str, subject: str, text: str) -> None:
        await asyncio.to_thread(self._send, to, subject, text)


# Posts a JSON body; answers the status code and the parsed body (None if not JSON).
Post = Callable[[str, dict[str, Any]], Awaitable[tuple[int, Any]]]


async def http_post(url: str, body: dict[str, Any]) -> tuple[int, Any]:
    async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
        response = await client.post(url, json=body)
    try:
        parsed = response.json()
    except ValueError:
        parsed = None
    return response.status_code, parsed


def webhook_body(kind: str, notice: Notice, link: str) -> dict[str, Any]:
    page = "打开运营看板" if notice.event == "app.budget" else "打开运行页"
    if kind == "wecom":
        text = f"**{notice.title}**\n{notice.body}\n[{page}]({link})\n> {KEYWORD}"
        return {"msgtype": "markdown", "markdown": {"content": text}}
    if kind == "dingtalk":
        text = f"#### {notice.title}\n\n{notice.body}\n\n[{page}]({link})\n\n> {KEYWORD}"
        return {"msgtype": "markdown", "markdown": {"title": notice.title, "text": text}}
    if kind == "feishu":
        text = f"[{KEYWORD}] {notice.title}\n{notice.body}\n{link}".replace("\n\n", "\n")
        return {"msg_type": "text", "content": {"text": text}}
    return {
        "event": notice.event,
        "run_id": notice.run_id,
        "app": notice.app,
        "app_title": notice.app_title,
        "title": notice.title,
        "body": notice.body,
        "link": link,
        "at": notice.at.isoformat(),
    }


def _bot_error(parsed: Any) -> str | None:
    """Group bots answer 200 with their own error code (WeCom, DingTalk: errcode;
    Feishu: code); None when the answer says it went through."""
    if not isinstance(parsed, dict):
        return None
    for code, message in (("errcode", "errmsg"), ("code", "msg"), ("StatusCode", "StatusMessage")):
        value = parsed.get(code)
        if isinstance(value, int) and value != 0:
            return f"机器人返回错误 {value}：{parsed.get(message) or '没有说明'}"
    return None


@dataclass
class Recipients:
    user_ids: list[str | None]
    emails: list[str]


class Notifier:
    def __init__(
        self,
        store: NotificationStore,
        settings: Settings,
        auth: "AuthService | None" = None,
        mailer: Any = None,
        post: Post = http_post,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.store = store
        self.settings = settings
        self.auth = auth
        self.mailer = mailer if mailer is not None else Mailer(settings)
        self.post = post
        self.now = now

    async def setup(self) -> None:
        await self.store.setup()
        await self.store.prune(self.now() - timedelta(days=KEEP_DAYS))

    def link(self, path: str) -> str:
        return self.settings.web_url.rstrip("/") + path

    # What happened

    async def run_settled(self, settled: "RunSettled") -> None:
        record = settled.record
        event = {"waiting": "run.waiting", "done": "run.done", "error": "run.error"}.get(
            record.status
        )
        # A draft's trial and eval runs belong to no version; they would flood people.
        if event is None or (record.app_version is None and record.config is not None):
            return
        pending = settled.pending or {}
        form = event == "run.waiting" and pending.get("kind") == "form"
        app = settled.app_title
        about = settled.title or ""
        if event == "run.waiting":
            title = f"「{app}」等你{'填写' if form else '确认'}"
            body = "\n".join(p for p in (about, str(pending.get("prompt") or "")) if p)
        elif event == "run.done":
            title = f"「{app}」运行完成"
            body = about
        else:
            title = f"「{app}」运行失败"
            body = "\n".join(p for p in (about, f"原因：{record.error or '没有说明'}") if p)
        notice = Notice(
            event,
            record.run_id,
            record.app,
            app,
            _short(title, 100),
            _short(body),
            f"/runs/{record.run_id}",
            self.now(),
        )
        to = await self._recipients(event, form, record.created_by)
        await self.deliver(notice, to)

    async def budget(
        self,
        app: str,
        app_title: str,
        run_id: str,
        *,
        over: bool,
        spent: float,
        monthly: float,
        block: bool,
    ) -> None:
        """An app's monthly model budget passed its warning line or ran out
        (docs/api/ops.md); to admins, or everyone's inbox while sign-in is off."""
        if over:
            title = f"「{app_title}」本月模型预算已用完"
            then = "之后这个应用的模型调用会被拦下" if block else "预算设成只提醒，调用不受影响"
        else:
            title = f"「{app_title}」本月模型预算快用完了"
            then = "用完后" + ("会拦下这个应用的模型调用" if block else "只提醒，不拦截")
        body = f"本月已用 ${spent:.2f}，预算 ${monthly:g}；{then}。"
        notice = Notice("app.budget", run_id, app, app_title, title, body, "/ops", self.now())
        accounts: list[User] = []
        if self.auth is not None and self.auth.enabled:
            accounts = [
                u for u in await self.auth.store.list_users() if not u.disabled and u.has("admin")
            ]
        to = Recipients(
            [u.user_id for u in accounts] or [None], [u.email for u in accounts if u.email]
        )
        await self.deliver(notice, to)

    async def _recipients(self, event: str, form: bool, requester: str | None) -> Recipients:
        accounts: list[User] = []
        if self.auth is not None and self.auth.enabled:
            accounts = [u for u in await self.auth.store.list_users() if not u.disabled]
        if event == "run.waiting" and not form:
            chosen = [u for u in accounts if u.has("approver")]
        else:
            chosen = [u for u in accounts if requester and u.display_name == requester]
        return Recipients(
            [u.user_id for u in chosen] or [None], [u.email for u in chosen if u.email]
        )

    async def deliver(self, notice: Notice, to: Recipients) -> None:
        try:
            await self.store.add(notice, to.user_ids)
        except Exception:
            log.exception("could not store the notice for run %s", notice.run_id)
        link = self.link(notice.link)
        if to.emails and self.mailer.configured:
            text = f"{notice.body}\n\n打开：{link}\n" if notice.body else f"打开：{link}\n"
            for address in to.emails:
                try:
                    await self.mailer.send(address, f"[{KEYWORD}] {notice.title}", text)
                except Exception:
                    log.exception("could not email the notice for run %s", notice.run_id)
        try:
            hooks = [w for w in await self.store.webhooks() if w.takes(notice)]
        except Exception:
            log.exception("could not read webhooks")
            hooks = []
        for hook in hooks:
            await self._send(hook, notice, link)

    async def _send(self, hook: Webhook, notice: Notice, link: str) -> Webhook:
        error: str | None = None
        try:
            status, parsed = await self.post(hook.url, webhook_body(hook.kind, notice, link))
            if not 200 <= status < 300:
                error = f"对方返回 HTTP {status}"
            else:
                error = _bot_error(parsed)
        except httpx.TimeoutException:
            error = "10 秒内没有响应"
        except Exception as exc:  # noqa: BLE001 - any failure is the webhook's result
            error = f"发送失败：{type(exc).__name__}"
        hook.last_sent_at = self.now()
        hook.last_status = "error" if error else "ok"
        hook.last_error = error
        try:
            # Only the result changes: the webhook may have been edited meanwhile.
            current = await self.store.webhook(hook.webhook_id)
            if current is not None:
                current.last_sent_at, current.last_status, current.last_error = (
                    hook.last_sent_at,
                    hook.last_status,
                    hook.last_error,
                )
                await self.store.save_webhook(current)
        except Exception:
            log.exception("could not record the result of webhook %s", hook.webhook_id)
        return hook

    # Webhooks (admins)

    async def create_webhook(
        self,
        name: str,
        kind: str,
        url: str,
        events: list[str],
        apps: list[str] | None,
        enabled: bool,
    ) -> Webhook:
        hook = Webhook(
            "wh_" + secrets.token_hex(6),
            name,
            kind,
            url,
            [e for e in EVENTS if e in events],
            apps,
            enabled,
            self.now(),
        )
        await self.store.save_webhook(hook)
        return hook

    async def test_webhook(self, hook: Webhook) -> Webhook:
        notice = Notice(
            "run.done",
            "test",
            "test",
            "测试",
            "这是一条测试消息",
            "通知渠道配置成功，以后运行待确认、完成或失败时会发到这里。",
            "/notifications",
            self.now(),
        )
        return await self._send(hook, notice, self.link(notice.link))
