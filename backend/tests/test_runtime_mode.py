"""A runtime (AEP_MODE=runtime): installing signed packages, binding connectors, the
self-check and what it serves (docs/api/runtime.md, step 1)."""

import copy
import json
import os

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver
from mcp.server.mcpserver import MCPServer

from aep import __version__
from aep.app_access import ApiKeys, MemoryApiKeyStore
from aep.auth import LABELS, required, runtime_allows
from aep.config import Settings, get_settings
from aep.connector_check import ConnectorCheck
from aep.datasources import MemoryDataSourceStore
from aep.installs import Installed, Runtime, new_enough
from aep.main import create_app
from aep.packages import Signer
from aep.runtime import agent_app
from aep.runtime.catalog import AppCatalog
from aep.runtime.registry import AppContext
from aep.runtime.service import RunService
from aep.runtime.store import MemoryRunStore
from aep.skills import MemorySkillStore, SkillContent
from aep.tools.mcp import McpTools
from tests.conftest import SETTINGS, FakeGateway
from tests.test_agent_apps import new_agent
from tests.test_agent_apps import publish as publish_agent
from tests.test_packages import OTHER_SEED, SEED, Packaging

PLATFORM_STOCK = "http://stock.platform.test/mcp"
STOCK = "http://stock.coach.test/mcp"
HALF_STOCK = "http://half.coach.test/mcp"
SIGNER = Signer.from_text(SEED)
assert SIGNER is not None


def stock(full: bool = True) -> MCPServer:
    """A stock footage connector; without ``full`` it lacks the search tool."""
    server = MCPServer("fake-stock")

    if full:

        @server.tool()
        def search_videos(query: str, token: str) -> dict:
            """Search clips."""
            return {"clips": [f"{query}-1.mp4"], "token_ok": token == "k3y"}

    @server.tool()
    def download_video(clip: str) -> dict:
        """Download a clip."""
        return {"path": f"/data/{clip}"}

    return server


def coach_flow(datasource: str, model: str = "qwen") -> dict:
    nodes = [
        {
            "id": "start",
            "type": "start",
            "title": "开始",
            "inputs": [{"key": "topic", "label": "主题", "type": "text", "required": True}],
        },
        {
            "id": "find",
            "type": "tool",
            "title": "找素材",
            "datasource": datasource,
            "tool": "search_videos",
            "args": {"query": "{{start.topic}}", "token": "{{secret.stock_key}}"},
        },
        {
            "id": "say",
            "type": "llm",
            "title": "写字幕",
            **({"model": model} if model else {}),
            "skills": [{"name": "coach-style", "version": 1}],
            "prompt": "{{find.result}}",
        },
        {
            "id": "end",
            "type": "end",
            "title": "结束",
            "outputs": [{"key": "out", "from": ["{{say.text}}"]}],
        },
    ]
    edges = [
        {"from": "start", "to": "find"},
        {"from": "find", "to": "say"},
        {"from": "say", "to": "end"},
    ]
    return {"env": {"secrets": ["stock_key"]}, "graph": {"nodes": nodes, "edges": edges}}


async def packaged(model: str = "qwen", versions: int = 1) -> tuple[dict, ...]:
    """Packages of the coach app's versions 2, 3, … made on a platform."""
    made = []
    async with Packaging(servers={PLATFORM_STOCK: stock()}) as p:
        source = await p.sources.create("素材搜索", "mcp", PLATFORM_STOCK)
        await p.skills.create("coach-style", SkillContent("教练风格", "口语化，短句。"))
        await p.skills.publish("coach-style", "第一版", None)
        await p.new_app("coach", coach_flow(source.datasource_id, model))
        await p.api.put(
            "/apps/coach/runtime-requirements",
            json={
                "machine": {"cpu_cores": 4, "memory_gb": 8, "disk_gb": 100},
                "network": ["api.pixabay.test", "down.example.test"],
            },
        )
        for n in range(versions):
            if n:
                await new_version(p, "coach", coach_flow(source.datasource_id, model))
            got = await p.api.get(f"/apps/coach/versions/{2 + n}/package")
            assert got.status_code == 200, got.text
            made.append(got.json())
    return tuple(made)


def resign(package: dict, signer: Signer = SIGNER, **changes) -> dict:
    changed = {**copy.deepcopy(package), **changes}
    changed.pop("signature")
    return signer.sign(changed)


class Machine:
    """The coach's server: a runtime with its own stores, gateway and connectors."""

    def __init__(self, keys: list[str] | None = None, models: list[str] | None = None) -> None:
        self.gateway = FakeGateway(["字幕一", "字幕二", "字幕三"])
        self.aliases = models if models is not None else ["mock", "qwen"]
        self.sources = MemoryDataSourceStore()
        self.skills = MemorySkillStore()
        self.settings = SETTINGS.model_copy(
            update={
                "mode": "runtime",
                "platform_public_keys": [SIGNER.public_key] if keys is None else keys,
                "connector_hosts": ["*.test:*"],
            }
        )
        servers = {STOCK: stock(), HALF_STOCK: stock(full=False)}
        self.tools = McpTools(5, resolve=lambda url: servers[url])
        self.service = RunService(
            MemoryRunStore(),
            InMemorySaver(),
            {},
            AppContext(
                self.gateway.client, self.settings, datasources=self.sources, tools=self.tools
            ),
            skills=self.skills,
        )
        self.catalog = AppCatalog(self.service, self.sources, self.settings, models=self.models)
        self.reached: list[str] = []
        self.runtime = Runtime(
            self.service,
            self.catalog,
            self.sources,
            self.tools,
            ConnectorCheck(),
            self.settings,
            self.gateway.client,
            machine=lambda: {"cpu_cores": 4, "memory_gb": 4, "disk_gb": 200},
            reach=self.reach,
        )
        self.service.ready_check = self.runtime.ready
        self.catalog.secrets_changed = self.runtime.recheck
        self.keys = ApiKeys(MemoryApiKeyStore())
        app = create_app()
        app.state.mode = "runtime"
        app.state.run_service = self.service
        app.state.catalog = self.catalog
        app.state.skills = self.skills
        app.state.datasources = self.sources
        app.state.mcp_tools = self.tools
        app.state.runtime = self.runtime
        app.state.api_keys = self.keys
        app.state.web_url = "https://coach.example.test"
        app.dependency_overrides[get_settings] = lambda: self.settings
        self.app = app
        self.api = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")

    async def models(self) -> list[str]:
        return self.aliases

    async def reach(self, host: str) -> bool:
        self.reached.append(host)
        return host.startswith("api.")

    async def install(self, package: dict) -> httpx.Response:
        return await self.api.post("/runtime/packages", content=json.dumps(package))


async def new_version(p, app: str, config: dict) -> None:
    resp = await p.api.put(f"/apps/{app}/draft", json={"config": config})
    assert resp.status_code == 200, resp.text
    resp = await p.api.post(f"/apps/{app}/draft/publish", json={"note": "再发一版"})
    assert resp.status_code == 200, resp.text


def item(check: dict, kind: str, name: str) -> dict:
    return next(i for i in check["items"] if i["kind"] == kind and i["name"] == name)


# What a runtime serves


def test_the_runtime_serves_only_what_running_packages_needs() -> None:
    allowed = [
        ("GET", "/healthz"),
        ("POST", "/auth/login"),
        ("GET", "/users"),
        ("POST", "/v1/apps/{app}/runs"),
        ("GET", "/v1/runs/{run_id}"),
        ("POST", "/runtime/packages"),
        ("GET", "/apps/{app}/keys"),
        ("DELETE", "/apps/{app}/keys/{key_id}"),
        ("PUT", "/apps/{app}/secrets/{name}"),
        ("GET", "/apps/{app}/usage"),
        ("POST", "/media-libraries/{library_id}/items"),
        ("GET", "/datasources/{datasource_id}/tools"),
        ("POST", "/datasources/test"),
        ("GET", "/models"),
    ]
    refused = [
        ("GET", "/apps"),
        ("POST", "/apps"),
        ("PUT", "/apps/{app}/draft"),
        ("GET", "/apps/{app}/versions/{version}/package"),
        ("GET", "/runs"),
        ("POST", "/runs"),
        ("POST", "/datasources"),
        ("PATCH", "/datasources/{datasource_id}"),
        ("POST", "/models"),
        ("GET", "/skills"),
        ("GET", "/projects"),
        ("GET", "/knowledge-bases"),
        ("GET", "/apps/{app}/secrets/{name}"),
    ]
    assert [r for r in allowed if not runtime_allows(*r)] == []
    assert [r for r in refused if runtime_allows(*r)] == []
    assert required("POST", "/runtime/packages") == "builder"
    assert required("PUT", "/runtime/apps/{app}/bindings/{datasource_id}") == "builder"
    assert required("GET", "/runtime/apps/{app}/check") == "user"
    for action in (
        "POST /runtime/packages",
        "POST /runtime/apps/{app}/activate",
        "PUT /runtime/apps/{app}/bindings/{datasource_id}",
    ):
        assert action in LABELS


def test_runtime_settings_and_versions() -> None:
    assert Settings(mode="runtime", platform_public_keys=[SIGNER.public_key]).mode == "runtime"
    with pytest.raises(ValueError, match="第 2 把公钥"):
        Settings(platform_public_keys=[SIGNER.public_key, "bad"])
    assert new_enough("0.1.0", "0.1.0") and new_enough("0.0.9", "0.1.0")
    assert not new_enough("0.2.0", "0.1.0") and not new_enough("1.x", "0.1.0")
    assert new_enough(__version__)


async def test_routes_outside_the_runtime_answer_404():
    m = Machine()
    for method, path in (("GET", "/apps"), ("POST", "/runs"), ("GET", "/skills")):
        resp = await m.api.request(method, path, json={})
        assert (resp.status_code, resp.json()["detail"]) == (404, "运行包里没有这个接口")
    assert (await m.api.get("/datasources")).status_code == 200
    assert (await m.api.get("/runtime/apps")).json() == []
    # The platform has no runtime API.
    async with Packaging() as p:
        resp = await p.api.get("/runtime/apps")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "这台机器不是运行包（AEP_MODE=runtime）"


# Installing


async def test_install_bind_check_and_run():
    (package,) = await packaged()
    m = Machine()
    resp = await m.install(package)
    assert resp.status_code == 201, resp.text
    installed = resp.json()
    assert (installed["app"], installed["kind"], installed["version"]) == ("coach", "flow", 2)
    assert installed["title"] == "coach"
    assert installed["versions"][0]["package_id"] == package["package_id"]
    assert installed["requirements"] == package["requirements"]
    check = installed["check"]
    assert check["ok"] is False
    assert item(check, "platform", __version__)["ok"]
    assert item(check, "skill", "coach-style") == {
        "kind": "skill",
        "name": "coach-style",
        "version": 1,
        "required": True,
        "ok": True,
        "message": None,
    }
    assert item(check, "model", "qwen")["ok"]
    stock_item = item(check, "datasource", "素材搜索")
    assert (stock_item["ok"], stock_item["message"]) == (False, "还没有绑定连接器")
    assert item(check, "secret", "stock_key")["message"] == "还没有填这个密钥"
    memory = item(check, "machine", "内存")
    assert (memory["required"], memory["ok"]) == (False, False)
    assert memory["message"] == "要求 8 GB，这台机器 4 GB"
    assert item(check, "machine", "CPU")["ok"] and item(check, "machine", "硬盘")["ok"]
    assert item(check, "network", "api.pixabay.test")["ok"]
    assert item(check, "network", "down.example.test")["message"] == "5 秒内连不上 443 端口"
    # Installed as an ordinary flow app, under the platform's numbers.
    assert await m.service.versions.current("coach") == 2
    assert (await m.skills.version("coach-style", 1)).body == "口语化，短句。"

    # The same package again changes nothing.
    again = await m.install(package)
    assert again.status_code == 200 and len(again.json()["versions"]) == 1

    # /v1 says what is missing until the check passes.
    key = (await m.api.post("/apps/coach/keys", json={"name": "教练页面"})).json()["secret"]
    v1 = {"Authorization": f"Bearer {key}"}
    blocked = await m.api.post("/v1/apps/coach/runs", json={"input": {"topic": "深蹲"}}, headers=v1)
    assert blocked.status_code == 503
    assert blocked.json()["detail"] == (
        "这个应用还不能运行：缺 素材搜索连接器、密钥 stock_key（运行包自检没通过）"
    )

    source_id = package["requirements"]["datasources"][0]["datasource_id"]
    path = f"/runtime/apps/coach/bindings/{source_id}"
    missing = await m.api.put(path, json={"connector_url": HALF_STOCK})
    assert (missing.status_code, missing.json()["detail"]) == (422, "连接器缺少工具：search_videos")
    other = await m.api.put(
        "/runtime/apps/coach/bindings/5b3c0000-0000-4000-8000-000000000000",
        json={"connector_url": STOCK},
    )
    assert (other.status_code, other.json()["detail"]) == (404, "这个应用不需要这个数据源")
    bound = await m.api.put(path, json={"connector_url": STOCK})
    assert bound.status_code == 201, bound.text
    assert bound.json()["datasource_id"] == source_id
    assert (bound.json()["name"], bound.json()["kind"]) == ("素材搜索", "mcp")
    moved = await m.api.put(path, json={"connector_url": STOCK})
    assert moved.status_code == 200
    assert item(await m.runtime.latest("coach"), "datasource", "素材搜索")["ok"]

    assert (
        await m.api.put("/apps/coach/secrets/stock_key", json={"value": "k3y"})
    ).status_code == 204
    passed = (await m.api.get("/runtime/apps/coach/check")).json()
    assert passed["ok"] is True

    run = await m.api.post(
        "/v1/apps/coach/runs", json={"input": {"topic": "深蹲"}, "wait": 5}, headers=v1
    )
    assert run.status_code == 201, run.text
    await m.service.wait_idle()
    done = (await m.api.get(f"/v1/runs/{run.json()['run_id']}", headers=v1)).json()
    assert done["status"] == "done", done
    assert done["result"] == {"out": "字幕一"}

    # A fresh check also calls each model once.
    calls = len(m.gateway.requests)
    fresh = (await m.api.get("/runtime/apps/coach/check", params={"fresh": "true"})).json()
    assert fresh["ok"] and len(m.gateway.requests) == calls + 1
    assert m.gateway.requests[-1]["body"]["max_tokens"] == 1


async def test_what_a_runtime_refuses_to_install():
    (package,) = await packaged()
    m = Machine()
    for body in ("not json", json.dumps([1]), json.dumps({**package, "format": "x"})):
        resp = await m.api.post("/runtime/packages", content=body)
        assert (resp.status_code, resp.json()["detail"]) == (422, "不是流程包文件")
    big = await m.api.post("/runtime/packages", content=b" " * (5 * 1024 * 1024 + 1))
    assert (big.status_code, big.json()["detail"]) == (413, "流程包不能超过 5 MB")

    keyless = Machine(keys=[])
    resp = await keyless.install(package)
    assert resp.status_code == 503
    assert resp.json()["detail"] == "运行包没有配置平台公钥（AEP_PLATFORM_PUBLIC_KEYS）"
    other = Signer.from_text(OTHER_SEED)
    assert other is not None
    stranger = await Machine(keys=[other.public_key]).install(package)
    assert stranger.json()["detail"] == "这个包不是用这台运行包认识的平台密钥签的"
    tampered = await m.install({**package, "title": "改过的"})
    assert (tampered.status_code, tampered.json()["detail"]) == (
        422,
        "流程包的签名不对，可能被改过",
    )

    newer = copy.deepcopy(package)
    newer["requirements"]["platform"]["min_version"] = "9.0.0"
    resp = await m.install(resign(newer))
    assert resp.status_code == 422
    assert resp.json()["detail"] == (
        f"这个包要求平台 9.0.0 以上，这台运行包是 {__version__}，请先升级镜像"
    )
    assert await m.runtime.store.apps() == []

    assert (await m.install(package)).status_code == 201
    reused = await m.install(resign(package, package_id="another-id"))
    assert (reused.status_code, reused.json()["detail"]) == (
        409,
        "第 2 版已经装过了，平台重新打的包要用新版本",
    )
    changed_skill = copy.deepcopy(package)
    changed_skill["skills"][0]["body"] = "换了说法。"
    resp = await m.install(resign(changed_skill, package_id="third-id", version=3))
    assert (resp.status_code, resp.json()["detail"]) == (
        409,
        "Skill「coach-style」第 1 版和这台机器上已有的不一样",
    )
    assert len(await m.runtime.store.versions("coach")) == 1


async def test_an_app_of_the_other_kind_keeps_its_name():
    (package,) = await packaged()
    async with Packaging() as p:
        await new_agent(p, "coach")
        await publish_agent(p, "coach", agent_app.config("你是教练助手。"))
        agent_package = (await p.api.get("/apps/coach/versions/2/package")).json()
    m = Machine()
    assert (await m.install(agent_package)).status_code == 201
    resp = await m.install(resign(package, version=5))
    assert (resp.status_code, resp.json()["detail"]) == (
        409,
        "这台机器上已经有一个同名的智能体应用",
    )


async def test_switching_versions_and_the_default_model():
    v2, v3 = await packaged(versions=2)
    m = Machine(models=["qwen"])
    assert (await m.install(v2)).status_code == 201
    third = (await m.install(v3)).json()
    assert third["version"] == 3
    assert [v["version"] for v in third["versions"]] == [3, 2]
    back = await m.api.post("/runtime/apps/coach/activate", json={"version": 2})
    assert back.status_code == 200 and back.json()["version"] == 2
    assert await m.service.versions.current("coach") == 2
    nope = await m.api.post("/runtime/apps/coach/activate", json={"version": 9})
    assert (nope.status_code, nope.json()["detail"]) == (404, "没有装过第 9 版")
    assert (await m.api.get("/runtime/apps/nope")).status_code == 404
    listed = (await m.api.get("/runtime/apps")).json()
    assert [a["app"] for a in listed] == ["coach"]

    # A node that names no model needs the runtime's default chat model on its gateway.
    (unnamed,) = await packaged(model="")
    bare = Machine(models=["qwen"])
    bare.service.default_model = "company"
    check = (await bare.install(unnamed)).json()["check"]
    assert unnamed["requirements"]["default_model"] is True
    assert item(check, "model", "company")["message"] == (
        "默认对话模型不在模型网关上，请在 LiteLLM 配置里加上"
    )


async def ready_machine(package: dict) -> tuple[Machine, dict[str, str]]:
    """A runtime with ``package`` installed, its connector bound and its secret filled; with
    the headers of an API key for /v1."""
    m = Machine()
    assert (await m.install(package)).status_code == 201
    source_id = package["requirements"]["datasources"][0]["datasource_id"]
    bound = await m.api.put(
        f"/runtime/apps/coach/bindings/{source_id}", json={"connector_url": STOCK}
    )
    assert bound.status_code == 201, bound.text
    filled = await m.api.put("/apps/coach/secrets/stock_key", json={"value": "k3y"})
    assert filled.status_code == 204
    key = (await m.api.post("/apps/coach/keys", json={"name": "教练页面"})).json()["secret"]
    return m, {"Authorization": f"Bearer {key}"}


async def start(m: Machine, headers: dict[str, str]) -> httpx.Response:
    return await m.api.post(
        "/v1/apps/coach/runs", json={"input": {"topic": "深蹲"}}, headers=headers
    )


async def test_a_failed_model_call_stops_runs_until_the_model_answers_again():
    (package,) = await packaged()
    m, v1 = await ready_machine(package)
    m.gateway.status = 401
    fresh = (await m.api.get("/runtime/apps/coach/check", params={"fresh": "true"})).json()
    assert fresh["ok"] is False
    assert item(fresh, "model", "qwen")["message"].startswith("别名在，但调用失败：")

    # Before refusing a run the runtime checks again, and calls the failed alias again
    # rather than trusting the alias list.
    calls = len(m.gateway.requests)
    blocked = await start(m, v1)
    assert (blocked.status_code, blocked.json()["detail"]) == (
        503,
        "这个应用还不能运行：缺 qwen 模型别名（运行包自检没通过）",
    )
    assert len(m.gateway.requests) == calls + 1

    m.gateway.status = 200
    assert (await start(m, v1)).status_code == 201
    await m.service.wait_idle()
    # Once it answers, checks without fresh=true stop calling it.
    calls = len(m.gateway.requests)
    assert (await m.runtime.check("coach"))["ok"] is True
    assert len(m.gateway.requests) == calls


async def test_a_passed_check_lets_only_its_own_version_run():
    (package,) = await packaged()
    m, v1 = await ready_machine(package)
    needs_more = copy.deepcopy(package)
    needs_more["requirements"]["secrets"] = ["stock_key", "music_key"]
    assert (await m.install(resign(needs_more, package_id="v3", version=3))).status_code == 201
    back = await m.api.post("/runtime/apps/coach/activate", json={"version": 2})
    assert back.json()["check"]["ok"] is True

    # Version 3 becomes current before its own check, as it does while a package installs
    # or a version switches: version 2's passed check does not let it run.
    await m.service.versions.activate("coach", 3)
    blocked = await start(m, v1)
    assert (blocked.status_code, blocked.json()["detail"]) == (
        503,
        "这个应用还不能运行：缺 密钥 music_key（运行包自检没通过）",
    )
    assert (await m.runtime.latest("coach"))["ok"] is False


async def test_an_install_that_stopped_halfway_finishes_when_installed_again():
    (package,) = await packaged()
    m, v1 = await ready_machine(package)
    config = (await m.service.versions.version("coach", 2)).config
    # Version 3 was added and made current, then the install stopped before its record.
    await m.service.versions.install("coach", 3, config, "装到一半", None)
    blocked = await start(m, v1)
    assert (blocked.status_code, blocked.json()["detail"]) == (
        503,
        "这个应用在运行包上没有装完，请重新装一次流程包",
    )

    finished = await m.install(resign(package, package_id="v3", version=3))
    assert finished.status_code == 201, finished.text
    assert [v["version"] for v in finished.json()["versions"]] == [3, 2]
    assert (await start(m, v1)).status_code == 201

    # The same number with another configuration is still somebody else's version.
    other = copy.deepcopy(config)
    other["graph"]["nodes"][1]["title"] = "换个名字"
    await m.service.versions.install("coach", 4, other, "别的配置", None)
    taken = await m.install(resign(package, package_id="v4", version=4))
    assert (taken.status_code, taken.json()["detail"]) == (
        409,
        "第 4 版已经装过了，平台重新打的包要用新版本",
    )


@pytest.mark.skipif(not os.environ.get("AEP_TEST_DATABASE_URL"), reason="needs Postgres")
async def test_installs_in_postgres():
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    from aep.installs import PostgresInstallStore
    from aep.runtime.versions import PostgresAppVersionStore, VersionExists
    from aep.skills import PostgresSkillStore

    async with AsyncConnectionPool(
        os.environ["AEP_TEST_DATABASE_URL"],
        kwargs={"autocommit": True, "row_factory": dict_row},
        open=False,
    ) as pool:
        store = PostgresInstallStore(pool)
        await store.setup()
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM runtime_installs WHERE app = 'rt-test'")
        first = Installed("rt-test", 2, "rt-test-p2", "2026-11-15T00:00:00+00:00", {"a": 1})
        second = Installed("rt-test", 3, "rt-test-p3", "2026-11-16T00:00:00+00:00", {"a": 2})
        await store.add(first)
        await store.add(second)
        assert [i.version for i in await store.versions("rt-test")] == [3, 2]
        found = await store.find("rt-test-p2")
        assert found is not None and found.requirements == {"a": 1}
        assert "rt-test" in await store.apps()

        # Versions and skills go in under the platform's numbers.
        versions = PostgresAppVersionStore(pool)
        await versions.setup()
        skills = PostgresSkillStore(pool)
        await skills.setup()
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM app_versions WHERE app = 'rt-test'")
            await conn.execute("DELETE FROM app_heads WHERE app = 'rt-test'")
            await conn.execute("DELETE FROM skill_versions WHERE name = 'rt-test-skill'")
        await versions.install("rt-test", 3, {"x": 3}, "包", None)
        await versions.install("rt-test", 2, {"x": 2}, "包", None)
        assert await versions.current("rt-test") == 2
        assert [v.version for v in await versions.versions("rt-test")] == [3, 2]
        with pytest.raises(VersionExists):
            await versions.install("rt-test", 3, {}, "包", None)
        # Finishing a half-done install compares the stored configuration with the package's.
        nested = {
            "env": {"secrets": ["k"]},
            "graph": {"nodes": [{"id": "say", "title": "写字幕", "temperature": 0.2}], "edges": []},
        }
        await versions.install("rt-test", 5, nested, "包", None)
        got_version = await versions.version("rt-test", 5)
        assert got_version is not None and got_version.config == nested
        content = SkillContent("说明", "正文", {"a.md": "参考"}, version=4)
        assert await skills.install("rt-test-skill", content) is True
        assert await skills.install("rt-test-skill", content) is False
        got = await skills.version("rt-test-skill", 4)
        assert got is not None and got.references == {"a.md": "参考"}
