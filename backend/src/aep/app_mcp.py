"""Each app as an MCP tool (docs/api/app-access.md, "MCP").

``/mcp/apps/{app}`` speaks stateless Streamable HTTP. A request needs an API key of
that app; the server then offers two tools: one that runs the app (named after it) and
``get_run``. Approvals and forms stay with people on the platform.
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

from aep.app_access import ApiKey, ApiKeys, bearer, run_result, settle
from aep.runtime.catalog import AppCatalog
from aep.runtime.service import (
    InvalidValues,
    ModelNotAllowed,
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


def input_schema(inputs: list[dict[str, Any]] | None) -> dict[str, Any]:
    """The JSON schema of a run's input: a flow app's start inputs, else ``request``."""
    if inputs is None:
        return {
            "type": "object",
            "properties": {"request": {"type": "string", "description": "需求"}},
            "required": ["request"],
        }
    properties: dict[str, Any] = {}
    required: list[str] = []
    for f in inputs:
        kind = f.get("type")
        prop: dict[str, Any] = {"description": f.get("label") or f["key"]}
        if kind == "number":
            prop["type"] = "number"
        elif kind == "boolean":
            prop["type"] = "boolean"
        elif kind == "select":
            prop["enum"] = [c["value"] for c in f.get("choices") or []]
        else:
            prop["type"] = "string"
            if f.get("max_length"):
                prop["maxLength"] = f["max_length"]
            if kind == "datasource":
                prop["description"] += "（数据源 id）"
        if f.get("default") is not None:
            prop["default"] = f["default"]
        properties[f["key"]] = prop
        if f.get("required") and f.get("default") is None:
            required.append(f["key"])
    return {"type": "object", "properties": properties, "required": required}


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
    """What the model reads: the outcome in a sentence or two, and the result."""
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
        run = types.Tool(
            name=tool_name(app),
            title=detail.get("title") or app,
            description=f"运行应用「{detail.get('title') or app}」。{about}".strip(),
            input_schema=input_schema(detail.get("inputs")),
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
                record = await service.create(key.app, args, requester=f"API：{key.name}")
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
        result = json.loads(
            json.dumps(run_result(view, who["web_url"]), default=lambda v: v.isoformat())
        )
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=summary(result))],
            structured_content=result,
            is_error=result["status"] == "error",
        )


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
