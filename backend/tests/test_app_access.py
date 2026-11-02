"""API keys, the app API and each app as an MCP tool (docs/api/app-access.md)."""

from typing import Any

import httpx
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from aep.app_access import ApiKeys, MemoryApiKeyStore, digest
from aep.app_mcp import input_schema
from tests.test_auth import Platform, signed
from tests.test_flow_apps import end, flow

FORM_APP = flow(
    {
        "id": "ask",
        "type": "human",
        "title": "补充",
        "mode": "form",
        "prompt": "请补充订单号",
        "fields": [{"key": "order", "label": "订单号", "type": "text", "required": True}],
    },
    end("{{ask.order}}"),
    edges=[{"from": "start", "to": "ask"}, {"from": "ask", "to": "end"}],
)


class Access(Platform):
    def __init__(self, enabled: bool = False) -> None:
        super().__init__(enabled)
        self.keys = ApiKeys(MemoryApiKeyStore())
        self.app.state.api_keys = self.keys
        self.app.state.web_url = "https://aep.example.com"

    async def key(self, app: str, name: str = "CRM 系统") -> str:
        _, secret = await self.keys.create(app, name, "王搭建")
        return secret

    async def form_app(self) -> None:
        async with self.client() as api:
            for method, path, body in (
                ("POST", "/apps", {"app": "orders", "title": "订单补录", "from": {"blank": True}}),
                ("PUT", "/apps/orders/draft", {"config": FORM_APP}),
                ("POST", "/apps/orders/draft/publish", {"note": "测试"}),
            ):
                resp = await api.request(method, path, json=body)
                assert resp.is_success, resp.text


def bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


async def test_builders_manage_keys_and_only_the_hash_is_kept():
    p = Access(enabled=True)
    await p.user("builder", ["builder"], "王搭建")
    await p.user("lilei", ["user"], "李雷")
    async with signed(await p.signed_in("lilei")) as api:
        assert (await api.get("/apps/hello/keys")).status_code == 403
        assert (await api.post("/apps/hello/keys", json={"name": "x"})).status_code == 403
    async with signed(await p.signed_in("builder")) as api:
        created = await api.post("/apps/hello/keys", json={"name": "CRM 系统"})
        assert created.status_code == 201, created.text
        key = created.json()
        secret = key["secret"]
        assert secret.startswith("aep_") and len(secret) == 47
        assert key["prefix"] == secret[:12] and key["created_by"] == "王搭建"
        listed = (await api.get("/apps/hello/keys")).json()
        assert [k["key_id"] for k in listed] == [key["key_id"]]
        assert "secret" not in listed[0]
        stored = next(iter(p.keys.store.keys.values()))
        assert stored.digest == digest(secret) and secret not in str(vars(stored))
        assert (await api.get("/apps/nope/keys")).status_code == 404
        assert (await api.post("/apps/hello/keys", json={"name": " "})).status_code == 422
        for n in range(19):
            assert (await api.post("/apps/hello/keys", json={"name": f"k{n}"})).is_success
        full = await api.post("/apps/hello/keys", json={"name": "多一个"})
        assert full.status_code == 409 and full.json()["detail"] == "一个应用最多 20 个密钥"

        assert (await api.delete(f"/apps/hello/keys/{key['key_id']}")).status_code == 204
        assert (await api.delete(f"/apps/hello/keys/{key['key_id']}")).status_code == 404
    async with p.client() as api:
        gone = await api.get("/v1/runs/whatever", headers=bearer(secret))
        assert gone.status_code == 401
    labels = [e["label"] for e in p.store.audit]
    assert "新建 API 密钥" in labels and "删除 API 密钥" in labels


async def test_the_app_api_runs_an_app_and_waits():
    p = Access()
    secret = await p.key("hello")
    async with p.client() as api:
        for headers in ({}, {"Authorization": "Bearer nope"}, bearer("aep_" + "x" * 43)):
            resp = await api.post("/v1/apps/hello/runs", json={"input": {}}, headers=headers)
            assert resp.status_code == 401
            assert resp.json()["detail"] == "缺少或无效的 API 密钥"
            assert resp.headers["www-authenticate"] == "Bearer"
        other = await api.post("/v1/apps/etl/runs", json={"input": {}}, headers=bearer(secret))
        assert other.status_code == 403
        assert other.json()["detail"] == "这个密钥不能调用应用 etl"

        started = await api.post(
            "/v1/apps/hello/runs",
            json={"input": {"text": "你好"}, "wait": 5},
            headers=bearer(secret),
        )
        assert started.status_code == 201, started.text
        run = started.json()
        # hello waits for an approval: not something a key may give.
        assert run["status"] == "waiting" and run["pending"]["kind"] != "form"
        assert run["pending"]["fields"] is None
        assert run["link"] == f"https://aep.example.com/runs/{run['run_id']}"
        refused = await api.post(
            f"/v1/runs/{run['run_id']}/answer", json={"values": {}}, headers=bearer(secret)
        )
        assert refused.status_code == 403
        assert refused.json()["detail"] == "这一步要审批者在平台上确认"
        # The run is on the platform like any other, started by the key.
        detail = (await api.get(f"/runs/{run['run_id']}")).json()
        assert detail["created_by"] == "API：CRM 系统"
        approved = await api.post(f"/runs/{run['run_id']}/resume", json={"decision": "approve"})
        assert approved.status_code == 202
        done = (await api.get(f"/v1/runs/{run['run_id']}?wait=5", headers=bearer(secret))).json()
        assert done["status"] == "done" and done["result"] is not None and done["pending"] is None
        late = await api.post(
            f"/v1/runs/{run['run_id']}/answer", json={"values": {}}, headers=bearer(secret)
        )
        assert late.status_code == 409

        invalid = await api.post(
            "/v1/apps/hello/runs", json={"input": {}, "wait": 61}, headers=bearer(secret)
        )
        assert invalid.status_code == 422
        missing = await api.get("/v1/runs/nope", headers=bearer(secret))
        assert missing.status_code == 404 and missing.json()["detail"] == "没有找到这次运行"
    await p.service.wait_idle()


async def test_forms_can_be_answered_and_keys_see_only_their_apps_runs():
    p = Access()
    await p.form_app()
    orders, hello = await p.key("orders"), await p.key("hello", "另一个系统")
    async with p.client() as api:
        bad = await api.post("/v1/apps/orders/runs", json={"input": {}}, headers=bearer(orders))
        assert bad.status_code == 422
        run = (
            await api.post(
                "/v1/apps/orders/runs",
                json={"input": {"question": "补录订单"}, "wait": 5},
                headers=bearer(orders),
            )
        ).json()
        assert run["status"] == "waiting" and run["pending"]["kind"] == "form"
        assert run["pending"]["prompt"] == "请补充订单号"
        assert [f["key"] for f in run["pending"]["fields"]] == ["order"]
        other = await api.get(f"/v1/runs/{run['run_id']}", headers=bearer(hello))
        assert other.status_code == 404
        empty = await api.post(
            f"/v1/runs/{run['run_id']}/answer", json={"values": {}}, headers=bearer(orders)
        )
        assert empty.status_code == 422
        answered = await api.post(
            f"/v1/runs/{run['run_id']}/answer",
            json={"values": {"order": "SO-1"}, "wait": 5},
            headers=bearer(orders),
        )
        assert answered.status_code == 200, answered.text
        assert answered.json()["status"] == "done"
        assert answered.json()["result"] == {"out": "SO-1"}
    actions = [(e["label"], e["target"].get("key"), e["user_id"]) for e in p.store.audit]
    assert ("经 API 发起运行", "CRM 系统", None) in actions
    assert ("经 API 填表单", "CRM 系统", None) in actions
    await p.service.wait_idle()


async def test_keys_never_sign_anyone_in():
    p = Access(enabled=True)
    secret = await p.key("hello")
    async with p.client() as api:
        # A key opens the app API only, not the platform.
        assert (await api.get("/runs", headers=bearer(secret))).status_code == 401
        resp = await api.post(
            "/v1/apps/hello/runs", json={"input": {"text": "你好"}}, headers=bearer(secret)
        )
        assert resp.status_code == 201
    await p.service.wait_idle()


def test_input_schema():
    assert input_schema(None)["required"] == ["request"]
    schema = input_schema(
        [
            {"key": "q", "label": "问题", "type": "textarea", "required": True, "max_length": 9},
            {"key": "n", "label": "数量", "type": "number", "default": 3},
            {"key": "ok", "label": "确认", "type": "boolean"},
            {
                "key": "c",
                "label": "城市",
                "type": "select",
                "choices": [{"value": "sh", "label": "上海"}],
                "required": True,
            },
            {"key": "src", "label": "数据源", "type": "datasource"},
        ]
    )
    assert schema["required"] == ["q", "c"]
    assert schema["properties"]["q"] == {"description": "问题", "type": "string", "maxLength": 9}
    assert schema["properties"]["n"] == {"description": "数量", "type": "number", "default": 3}
    assert schema["properties"]["ok"]["type"] == "boolean"
    assert schema["properties"]["c"]["enum"] == ["sh"]
    assert schema["properties"]["src"]["description"] == "数据源（数据源 id）"


def mcp_client(p: Access, app: str, secret: str) -> Client:
    http = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=p.app),
        base_url="http://test",
        headers=bearer(secret),
        timeout=30,
    )
    return Client(streamable_http_client(f"http://test/mcp/apps/{app}", http_client=http))


async def test_an_app_as_an_mcp_tool():
    p = Access()
    await p.form_app()
    secret = await p.key("orders")
    async with mcp_client(p, "orders", secret) as client:
        tools = (await client.list_tools()).tools
        assert [t.name for t in tools] == ["orders", "get_run"]
        assert tools[0].description.startswith("运行应用「订单补录」")
        assert tools[0].input_schema["required"] == ["question"]

        bad: Any = await client.call_tool("orders", {})
        assert bad.is_error and "问题不能为空" in bad.content[0].text
        started: Any = await client.call_tool("orders", {"question": "补录"})
        result = started.structured_content
        assert result["status"] == "waiting" and not started.is_error
        assert "运行在等人在平台上填写表单" in started.content[0].text
        assert result["run_id"] in started.content[0].text

        await p.service.resume(result["run_id"], "submit", None, "李雷", {"order": "SO-9"})
        await p.service.wait_idle()
        got: Any = await client.call_tool("get_run", {"run_id": result["run_id"]})
        assert got.structured_content["status"] == "done"
        assert got.structured_content["result"] == {"out": "SO-9"}
        assert "SO-9" in got.content[0].text
        missing: Any = await client.call_tool("get_run", {"run_id": "nope"})
        assert missing.is_error and missing.content[0].text == "没有找到这次运行"


@pytest.mark.parametrize(
    ("app", "key_app", "status"), [("orders", None, 401), ("hello", "orders", 403)]
)
async def test_mcp_needs_the_apps_key(app, key_app, status):
    p = Access()
    headers = bearer(await p.key(key_app)) if key_app else {}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=p.app), base_url="http://test"
    ) as api:
        resp = await api.post(
            f"/mcp/apps/{app}",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers=headers | {"Accept": "application/json, text/event-stream"},
        )
    assert resp.status_code == status
