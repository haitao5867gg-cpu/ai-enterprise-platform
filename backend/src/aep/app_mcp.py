"""Each app as an MCP tool (docs/api/app-access.md, "MCP").

``/mcp/apps/{app}`` speaks stateless Streamable HTTP. A request needs an API key of
that app; the server then offers two tools: one that runs the app (named after it) and
``get_run``. Approvals and forms stay with people on the platform. An app that converses
takes ``conversation_id`` or ``new_conversation`` too: the conversation lives on the
platform, the stateless endpoint only carries its id (docs/api/app-access.md, "会话").
"""

import json
import logging
from typing import Any

from mcp import types
from mcp.server import Server
from mcp.server.context import ServerRequestContext
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from aep.app_access import (
    ApiKey,
    ApiKeys,
    ConversationProblem,
    bearer,
    run_result,
    settle,
    start,
)
from aep.conversations import MESSAGE
from aep.entries import CLOSED_ENTRY
from aep.runtime.catalog import AppCatalog
from aep.runtime.flow.schema import input_schema
from aep.runtime.service import (
    ConversationBusy,
    InvalidValues,
    ModelNotAllowed,
    NoDraftToRun,
    NotConversable,
    ProjectProblem,
    RunNotFound,
    RunService,
    UnknownApp,
    UnknownVersion,
)

log = logging.getLogger(__name__)

TOOL_WAIT = 50
STATE = "aep_mcp"


def tool_name(app: str) -> str:
    return app.replace("-", "_")


# What the run tool of an app that converses takes besides its inputs.
CONVERSATION = {
    "conversation_id": {
        "type": "string",
        "description": "要接着上次聊，就传上次结果里的 conversation_id",
    },
    "new_conversation": {
        "type": "boolean",
        "description": "要开始一段新的多轮对话，传 true",
    },
}


def conversing(schema: dict[str, Any]) -> dict[str, Any]:
    """The run tool's schema for an app that converses: going on with a conversation
    takes only the message, so only it is required; the start's other required inputs
    are checked when a conversation opens or the app runs once (docs/api/app-access.md)."""
    properties = dict(schema["properties"])
    for key in schema["required"]:
        if key != MESSAGE:
            said = properties[key].get("description") or key
            properties[key] = {
                **properties[key],
                "description": f"{said}（开新会话或单次调用时必填）",
            }
    return {
        **schema,
        "properties": {**properties, **CONVERSATION},
        "required": [MESSAGE] if MESSAGE in schema["required"] else [],
    }


GET_RUN = types.Tool(
    name="get_run",
    description="查一次运行现在的状态和结果。运行还在跑时最多等 wait_seconds 秒。",
    input_schema={
        "type": "object",
        "properties": {
            "run_id": {"type": "string"},
            "wait_seconds": {"type": "integer", "minimum": 0, "maximum": TOOL_WAIT},
        },
        "required": ["run_id"],
    },
)


def summary(result: dict[str, Any]) -> str:
    """What the model reads: the outcome in a sentence or two, and the result; for a turn
    of a conversation, the id to go on with."""
    said = _outcome(result)
    if result.get("conversation_id"):
        said += f"\n要接着这段对话，下次传 conversation_id：{result['conversation_id']}。"
    return said


def _outcome(result: dict[str, Any]) -> str:
    status, link = result["status"], result["link"]
    if status == "done":
        body = json.dumps(result["result"], ensure_ascii=False, indent=2, default=str)
        return f"运行完成（{link}）。结果：\n{body}"
    if status == "waiting":
        pending = result["pending"] or {}
        what = "填写表单" if pending.get("kind") == "form" else "确认"
        return (
            f"运行在等人在平台上{what}：{pending.get('prompt') or ''}\n"
            f"链接：{link}。处理后用 get_run 查结果，run_id 是 {result['run_id']}。"
        )
    if status == "running":
        return f"运行还在进行，稍后用 get_run 查结果，run_id 是 {result['run_id']}（{link}）。"
    if status == "error":
        return f"运行失败：{result['error']}（{link}）"
    return f"运行已{ {'cancelled': '取消'}.get(status, status) }（{link}）。"


class AppTools:
    """The MCP server; who is calling comes with each request (``scope["state"]``)."""

    def __init__(self) -> None:
        self.server: Server[Any] = Server(
            "aep-apps",
            version="0.1.0",
            instructions="运行企业 AI 应用平台上的一个应用，并查它的运行结果。",
            on_list_tools=self._list_tools,
            on_call_tool=self._call_tool,
        )

    @staticmethod
    def _who(ctx: ServerRequestContext[Any]) -> dict[str, Any]:
        request = ctx.request
        assert isinstance(request, Request)
        return request.state.aep_mcp

    async def _list_tools(
        self, ctx: ServerRequestContext[Any], params: types.PaginatedRequestParams | None
    ) -> types.ListToolsResult:
        who = self._who(ctx)
        catalog: AppCatalog = who["catalog"]
        app: str = who["key"].app
        detail = await catalog.detail(app)
        about = "\n".join(p for p in (detail.get("title"), detail.get("summary")) if p)
        schema = input_schema(detail.get("inputs"))
        if await _converses(who["service"], app):
            schema = conversing(schema)
            about += (
                "\n可以多轮对话：要接着上次聊，就传上次结果里的 conversation_id；"
                "要开始一段新的多轮对话，传 new_conversation: true。"
            )
        run = types.Tool(
            name=tool_name(app),
            title=detail.get("title") or app,
            description=f"运行应用「{detail.get('title') or app}」。{about}".strip(),
            input_schema=schema,
        )
        return types.ListToolsResult(tools=[run, GET_RUN])

    async def _call_tool(
        self, ctx: ServerRequestContext[Any], params: types.CallToolRequestParams
    ) -> types.CallToolResult:
        who = self._who(ctx)
        key: ApiKey = who["key"]
        service: RunService = who["service"]
        args = dict(params.arguments or {})
        try:
            if params.name == tool_name(key.app):
                conversation_id = args.pop("conversation_id", None)
                new_conversation = args.pop("new_conversation", False) is True
                record = await start(
                    service,
                    key,
                    args,
                    conversation_id=str(conversation_id) if conversation_id else None,
                    new_conversation=new_conversation,
                    entry="mcp",
                )
                view = await settle(service, record.run_id, TOOL_WAIT)
            elif params.name == "get_run":
                run_id = str(args.get("run_id") or "")
                wait = args.get("wait_seconds") or 0
                view = await service.get(run_id)
                if view.record.app != key.app:
                    raise RunNotFound(run_id)
                if isinstance(wait, int | float) and wait > 0:
                    view = await settle(service, run_id, min(float(wait), TOOL_WAIT))
            else:
                return _error(f"没有工具 {params.name}")
        except InvalidValues as exc:
            return _error("输入不对：" + "；".join(exc.problems.values()))
        except RunNotFound:
            return _error("没有找到这次运行")
        except (UnknownApp, UnknownVersion):
            return _error(f"没有应用 {key.app}")
        except (ModelNotAllowed, ProjectProblem) as exc:
            return _error(str(exc))
        except ConversationProblem as exc:
            return _error(exc.message)
        except (NotConversable, NoDraftToRun):
            return _error("这个应用没有开对话")
        except ConversationBusy:
            return _error("上一条还没处理完，稍后用 get_run 查它的结果")
        result = json.loads(
            json.dumps(run_result(view, who["web_url"]), default=lambda v: v.isoformat())
        )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=summary(result))],
            structured_content=result,
            is_error=result["status"] == "error",
        )


async def _converses(service: RunService, app: str) -> bool:
    try:
        await service.conversable(app)
    except (UnknownApp, UnknownVersion, NotConversable, NoDraftToRun):
        return False
    return True


def _error(message: str) -> types.CallToolResult:
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=message)], is_error=True
    )


class AppMcpEndpoint:
    """ASGI app mounted at ``/mcp/apps``: checks the key, then hands the request to a
    stateless session manager (one per request: nothing is kept between requests)."""

    def __init__(self) -> None:
        self.tools = AppTools()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        request = Request(scope, receive)
        state = request.app.state
        path = scope["path"]
        root = scope.get("root_path", "")
        if root and path.startswith(root):
            path = path[len(root) :]
        app = path.strip("/")
        keys: ApiKeys | None = getattr(state, "api_keys", None)
        key = await keys.check(bearer(request.headers.get("authorization"))) if keys else None
        if key is None:
            response = JSONResponse(
                {"detail": "缺少或无效的 API 密钥"},
                401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            return await response(scope, receive, send)
        if key.app != app:
            response = JSONResponse({"detail": f"这个密钥不能调用应用 {app}"}, 403)
            return await response(scope, receive, send)
        if not (await state.run_service.entries.get(app)).mcp:
            response = JSONResponse({"detail": CLOSED_ENTRY}, 403)
            return await response(scope, receive, send)
        scope.setdefault("state", {})[STATE] = {
            "key": key,
            "service": state.run_service,
            "catalog": state.catalog,
            "web_url": state.web_url,
        }
        manager = StreamableHTTPSessionManager(
            self.tools.server,
            json_response=True,
            stateless=True,
            # Callers are other systems with a key, not browsers: no rebinding to guard.
            security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
        async with manager.run():
            await manager.handle_request(scope, receive, send)
