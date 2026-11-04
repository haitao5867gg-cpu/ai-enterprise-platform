"""Conversations: chatting with an app turn by turn. Contract: docs/api/conversations.md."""

from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field, StringConstraints, field_validator

from aep.api.runs import Pending, ProjectChoice, RunRef
from aep.api.validation import chinese_errors
from aep.auth import ROLE_LABELS, current_user, enabled, require, who
from aep.conversations import MAX_MESSAGE, MAX_TITLE, MESSAGE, Conversation
from aep.knowledge.citations import CitedSource
from aep.runtime.service import (
    ConversationBusy,
    InvalidValues,
    ModelNotAllowed,
    NoDraftToRun,
    NotConversable,
    ProjectProblem,
    RunService,
    RunView,
    UnknownApp,
    UnknownVersion,
)
from aep.runtime.store import FINISHED

router = APIRouter(
    prefix="/conversations",
    tags=["conversations"],
    route_class=chinese_errors({"message": "消息", "title": "标题"}),
)

NOT_FOUND = "没有找到这个会话"
Title = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_TITLE)]


class OpenConversation(BaseModel):
    app: str = Field(max_length=64)
    draft: bool = False
    input: dict[str, Any] = Field(default_factory=dict)
    title: Title | None = None
    requester: str | None = Field(default=None, max_length=100)
    project: ProjectChoice | None = None


class SendMessage(BaseModel):
    message: str = Field(max_length=MAX_MESSAGE)
    requester: str | None = Field(default=None, max_length=100)

    @field_validator("message")
    @classmethod
    def not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("消息不能为空")
        return value


class Rename(BaseModel):
    title: Title


class ConversationOut(BaseModel):
    conversation_id: str
    app: str
    app_title: str
    title: str
    draft: bool
    input: dict[str, Any]
    created_by: str | None
    created_at: str
    updated_at: str
    expires_at: str | None
    turn_count: int
    busy: bool


class Turn(BaseModel):
    turn: int
    run_id: str
    message: str
    reply: str | None
    status: str
    pending: Pending | None
    sources: list[CitedSource]
    app_version: int | None
    created_at: str
    finished_at: str | None


class ConversationDetail(ConversationOut):
    turns: list[Turn]


class TurnStarted(RunRef):
    turn: int


def _service(request: Request) -> RunService:
    return request.app.state.run_service


def owner_of(request: Request) -> str:
    """Whose conversations a request sees: the signed-in user's, or everyone's ("") with
    sign-in off (everyone is one person then, docs/api/auth.md)."""
    return current_user(request).user_id if enabled(request) else ""


def _admin(request: Request) -> bool:
    return enabled(request) and current_user(request).has("admin")


async def _visible(request: Request, conversation_id: str) -> Conversation:
    found = await _service(request).conversations.get(conversation_id)
    if found is None or (found.owner != owner_of(request) and not _admin(request)):
        raise HTTPException(404, NOT_FOUND)
    return found


async def out(service: RunService, found: Conversation) -> dict[str, Any]:
    """A conversation as the API shows it (``Conversation`` in the contract)."""
    titles = await service.app_titles()
    count, busy = (await service.turn_counts([found.conversation_id]))[found.conversation_id]
    days = service.settings.conversation_retention_days
    return {
        "conversation_id": found.conversation_id,
        "app": found.app,
        "app_title": titles.get(found.app, found.app),
        "title": found.shown_title,
        "draft": found.draft,
        "input": found.input,
        "created_by": found.created_by,
        "created_at": found.created_at.isoformat(),
        "updated_at": found.updated_at.isoformat(),
        "expires_at": (found.updated_at + timedelta(days=days)).isoformat() if days else None,
        "turn_count": count,
        "busy": busy,
    }


def _turn(view: RunView, reply: str | None) -> dict[str, Any]:
    r = view.record
    message = r.input.get("message")
    return {
        "turn": r.conversation_turn or 0,
        "run_id": r.run_id,
        "message": message if isinstance(message, str) else "",
        "reply": reply,
        "status": r.status,
        "pending": view.pending,
        "sources": view.sources,
        "app_version": r.app_version,
        "created_at": r.created_at.isoformat(),
        "finished_at": r.updated_at.isoformat() if r.status in FINISHED else None,
    }


async def detail(service: RunService, found: Conversation) -> dict[str, Any]:
    """A conversation with its turns, the first first."""
    turns = [_turn(view, reply) for view, reply in await service.turns(found.conversation_id)]
    return await out(service, found) | {"turns": turns}


def _invalid(exc: InvalidValues, turn: bool = False) -> HTTPException:
    """422 with each input's problem where it is, as ``POST /runs`` answers (not one
    sentence: the page shows each next to its field)."""
    # A turn's message is the request's own field; the other inputs are the conversation's.
    return HTTPException(
        422,
        [
            {
                "loc": ["body", "message"] if turn and key == MESSAGE else ["body", "input", key],
                "msg": message,
                "type": "value_error",
            }
            for key, message in exc.problems.items()
        ],
    )


@router.post("", status_code=status.HTTP_201_CREATED)
async def open_conversation(body: OpenConversation, request: Request) -> ConversationOut:
    if body.draft:
        require(request, "builder", f"没有权限：这项操作需要{ROLE_LABELS['builder']}")
    service = _service(request)
    try:
        found = await service.open_conversation(
            body.app,
            owner=owner_of(request),
            created_by=who(request, body.requester),
            draft=body.draft,
            input=body.input,
            title=body.title,
            project=body.project.model_dump() if body.project else None,
        )
    except (UnknownApp, UnknownVersion) as exc:
        raise HTTPException(404, f"没有应用 {body.app}") from exc
    except NotConversable as exc:
        raise HTTPException(409, "这个应用没有开对话") from exc
    except NoDraftToRun as exc:
        raise HTTPException(409, "没有草稿可以试") from exc
    except InvalidValues as exc:
        raise _invalid(exc) from exc
    except ProjectProblem as exc:
        raise HTTPException(422, str(exc)) from exc
    return ConversationOut(**await out(service, found))


@router.get("")
async def list_conversations(
    request: Request, app: str | None = None, draft: bool = False, limit: int = 50
) -> list[ConversationOut]:
    service = _service(request)
    found = await service.conversations.owned(
        owner_of(request), app or None, draft, min(max(limit, 1), 200)
    )
    return [ConversationOut(**await out(service, c)) for c in found]


@router.get("/{conversation_id}")
async def get_conversation(conversation_id: str, request: Request) -> ConversationDetail:
    found = await _visible(request, conversation_id)
    return ConversationDetail(**await detail(_service(request), found))


async def send(
    service: RunService, found: Conversation, message: str, requester: str | None
) -> Any:
    """Start the conversation's next turn, the errors as the API answers them."""
    try:
        return await service.send(found, message, requester)
    except ConversationBusy as exc:
        raise HTTPException(409, "上一条还没处理完") from exc
    except (UnknownApp, UnknownVersion) as exc:
        raise HTTPException(404, f"没有应用 {found.app}") from exc
    except NotConversable as exc:
        raise HTTPException(409, "这个应用没有开对话") from exc
    except NoDraftToRun as exc:
        raise HTTPException(409, "没有草稿可以试") from exc
    except InvalidValues as exc:
        raise _invalid(exc, turn=True) from exc
    except (ModelNotAllowed, ProjectProblem) as exc:
        raise HTTPException(422, str(exc)) from exc


@router.post("/{conversation_id}/messages", status_code=status.HTTP_201_CREATED)
async def send_message(conversation_id: str, body: SendMessage, request: Request) -> TurnStarted:
    found = await _visible(request, conversation_id)
    record = await send(_service(request), found, body.message, who(request, body.requester))
    return TurnStarted(
        run_id=record.run_id, status=record.status, turn=record.conversation_turn or 0
    )


@router.patch("/{conversation_id}")
async def rename(conversation_id: str, body: Rename, request: Request) -> ConversationOut:
    await _visible(request, conversation_id)
    service = _service(request)
    found = await service.conversations.update(conversation_id, title=body.title)
    if found is None:
        raise HTTPException(404, NOT_FOUND)
    return ConversationOut(**await out(service, found))


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete(conversation_id: str, request: Request) -> Response:
    await _visible(request, conversation_id)
    await _service(request).delete_conversation(conversation_id, who(request, None))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
