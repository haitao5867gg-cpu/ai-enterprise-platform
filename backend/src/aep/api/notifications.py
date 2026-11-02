"""Notifications and their channels. Contract: docs/api/notifications.md."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field, StringConstraints

from aep.api.validation import NoNulls, chinese_errors
from aep.auth import current_user, enabled
from aep.notify import Notifier, Webhook

router = APIRouter(
    tags=["notifications"],
    route_class=chinese_errors(
        {
            "ids": "通知",
            "name": "名称",
            "kind": "格式",
            "url": "地址",
            "events": "事件",
            "apps": "应用范围",
            "enabled": "启用",
        }
    ),
)

EventName = Literal["run.waiting", "run.done", "run.error"]
KindName = Literal["generic", "wecom", "dingtalk", "feishu"]
Name = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=50, pattern=r"^[^\x00]*$"),
]
Url = Annotated[
    str,
    StringConstraints(strip_whitespace=True, max_length=1000, pattern=r"^https?://[^\s]+$"),
]
AppName = Annotated[str, StringConstraints(min_length=1, max_length=128)]


class NotificationOut(BaseModel):
    id: int
    event: str
    run_id: str
    app: str
    title: str
    body: str
    link: str
    created_at: datetime
    read: bool


class NotificationPage(BaseModel):
    items: list[NotificationOut]
    unread: int
    next: int | None


class MarkRead(NoNulls):
    ids: list[int] | None = Field(None, max_length=500)


class Unread(BaseModel):
    unread: int


class WebhookOut(BaseModel):
    webhook_id: str
    name: str
    kind: str
    url_hint: str
    events: list[str]
    apps: list[str] | None
    enabled: bool
    created_at: datetime
    last_sent_at: datetime | None
    last_status: str | None
    last_error: str | None


class EmailStatus(BaseModel):
    configured: bool
    sender: str | None = Field(None, serialization_alias="from")


class ChannelStatus(BaseModel):
    email: EmailStatus
    web_url: str
    webhooks: list[WebhookOut]


class CreateWebhook(NoNulls):
    name: Name
    kind: KindName
    url: Url
    events: list[EventName] = Field(min_length=1, max_length=3)
    apps: list[AppName] | None = Field(None, min_length=1, max_length=100)
    enabled: bool = True


class UpdateWebhook(BaseModel):
    # ``apps`` may be null (all apps), so this model takes nulls; the others refuse them.
    name: Name | None = None
    kind: KindName | None = None
    url: Url | None = None
    events: list[EventName] | None = Field(None, min_length=1, max_length=3)
    apps: list[AppName] | None = Field(None, min_length=1, max_length=100)
    enabled: bool | None = None


def _notifier(request: Request) -> Notifier:
    notifier: Notifier | None = getattr(request.app.state, "notifier", None)
    if notifier is None:
        raise HTTPException(503, "通知服务还没有准备好")
    return notifier


def _whose(request: Request) -> str | None:
    # Sign-in off: no accounts, every notice is everyone's.
    return current_user(request).user_id if enabled(request) else None


def _hook_out(hook: Webhook) -> WebhookOut:
    return WebhookOut(**hook.out())


async def _hook(notifier: Notifier, webhook_id: str) -> Webhook:
    hook = await notifier.store.webhook(webhook_id)
    if hook is None:
        raise HTTPException(404, "没有找到这个 Webhook")
    return hook


@router.get("/notifications")
async def list_notifications(
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    before: Annotated[int | None, Query(ge=1)] = None,
    unread: bool = False,
) -> NotificationPage:
    rows, count = await _notifier(request).store.page(_whose(request), limit + 1, before, unread)
    more = len(rows) > limit
    rows = rows[:limit]
    return NotificationPage(
        items=[NotificationOut(**r) for r in rows],
        unread=count,
        next=rows[-1]["id"] if more and rows else None,
    )


@router.post("/notifications/read")
async def mark_read(request: Request, body: MarkRead | None = None) -> Unread:
    notifier = _notifier(request)
    ids = body.ids if body is not None else None
    return Unread(unread=await notifier.store.mark_read(_whose(request), ids, notifier.now()))


@router.get("/notification-channels")
async def channels(request: Request) -> ChannelStatus:
    notifier = _notifier(request)
    mailer = notifier.mailer
    return ChannelStatus(
        email=EmailStatus(
            configured=bool(mailer.configured),
            sender=mailer.sender if mailer.configured else None,
        ),
        web_url=notifier.settings.web_url,
        webhooks=[_hook_out(h) for h in await notifier.store.webhooks()],
    )


@router.post("/webhooks", status_code=status.HTTP_201_CREATED)
async def create_webhook(body: CreateWebhook, request: Request) -> WebhookOut:
    hook = await _notifier(request).create_webhook(
        body.name, body.kind, body.url, list(body.events), body.apps, body.enabled
    )
    return _hook_out(hook)


@router.patch("/webhooks/{webhook_id}")
async def update_webhook(webhook_id: str, body: UpdateWebhook, request: Request) -> WebhookOut:
    notifier = _notifier(request)
    hook = await _hook(notifier, webhook_id)
    given = body.model_fields_set
    labels = {"name": "名称", "kind": "格式", "url": "地址", "events": "事件", "enabled": "启用"}
    for key, label in labels.items():
        if key in given and getattr(body, key) is None:
            raise HTTPException(422, f"「{label}」不能为空")
    if body.name is not None:
        hook.name = body.name
    if body.kind is not None:
        hook.kind = body.kind
    if body.url is not None:
        hook.url = body.url
    if body.events is not None:
        hook.events = [e for e in ("run.waiting", "run.done", "run.error") if e in body.events]
    if "apps" in given:
        hook.apps = body.apps
    if body.enabled is not None:
        hook.enabled = body.enabled
    await notifier.store.save_webhook(hook)
    return _hook_out(hook)


@router.delete("/webhooks/{webhook_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_webhook(webhook_id: str, request: Request) -> Response:
    if not await _notifier(request).store.delete_webhook(webhook_id):
        raise HTTPException(404, "没有找到这个 Webhook")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/webhooks/{webhook_id}/test")
async def test_webhook(webhook_id: str, request: Request) -> WebhookOut:
    notifier = _notifier(request)
    return _hook_out(await notifier.test_webhook(await _hook(notifier, webhook_id)))
