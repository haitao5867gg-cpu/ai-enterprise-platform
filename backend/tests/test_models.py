"""Models API (docs/api/models.md): the gateway's models, adding and changing them on the
page, tests, defaults, usage and call records. A fake management API stands in for the
gateway and FakeGateway answers the model calls; nothing leaves the process."""

import json
from datetime import UTC, datetime, timedelta

import httpx
import openai
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from aep import llm
from aep.datasources import MemoryDataSourceStore
from aep.main import create_app
from aep.models import (
    PROBLEMS,
    MemoryCallStore,
    MemorySettingStore,
    ModelService,
    UsageSummary,
)
from aep.runtime.catalog import AppCatalog
from aep.runtime.flow.builtin import HELLO
from aep.runtime.registry import AppContext, default_apps
from aep.runtime.service import RunService
from aep.runtime.store import MemoryRunStore
from tests.conftest import SETTINGS, FakeGateway
from tests.fake_gateway_admin import FakeGatewayAdmin

CLAUDE = {
    "alias": "claude-sonnet",
    "display_name": "Claude Sonnet 5.5",
    "purpose": "chat",
    "provider": "anthropic",
    "model": "claude-sonnet-5-5",
    "key": {"source": "page", "value": "sk-ant-secret-123"},
}


class PickyGateway(FakeGateway):
    """A FakeGateway whose calls to some aliases fail with a given status and message."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.failing: dict[str, tuple[int, str]] = {}
        self.cost: str | None = None

    def _handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body.get("model") in self.failing:
            self.requests.append(
                {"url": str(request.url), "headers": request.headers, "body": body}
            )
            status, message = self.failing[body["model"]]
            return httpx.Response(status, json={"error": {"message": message}})
        resp = super()._handle(request)
        if self.cost is not None:
            resp.headers["x-litellm-response-cost"] = self.cost
        return resp


class Platform:
    def __init__(self, db: str = "connected", **settings) -> None:
        self.settings = SETTINGS.model_copy(update=settings)
        self.gateway = PickyGateway()
        self.fake = FakeGatewayAdmin(db=db)
        self.sources = MemoryDataSourceStore()
        self.service = RunService(
            MemoryRunStore(),
            InMemorySaver(),
            default_apps(),
            AppContext(self.gateway.client, self.settings, datasources=self.sources),
        )
        self.catalog = AppCatalog(self.service, self.sources, self.settings)
        self.calls = MemoryCallStore()
        self.store = MemorySettingStore()

        def set_default(alias: str) -> None:
            self.service.default_model = alias

        self.models = ModelService(
            self.fake.admin(self.settings),
            self.calls,
            self.store,
            self.settings,
            self.gateway.client,
            usage_of=self.catalog.model_usage,
            on_default_chat=set_default,
        )
        self.catalog.model_choices = self.models.choices
        self.app = create_app()

    async def __aenter__(self) -> "Platform":
        await self.models.setup()
        llm.set_recorder(self.calls.record)
        self.app.state.models = self.models
        self.app.state.catalog = self.catalog
        self.app.state.run_service = self.service
        self.app.state.datasources = self.sources
        self.app.state.skills = self.service.skills
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test"
        )
        self.api = await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc) -> None:
        llm.set_recorder(None)
        await self._client.__aexit__(*exc)

    def called(self) -> list[str]:
        return [r["body"]["model"] for r in self.gateway.requests]


def model(listing: dict, alias: str) -> dict:
    return next(m for m in listing["models"] if m["alias"] == alias)


async def test_list_reads_the_gateway_with_defaults_and_usage():
    async with Platform() as p:
        p.calls.calls.append(
            (
                datetime.now(UTC),
                llm.CallRecord("company", "chat", "r1", "draft", 100, 20, 0.002, True, None, 900),
            )
        )
        p.calls.calls.append(
            (
                datetime.now(UTC),
                llm.CallRecord("company", "chat", None, None, 0, 0, None, False, "auth", 50),
            )
        )
        p.calls.calls.append(
            (
                datetime.now(UTC) - timedelta(days=40),
                llm.CallRecord("company", "chat", None, None, 9, 9, 9.0, True, None, 1),
            )
        )
        resp = await p.api.get("/models")

    assert resp.status_code == 200
    listing = resp.json()
    assert listing["gateway"] == "ok"
    assert listing["defaults"] == {"chat": "mock", "embedding": "mock-embed"}
    assert [m["alias"] for m in listing["models"]] == ["mock", "company", "company-embed"]
    company = model(listing, "company")
    assert company | {"usage": None} == {
        "alias": "company",
        "display_name": "公司网关 DeepSeek",
        "purpose": "chat",
        "provider": "openai_compatible",
        "model": "deepseek-v4.1-flash-new",
        "api_base": "https://aigw.example.com/v1",
        "key": {"source": "unknown", "env": None},
        "description": "",
        "source": "config",
        "is_default": False,
        "allowed": True,
        "context_window": None,
        "price": None,
        "usage": None,
    }
    usage = company["usage"]
    assert (usage["calls"], usage["errors"], usage["prompt_tokens"]) == (2, 1, 100)
    assert usage["cost_usd"] == pytest.approx(0.002)
    assert usage["last_error"] == PROBLEMS["auth"]
    assert model(listing, "mock")["is_default"] and model(listing, "mock")["provider"] == "mock"
    embed = model(listing, "company-embed")
    assert (embed["purpose"], embed["display_name"]) == ("embedding", "company-embed")
    assert embed["usage"]["cost_usd"] is None


async def test_gateway_without_db_or_down():
    async with Platform(db="Not connected") as p:
        listing = (await p.api.get("/models")).json()
        refused = await p.api.post("/models", json=CLAUDE)
    assert listing["gateway"] == "no_db" and len(listing["models"]) == 3
    assert refused.status_code == 503 and "没有接数据库" in refused.json()["detail"]

    async with Platform() as p:
        p.fake.up = False
        listing = (await p.api.get("/models")).json()
        refused = await p.api.post("/models", json=CLAUDE)
    assert (listing["gateway"], listing["models"]) == ("down", [])
    assert refused.status_code == 503 and refused.json()["detail"] == "连不上模型网关"


async def test_add_claude_from_the_page():
    async with Platform() as p:
        resp = await p.api.post("/models", json=CLAUDE | {"description": "主力模型"})
        listing = (await p.api.get("/models")).json()

    assert resp.status_code == 201, resp.text
    added = resp.json()
    assert added["source"] == "page" and added["provider"] == "anthropic"
    assert added["model"] == "claude-sonnet-5-5" and added["key"] == {"source": "page", "env": None}
    assert "sk-ant" not in json.dumps(listing) and "sk-ant" not in resp.text
    dep = p.fake.by_alias("claude-sonnet")
    assert dep["litellm_params"] == {
        "model": "anthropic/claude-sonnet-5-5",
        "api_key": "sk-ant-secret-123",
    }
    assert dep["model_info"]["id"] == "aep-claude-sonnet"
    assert dep["model_info"]["aep_description"] == "主力模型"
    # The test call went through the gateway under the alias and was recorded.
    assert p.called() == ["claude-sonnet"]
    test_body = p.gateway.requests[0]["body"]
    assert test_body["max_tokens"] == 16 and "OK" in test_body["messages"][0]["content"]
    assert added["usage"]["calls"] == 1


async def test_a_failing_test_keeps_nothing():
    async with Platform() as p:
        p.gateway.failing["claude-sonnet"] = (
            401,
            'litellm.AuthenticationError: AnthropicException - {"type":"error","error":'
            '{"type":"authentication_error","message":"invalid x-api-key"}}',
        )
        resp = await p.api.post("/models", json=CLAUDE)
        p.gateway.failing["claude-sonnet"] = (
            400,
            'litellm.BadRequestError: AnthropicException - {"type":"error","error":{"type":'
            '"invalid_request_error","message":"Your credit balance is too low to access the '
            'Anthropic API."}}\n\nLiteLLM: model group failed',
        )
        quota = await p.api.post("/models", json=CLAUDE)
        p.gateway.failing["claude-sonnet"] = (500, "OpenAIException - Connection error.")
        unreachable = await p.api.post("/models", json=CLAUDE)

    assert resp.status_code == 422
    assert resp.json()["detail"] == "测试没有通过：" + PROBLEMS["auth"]
    assert quota.json()["detail"] == "测试没有通过：" + PROBLEMS["quota"]
    assert unreachable.json()["detail"] == "测试没有通过：" + PROBLEMS["unreachable"]
    assert p.fake.by_alias("claude-sonnet") is None


@pytest.mark.parametrize(
    ("change", "detail"),
    [
        ({"alias": "Claude"}, "「别名」的格式、长度或范围不正确"),
        ({"alias": "mock"}, "已经有叫「mock」的模型了"),
        ({"purpose": "rerank"}, "页面上只能新增对话模型和向量模型，重排模型请在配置文件里加"),
        ({"provider": "acme"}, "「供应商」的格式、长度或范围不正确"),
        ({"key": None}, "请求字段不能为 null"),
        ({"model": ""}, "Anthropic（Claude）要填型号"),
        ({"api_base": "https://api.anthropic.com"}, "Anthropic（Claude）不用填地址"),
        ({"key": {"source": "env", "env": "LITELLM_MASTER_KEY"}}, "环境变量名要以 _API_KEY"),
        ({"key": {"source": "env", "env": "AEP_DATABRICKS_API_KEY"}}, "不能以 LITELLM_ 或 AEP_"),
        ({"key": {"source": "env", "env": "anthropic_api_key"}}, "环境变量名"),
        ({"key": {"source": "page", "value": "有中文"}}, "「key」的格式"),
        ({"key": {"source": "vault"}}, "「key」的格式"),
    ],
)
async def test_create_checks(change, detail):
    body = {k: v for k, v in (CLAUDE | change).items() if not (k == "key" and v is None)}
    if change == {"key": None}:
        body = CLAUDE | change
    async with Platform() as p:
        resp = await p.api.post("/models", json=body)
    assert resp.status_code in (409, 422)
    assert detail in resp.json()["detail"]
    assert p.called() == []


async def test_missing_key_and_provider_fields():
    no_key = {k: v for k, v in CLAUDE.items() if k != "key"}
    azure = CLAUDE | {"alias": "gpt-azure", "provider": "azure", "model": "gpt5-deploy"}
    async with Platform() as p:
        missing_key = await p.api.post("/models", json=no_key)
        missing_base = await p.api.post("/models", json=azure)
        missing_version = await p.api.post(
            "/models", json=azure | {"api_base": "https://x.openai.azure.com"}
        )
        bedrock_key = await p.api.post(
            "/models",
            json=CLAUDE | {"alias": "claude-br", "provider": "bedrock", "aws_region": "us-east-1"},
        )
        bedrock = await p.api.post(
            "/models",
            json={k: v for k, v in CLAUDE.items() if k != "key"}
            | {"alias": "claude-br", "provider": "bedrock", "aws_region": "us-east-1"},
        )
    assert missing_key.json()["detail"] == "Anthropic（Claude）要填key"
    assert missing_base.json()["detail"] == "Azure OpenAI要填地址"
    assert missing_version.json()["detail"] == "Azure OpenAI要填API 版本"
    assert "不用填 key" in bedrock_key.json()["detail"]
    assert bedrock.status_code == 201
    assert p.fake.by_alias("claude-br")["litellm_params"] == {
        "model": "bedrock/claude-sonnet-5-5",
        "aws_region_name": "us-east-1",
    }
    assert bedrock.json()["key"] == {"source": "none", "env": None}


@pytest.mark.parametrize(
    ("base", "detail"),
    [
        ("ftp://x.example.com", "地址格式不对"),
        ("https://user:pw@x.example.com/v1", "地址里不能带账号密码"),
        ("https://x.example.com/v1?api_key=1", "地址里不能带账号密码"),
        ("http://169.254.169.254/v1", "元数据地址"),
        ("http://127.0.0.1:11434/v1", "不能用本机地址"),
        ("http://litellm:4000/v1", "平台内部服务"),
        ("http://postgres:5432", "平台内部服务"),
    ],
)
async def test_model_addresses_are_checked(base, detail):
    body = CLAUDE | {"alias": "compat", "provider": "openai_compatible", "api_base": base}
    async with Platform() as p:
        resp = await p.api.post("/models", json=body)
    assert resp.status_code == 422 and detail in resp.json()["detail"]


async def test_env_key_is_a_reference():
    body = CLAUDE | {
        "alias": "compat",
        "provider": "openai_compatible",
        "api_base": " https://aigw.example.com/v1 ",
        "key": {"source": "env", "env": "COMPANY_API_KEY"},
    }
    async with Platform() as p:
        resp = await p.api.post("/models", json=body)
    assert resp.status_code == 201
    assert resp.json()["key"] == {"source": "env", "env": "COMPANY_API_KEY"}
    assert p.fake.by_alias("compat")["litellm_params"] == {
        "model": "openai/claude-sonnet-5-5",
        "api_base": "https://aigw.example.com/v1",
        "api_key": "os.environ/COMPANY_API_KEY",
    }


async def test_update_names_without_testing_and_settings_with_a_test():
    async with Platform() as p:
        await p.api.post("/models", json=CLAUDE)
        renamed = await p.api.patch("/models/claude-sonnet", json={"display_name": "Claude 主力"})
        calls = len(p.called())
        p.gateway.failing["claude-sonnet"] = (404, "model: claude-x not_found_error")
        bad = await p.api.patch("/models/claude-sonnet", json={"model": "claude-x"})
        after_bad = p.fake.by_alias("claude-sonnet")["litellm_params"]["model"]
        del p.gateway.failing["claude-sonnet"]
        good = await p.api.patch("/models/claude-sonnet", json={"model": "claude-opus-5-5"})
        fixed = await p.api.patch("/models/claude-sonnet", json={"provider": "openai"})
        config = await p.api.patch("/models/company", json={"display_name": "x"})
        missing = await p.api.patch("/models/nope", json={"display_name": "x"})

    assert renamed.status_code == 200 and renamed.json()["display_name"] == "Claude 主力"
    assert calls == 1  # only the create's test
    assert (
        bad.status_code == 422 and bad.json()["detail"] == "测试没有通过：" + PROBLEMS["not_found"]
    )
    assert after_bad == "anthropic/claude-sonnet-5-5"
    assert good.status_code == 200 and good.json()["model"] == "claude-opus-5-5"
    assert fixed.status_code == 422 and fixed.json()["detail"] == "供应商登记后不能修改"
    assert config.status_code == 409 and "只能在配置文件里修改" in config.json()["detail"]
    assert missing.status_code == 404 and missing.json()["detail"] == "没有找到这个模型"


async def test_a_new_key_is_tried_on_a_trial_model_first():
    async with Platform() as p:
        await p.api.post("/models", json=CLAUDE)
        p.gateway.failing.clear()

        trial_aliases: list[str] = []
        original = p.fake._handle

        def watch(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/model/new":
                body = json.loads(request.content)
                trial_aliases.append(body["model_name"])
                if body["litellm_params"].get("api_key") == "sk-bad":
                    p.gateway.failing[body["model_name"]] = (401, "invalid x-api-key")
            return original(request)

        p.fake._handle = watch  # type: ignore[method-assign]
        p.models.admin = p.fake.admin(p.settings)
        bad = await p.api.patch(
            "/models/claude-sonnet", json={"key": {"source": "page", "value": "sk-bad"}}
        )
        kept = p.fake.secrets["aep-claude-sonnet"]["api_key"]
        good = await p.api.patch(
            "/models/claude-sonnet",
            json={"key": {"source": "env", "env": "ANTHROPIC_API_KEY"}, "display_name": "C"},
        )

    assert bad.status_code == 422 and PROBLEMS["auth"] in bad.json()["detail"]
    assert kept == "sk-ant-secret-123"
    assert all(a.startswith("aep-trial-") for a in trial_aliases) and len(trial_aliases) == 2
    assert [d["model_name"] for d in p.fake.deployments if "trial" in d["model_name"]] == []
    assert good.status_code == 200
    assert good.json()["key"] == {"source": "env", "env": "ANTHROPIC_API_KEY"}
    assert good.json()["display_name"] == "C"
    assert p.fake.secrets["aep-claude-sonnet"]["api_key"] == "os.environ/ANTHROPIC_API_KEY"


async def test_defaults_usage_and_delete():
    async with Platform() as p:
        await p.api.post("/models", json=CLAUDE)
        wrong = await p.api.put("/models/defaults", json={"chat": "company-embed"})
        unknown = await p.api.put("/models/defaults", json={"embedding": "nope"})
        chosen = await p.api.put("/models/defaults", json={"chat": "claude-sonnet"})
        service_default = p.service.default_model
        stored = await p.store.get("default_models")
        default_delete = await p.api.delete("/models/claude-sonnet")
        usage = (await p.api.get("/models/claude-sonnet/usage")).json()
        await p.api.put("/models/defaults", json={"chat": "mock"})

        # An app whose current version names the model keeps it from being deleted.
        app = (await p.api.get("/apps")).status_code
        config = json.loads(json.dumps(HELLO.config))
        draft = next(n for n in config["graph"]["nodes"] if n["id"] == "draft")
        draft["model"] = "claude-sonnet"
        await p.service.ensure_versions("hello")
        await p.service.versions.save_draft("hello", config, None)
        await p.service.versions.publish("hello", "用 Claude", None)
        used = await p.models.usage("claude-sonnet")
        in_use = await p.api.delete("/models/claude-sonnet")

    assert wrong.status_code == 422 and wrong.json()["detail"] == "「company-embed」不是对话模型"
    assert unknown.status_code == 422
    assert chosen.json() == {"chat": "claude-sonnet", "embedding": "mock-embed"}
    assert service_default == "claude-sonnet"
    assert stored == {"chat": "claude-sonnet", "embedding": "mock-embed"}
    assert (
        default_delete.status_code == 409
        and "平台默认的对话模型" in default_delete.json()["detail"]
    )
    # While it was the default, apps whose nodes name no model used it.
    assert {u["app"]: u["via"] for u in usage["apps"]}.get("hello") == "default"
    assert app == 200
    assert used == [
        {
            "app": "hello",
            "title": used[0]["title"],
            "version": 2,
            "nodes": [draft["title"]],
            "via": "literal",
        }
    ]
    assert in_use.status_code == 409
    assert in_use.json()["detail"].startswith("这些应用正在用「claude-sonnet」：")


async def test_delete_page_model_and_refuse_config_models():
    async with Platform() as p:
        await p.api.post("/models", json=CLAUDE)
        config = await p.api.delete("/models/company")
        gone = await p.api.delete("/models/claude-sonnet")
        again = await p.api.delete("/models/claude-sonnet")
    assert config.status_code == 409
    assert gone.status_code == 204 and p.fake.by_alias("claude-sonnet") is None
    assert again.status_code == 404


async def test_saved_defaults_are_loaded_at_start():
    async with Platform() as p:
        await p.store.set("default_models", {"chat": "company", "embedding": "company-embed"})
        await p.models.setup()
        assert p.service.default_model == "company"
        listing = (await p.api.get("/models")).json()
        options = (await p.api.get("/knowledge-bases/options")).json()
    assert model(listing, "company")["is_default"]
    assert listing["models"][0]["alias"] == "company"
    assert options["default_embedding_model"] == "company-embed"


async def test_embedding_models_on_the_page():
    body = {
        "alias": "openai-embed",
        "display_name": "OpenAI 向量",
        "purpose": "embedding",
        "provider": "openai",
        "model": "text-embedding-3-large",
        "key": {"source": "env", "env": "OPENAI_API_KEY"},
    }
    async with Platform() as p:
        added = await p.api.post("/models", json=body)
        options = (await p.api.get("/knowledge-bases/options")).json()
        p.gateway.embedding_dimensions = 768
        dims = await p.api.post("/models/openai-embed/test")
        rerank = await p.models._test("company-rerank", "rerank")

    assert added.status_code == 201, added.text
    assert p.fake.by_alias("openai-embed")["model_info"]["mode"] == "embedding"
    assert options["embedding_models"] == ["mock-embed", "company-embed", "openai-embed"]
    assert dims.json()["ok"] is False
    assert dims.json()["problem"]["message"] == PROBLEMS["dimensions"].format(n=768)
    assert rerank["problem"]["code"] == "unsupported"


async def test_app_options_name_models_by_their_display_names():
    async with Platform() as p:
        await p.api.post("/models", json=CLAUDE)
        detail = (await p.api.get("/apps/hello")).json()
    choices = {c["alias"]: c for c in detail["options"]["model_choices"]}
    assert choices["claude-sonnet"] == {
        "alias": "claude-sonnet",
        "label": "Claude Sonnet 5.5",
        "purpose": "chat",
        "allowed": True,
    }
    assert choices["company-embed"]["purpose"] == "embedding"


async def test_policy_marks_models_runs_may_not_use():
    async with Platform(allowed_models="company") as p:
        listing = (await p.api.get("/models")).json()
    assert model(listing, "company")["allowed"] and not model(listing, "mock")["allowed"]


async def test_test_endpoint_reports_latency_and_reply():
    async with Platform() as p:
        ok = (await p.api.post("/models/company/test")).json()
        p.gateway.failing["company"] = (429, "Rate limit reached")
        limited = (await p.api.post("/models/company/test")).json()
        missing = await p.api.post("/models/nope/test")
    assert ok["ok"] and ok["reply"] == "你好" and isinstance(ok["latency_ms"], int)
    assert limited["problem"] == {"code": "quota", "message": PROBLEMS["quota"]}
    assert missing.status_code == 404


# Call records (aep.llm)


async def test_calls_are_recorded_with_cost_and_errors():
    gateway = PickyGateway()
    gateway.cost = "0.0042"
    store = MemoryCallStore()
    llm.set_recorder(store.record)
    try:
        await llm.chat(gateway.client, [{"role": "user", "content": "hi"}], "company")
        await llm.embed(gateway.client, ["a", "b"], "company-embed")
        gateway.failing["company"] = (401, "authentication_error")
        with pytest.raises(openai.AuthenticationError):
            await llm.chat(gateway.client, [{"role": "user", "content": "hi"}], "company")
    finally:
        llm.set_recorder(None)
    records = [c for _, c in store.calls]
    assert [(r.model, r.kind, r.ok, r.error) for r in records] == [
        ("company", "chat", True, None),
        ("company-embed", "embedding", True, None),
        ("company", "chat", False, "auth"),
    ]
    assert records[0].cost_usd == pytest.approx(0.0042)
    assert (records[0].prompt_tokens, records[0].completion_tokens) == (3, 2)
    assert records[0].run_id is None
    summary = await store.summary(datetime.now(UTC) - timedelta(days=1))
    assert summary["company"].calls == 2 and summary["company"].last_error == "auth"
    assert summary["company-embed"] == UsageSummary(
        calls=1,
        prompt_tokens=3,
        cost_usd=pytest.approx(0.0042),
        last_call_at=summary["company-embed"].last_call_at,
    )


async def test_a_broken_recorder_does_not_break_the_call():
    async def broken(record):
        raise RuntimeError("db down")

    gateway = FakeGateway()
    llm.set_recorder(broken)
    try:
        result = await llm.chat(gateway.client, [{"role": "user", "content": "hi"}], "mock")
    finally:
        llm.set_recorder(None)
    assert result.text == "你好"


@pytest.mark.parametrize(
    ("status", "message", "code"),
    [
        (401, "invalid x-api-key", "auth"),
        (403, "forbidden", "auth"),
        (429, "slow down", "quota"),
        (400, "Your credit balance is too low", "quota"),
        (404, "no such model", "not_found"),
        (400, "Invalid model name passed in model=x", "not_found"),
        (408, "Request timed out", "timeout"),
        (500, "OpenAIException - Connection error.", "unreachable"),
        (400, "max_tokens: must be positive", "bad_request"),
        (500, "boom", "gateway"),
    ],
)
async def test_error_codes(status, message, code):
    gateway = PickyGateway()
    gateway.failing["m"] = (status, message)
    client = gateway.client.with_options(max_retries=0)
    with pytest.raises(openai.APIStatusError) as caught:
        await llm.chat(client, [{"role": "user", "content": "hi"}], "m")
    assert llm.error_code(caught.value) == code


async def test_old_records_are_pruned_at_start():
    async with Platform() as p:
        old = datetime.now(UTC) - timedelta(days=200)
        p.calls.calls.append(
            (old, llm.CallRecord("mock", "chat", None, None, 1, 1, None, True, None, 1))
        )
        await p.models.setup()
    assert p.calls.calls == []
