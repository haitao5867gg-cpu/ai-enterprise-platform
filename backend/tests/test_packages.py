"""Requirements lists and signed packages for runtimes (docs/api/runtime.md, step 1)."""

import base64
import json
import os

import pytest
import yaml
from pydantic import ValidationError

from aep import __version__, packages
from aep.auth import LABELS, required
from aep.config import Settings, get_settings
from aep.main import app
from aep.packages import Packages, Signer
from aep.runtime.flow import files as flowfiles
from aep.skills import SkillContent
from tests.conftest import SETTINGS
from tests.test_flow_tools import Platform, order_flow

SEED = base64.b64encode(bytes(range(32))).decode()
OTHER_SEED = base64.b64encode(bytes(range(1, 33))).decode()
CODE = "def main(inputs):\n    return {'total': 1}\n"


class Packaging:
    """The tools-test platform with packages wired and a signing key (or none)."""

    def __init__(self, key: str = SEED, servers: dict | None = None) -> None:
        self.platform = Platform(["x"], servers)
        self.settings = SETTINGS.model_copy(update={"package_signing_key": key})

    async def __aenter__(self) -> Platform:
        p = await self.platform.__aenter__()
        app.state.packages = Packages(p.service, p.catalog, p.sources)
        app.dependency_overrides[get_settings] = lambda: self.settings
        return p

    async def __aexit__(self, *exc) -> None:
        app.dependency_overrides.pop(get_settings, None)
        await self.platform.__aexit__(*exc)


async def orders(p: Platform) -> None:
    """A flow with a tool, a model it names, one that names none, code and a skill."""
    await p.skills.create("polite", SkillContent("礼貌", "先称呼您。", {"cases.md": "例子"}))
    await p.skills.publish("polite", "第一版", None)
    config = order_flow(p.crm)
    config["graph"]["nodes"][2:2] = [
        {
            "id": "say",
            "type": "llm",
            "title": "说明",
            "model": "qwen",
            "skills": [{"name": "polite", "version": 1}],
            "prompt": "{{lookup.result}}",
        },
        {"id": "wrap", "type": "llm", "title": "收尾", "prompt": "{{say.text}}"},
        {
            "id": "calc",
            "type": "code",
            "title": "算一下",
            "code": CODE,
            "inputs": {"raw": "{{wrap.text}}"},
            "outputs": [{"key": "total", "type": "number"}],
            "timeout_seconds": 5,
        },
    ]
    config["graph"]["edges"] = [
        {"from": "start", "to": "lookup"},
        {"from": "lookup", "to": "say"},
        {"from": "say", "to": "wrap"},
        {"from": "wrap", "to": "calc"},
        {"from": "calc", "to": "end"},
    ]
    await p.new_app("orders", config)


# What the platform derives from a configuration


def test_tools_per_data_source_follow_constants_inputs_and_agents() -> None:
    config = {
        "env": {"vars": [{"key": "stock", "value": " ds-stock "}]},
        "graph": {
            "nodes": [
                {
                    "id": "start",
                    "type": "start",
                    "inputs": [{"key": "src", "type": "datasource", "default": "ds-input"}],
                },
                {"id": "a", "type": "tool", "datasource": "{{env.stock}}", "tool": "search"},
                {"id": "b", "type": "tool", "datasource": "ds-stock", "tool": "download"},
                {"id": "c", "type": "tool", "datasource": "{{start.src}}", "tool": "read"},
                {"id": "d", "type": "tool", "datasource": "ds-stock", "tool": "{{start.x}}"},
                {
                    "id": "e",
                    "type": "agent",
                    "tools": [
                        {"datasource": "ds-stock", "tools": ["search", "preview"]},
                        {"datasource": "ds-all", "tools": None},
                    ],
                },
                {"id": "f", "type": "etl_profile", "datasource": "ds-db"},
                {"id": "end", "type": "end"},
            ]
        },
    }
    assert flowfiles.datasource_tools(config) == {
        "ds-stock": ["search", "download", "preview"],
        "ds-all": [],
        "ds-db": [],
        "ds-input": ["read"],
    }


def test_models_in_first_use_order_and_the_default() -> None:
    def flow(*nodes: dict) -> dict:
        return {"graph": {"nodes": [{"id": "start", "type": "start"}, *nodes]}}

    assert flowfiles.model_refs(flow({"id": "t", "type": "template"})) == ([], False)
    assert flowfiles.model_refs(
        flow(
            {"id": "a", "type": "llm", "model": "vision"},
            {"id": "b", "type": "classifier", "model": "company"},
            {"id": "c", "type": "agent", "model": "vision"},
        )
    ) == (["vision", "company"], False)
    assert flowfiles.model_refs(
        flow({"id": "a", "type": "llm_review"}, {"id": "b", "type": "llm", "model": "qwen"})
    ) == (["qwen"], True)


def test_what_step_one_cannot_package_in_order() -> None:
    approve = {"type": "human"}
    form = {"type": "human", "mode": "form"}
    call = {"type": "app", "app": "x"}
    agent_call = {"type": "agent", "apps": [{"app": "x"}]}
    data = {"type": "databricks_release"}
    assert packages.unsupported([form, {"type": "agent", "apps": []}], [], ["mcp"]) is None
    assert packages.unsupported([data, call, approve], ["kb"], ["file"]) == packages.APPROVAL
    assert packages.unsupported([data, call], ["kb"], ["file"]) == packages.CALLS_APPS
    assert packages.unsupported([agent_call], [], []) == packages.CALLS_APPS
    assert packages.unsupported([data], ["kb"], ["file"]) == packages.KNOWLEDGE
    assert packages.unsupported([data], [], ["postgres", "file"]) == packages.FILE_SOURCE
    assert packages.unsupported([data, form], [], [None]) == packages.DATA_NODES
    for kind in packages.DATA_NODE_TYPES:
        assert packages.unsupported([{"type": kind}], [], []) == packages.DATA_NODES
    required = {"type": "start", "project": "required"}
    assert packages.unsupported([data, required], [], []) == packages.DATA_NODES
    assert packages.unsupported([required], [], []) == packages.NEEDS_PROJECT
    assert packages.unsupported([{"type": "start", "project": "optional"}], [], []) is None
    assert packages.unsupported([required], [], [], True) == packages.NEEDS_PROJECT
    assert packages.unsupported([form], [], [], True) == packages.OPEN_SOURCE


def test_sources_only_known_when_the_app_runs() -> None:
    def flow(datasource: str, inputs: list | None = None, consts: list | None = None) -> dict:
        return {
            "env": {"vars": consts or []},
            "graph": {
                "nodes": [
                    {"id": "start", "type": "start", "inputs": inputs or []},
                    {"id": "a", "type": "tool", "datasource": datasource, "tool": "t"},
                ]
            },
        }

    src = [{"key": "src", "type": "datasource"}]
    assert flowfiles.unresolved_datasources(flow("{{start.src}}", src))
    assert flowfiles.unresolved_datasources(flow("{{lookup.result}}"))
    assert flowfiles.unresolved_datasources(flow("{{env.stock}}"))
    assert not flowfiles.unresolved_datasources(flow("ds-1"))
    assert not flowfiles.unresolved_datasources(flow("{{start.src}}", [{**src[0], "default": "d"}]))
    assert not flowfiles.unresolved_datasources(
        flow("{{env.stock}}", consts=[{"key": "stock", "value": "ds-2"}])
    )
    agent = {"graph": {"nodes": [{"id": "x", "type": "agent", "tools": [{"datasource": ""}]}]}}
    assert not flowfiles.unresolved_datasources(agent)


# Signing


def test_a_signature_verifies_and_breaks_on_any_change() -> None:
    signer = Signer.from_text(SEED)
    assert signer is not None and Signer.from_text("  ") is None
    assert signer.key_id == packages.key_id(base64.b64decode(signer.public_key))
    assert len(signer.key_id) == 16
    signed = signer.sign(
        {"format": packages.FORMAT, "issuer": {"key_id": signer.key_id}, "title": "教练做视频"}
    )
    assert packages.verify(signed, [signer.public_key]) == signer.key_id
    # Key order and whitespace do not matter; the content does.
    shuffled = json.loads(json.dumps(dict(reversed(list(signed.items()))), indent=2))
    assert packages.verify(shuffled, [signer.public_key]) == signer.key_id
    other = Signer.from_text(OTHER_SEED)
    assert other is not None
    # With an old and a new key side by side, either one is accepted.
    assert packages.verify(signed, [other.public_key, signer.public_key]) == signer.key_id
    with pytest.raises(packages.BadSignature, match="不是用这台运行包认识的平台密钥签的"):
        packages.verify(signed, [other.public_key])
    for changed in (
        {**signed, "title": "教练做视频 "},
        {**signed, "extra": 1},
        {**signed, "signature": base64.b64encode(b"x" * 64).decode()},
        {**signed, "signature": "not base64!"},
    ):
        with pytest.raises(packages.BadSignature, match="签名不对"):
            packages.verify(changed, [signer.public_key])
    forged = other.sign({**signed, "issuer": {"key_id": signer.key_id}})
    with pytest.raises(packages.BadSignature, match="签名不对"):
        packages.verify(forged, [signer.public_key])


def test_a_bad_signing_key_stops_the_platform_at_startup() -> None:
    assert Settings(package_signing_key=SEED).package_signing_key == SEED
    for bad in ("not base64!", base64.b64encode(b"short").decode()):
        with pytest.raises(ValidationError, match="AEP_PACKAGE_SIGNING_KEY"):
            Settings(package_signing_key=bad)


def test_package_routes_need_a_builder() -> None:
    for path in (
        "/apps/{app}/runtime-requirements",
        "/apps/{app}/versions/{version}/requirements",
        "/apps/{app}/versions/{version}/package",
        "/packages/public-key",
    ):
        assert required("GET", path) == "builder"
    assert required("PUT", "/apps/{app}/runtime-requirements") == "builder"
    assert LABELS["PUT /apps/{app}/runtime-requirements"] == "修改运行要求"


# The API


async def test_publisher_fills_in_machine_network_and_notes():
    async with Packaging() as p:
        await orders(p)
        empty = await p.api.get("/apps/orders/runtime-requirements")
        assert empty.json() == {"machine": None, "network": [], "notes": ""}
        filled = {
            "machine": {"cpu_cores": 4, "memory_gb": 8, "disk_gb": 100},
            "network": ["api.pexels.com", " Pixabay.com ", "api.pexels.com"],
            "notes": "先装素材搜索连接器",
        }
        saved = await p.api.put("/apps/orders/runtime-requirements", json=filled)
        assert saved.status_code == 200, saved.text
        assert saved.json()["network"] == ["api.pexels.com", "pixabay.com"]
        assert (await p.api.get("/apps/orders/runtime-requirements")).json() == saved.json()
        # The whole thing is replaced: keys left out go back to empty.
        notes_only = await p.api.put("/apps/orders/runtime-requirements", json={"notes": "改了"})
        assert notes_only.json() == {"machine": None, "network": [], "notes": "改了"}

        for host in ("https://api.pexels.com", "api.pexels.com:443", "a/b", "", "a..b", "空"):
            bad = await p.api.put("/apps/orders/runtime-requirements", json={"network": [host]})
            assert bad.status_code == 422, host
            assert bad.json()["detail"] == "「网络」里要写主机名，比如 api.pexels.com"
        for body in (
            {"machine": {"cpu_cores": 0, "memory_gb": 8, "disk_gb": 100}},
            {"machine": {"cpu_cores": 4, "memory_gb": 8.5, "disk_gb": 100}},
            {"machine": {"cpu_cores": 4, "memory_gb": 8}},
            {"machine": {"cpu_cores": 4, "memory_gb": 8, "disk_gb": 100001}},
            {"network": [f"h{i}.example.com" for i in range(21)]},
            {"notes": "字" * 2001},
        ):
            bad = await p.api.put("/apps/orders/runtime-requirements", json=body)
            assert bad.status_code == 422, body
        assert (await p.api.get("/apps/orders/runtime-requirements")).json()["notes"] == "改了"
        missing = await p.api.get("/apps/nope/runtime-requirements")
        assert missing.status_code == 404
        assert (await p.api.put("/apps/nope/runtime-requirements", json={})).status_code == 404


async def test_requirements_list_of_a_version():
    async with Packaging() as p:
        await orders(p)
        await p.api.put(
            "/apps/orders/runtime-requirements",
            json={"network": ["api.pexels.com"], "notes": "说明"},
        )
        got = await p.api.get("/apps/orders/versions/2/requirements")
        assert got.status_code == 200, got.text
        assert got.json() == {
            "platform": {"min_version": __version__},
            "models": [{"alias": "qwen"}],
            "default_model": True,
            "datasources": [
                {"datasource_id": p.crm, "name": "CRM", "kind": "postgres", "tools": ["get_order"]}
            ],
            "sandbox": True,
            "skills": [{"name": "polite", "version": 1}],
            "secrets": ["crm_token"],
            "media_libraries": [],
            "machine": None,
            "network": ["api.pexels.com"],
            "notes": "说明",
            "unsupported": None,
        }
        # Version 1 is the blank app: nothing derived.
        blank = (await p.api.get("/apps/orders/versions/1/requirements")).json()
        assert (blank["models"], blank["datasources"], blank["sandbox"]) == ([], [], False)
        assert (await p.api.get("/apps/orders/versions/9/requirements")).status_code == 404
        assert (await p.api.get("/apps/nope/versions/1/requirements")).status_code == 404

        # What step 1 cannot package is still listed, with the reason.
        code = (await p.api.get("/apps/etl/versions/1/requirements")).json()
        assert code["unsupported"] == "代码应用暂时不能打成流程包"
        hello = (await p.api.get("/apps/hello/versions/1/requirements")).json()
        assert hello["unsupported"] == packages.APPROVAL


async def test_signed_package_of_a_version():
    async with Packaging() as p:
        await orders(p)
        await p.api.put(
            "/apps/orders/runtime-requirements",
            json={"machine": {"cpu_cores": 4, "memory_gb": 8, "disk_gb": 100}},
        )
        key = (await p.api.get("/packages/public-key")).json()
        signer = Signer.from_text(SEED)
        assert signer is not None
        assert key == {"key_id": signer.key_id, "public_key": signer.public_key}

        got = await p.api.get("/apps/orders/versions/2/package")
        assert got.status_code == 200, got.text
        assert got.headers["content-type"].startswith("application/json")
        assert (
            got.headers["content-disposition"]
            == "attachment; filename*=UTF-8''orders-v2.aep-package.json"
        )
        package = got.json()
        assert packages.verify(package, [key["public_key"]]) == key["key_id"]
        assert package["format"] == "aep-package/1"
        assert package["issuer"] == {"platform_version": __version__, "key_id": key["key_id"]}
        assert (package["app"], package["version"], package["kind"]) == ("orders", 2, "flow")
        assert package["title"] == "orders"
        assert package["skills"] == [
            {
                "name": "polite",
                "version": 1,
                "description": "礼貌",
                "body": "先称呼您。",
                "references": {"cases.md": "例子"},
            }
        ]
        requirements = (await p.api.get("/apps/orders/versions/2/requirements")).json()
        del requirements["unsupported"]
        assert package["requirements"] == requirements
        assert package["requirements"]["machine"] == {
            "cpu_cores": 4,
            "memory_gb": 8,
            "disk_gb": 100,
        }
        exported = (await p.api.get("/apps/orders/versions/2/export")).text
        assert package["app_file"] == exported
        assert yaml.safe_load(package["app_file"])["exported_from"] == {
            "app": "orders",
            "version": 2,
        }
        # Secrets travel by name only.
        await p.api.put("/apps/orders/secrets/crm_token", json={"value": "s3cret"})
        again = await p.api.get("/apps/orders/versions/2/package")
        assert "s3cret" not in again.text
        assert again.json()["package_id"] != package["package_id"]

        for path, detail in (
            ("/apps/etl/versions/1/package", "代码应用暂时不能打成流程包"),
            ("/apps/hello/versions/1/package", packages.APPROVAL),
        ):
            refused = await p.api.get(path)
            assert (refused.status_code, refused.json()["detail"]) == (422, detail)
        assert (await p.api.get("/apps/orders/versions/9/package")).status_code == 404
        assert (await p.api.get("/apps/nope/versions/1/package")).status_code == 404


async def test_a_package_over_5_mb_is_refused():
    async with Packaging() as p:
        await orders(p)
        big = SkillContent("很长", "长" * 19000, {f"r{i}.md": "字" * 19000 for i in range(10)})
        for n in range(10):
            await p.skills.create(f"long-{n}", big)
            await p.skills.publish(f"long-{n}", "第一版", None)
        config = order_flow(p.crm)
        config["graph"]["nodes"].insert(
            2,
            {
                "id": "say",
                "type": "llm",
                "title": "说明",
                "skills": [{"name": f"long-{n}", "version": 1} for n in range(10)],
                "prompt": "{{lookup.result}}",
            },
        )
        config["graph"]["edges"] = [
            {"from": "start", "to": "lookup"},
            {"from": "lookup", "to": "say"},
            {"from": "say", "to": "end"},
        ]
        await p.new_app("huge", config)
        got = await p.api.get("/apps/huge/versions/2/package")
        assert got.status_code == 422, got.status_code
        detail = got.json()["detail"]
        assert detail.startswith("流程包超过 5 MB（") and detail.endswith(
            "请减少 Skill 的内容或个数"
        )


async def test_no_signing_key_means_no_packages():
    async with Packaging(key="") as p:
        await orders(p)
        for path in ("/apps/orders/versions/2/package", "/packages/public-key"):
            got = await p.api.get(path)
            assert got.status_code == 503
            assert got.json()["detail"] == "平台没有配置流程包签名密钥（AEP_PACKAGE_SIGNING_KEY）"
        # The requirements list does not need the key.
        assert (await p.api.get("/apps/orders/versions/2/requirements")).status_code == 200


@pytest.mark.skipif(not os.environ.get("AEP_TEST_DATABASE_URL"), reason="needs Postgres")
async def test_runtime_needs_in_postgres():
    from psycopg.rows import dict_row
    from psycopg_pool import AsyncConnectionPool

    async with AsyncConnectionPool(
        os.environ["AEP_TEST_DATABASE_URL"],
        kwargs={"autocommit": True, "row_factory": dict_row},
        open=False,
    ) as pool:
        store = packages.PostgresRuntimeNeedsStore(pool)
        await store.setup()
        async with pool.connection() as conn:
            await conn.execute("DELETE FROM app_runtime_needs WHERE app = 'pkg-test'")
        assert await store.get("pkg-test") == packages.RuntimeNeeds()
        needs = packages.RuntimeNeeds({"cpu_cores": 2, "memory_gb": 4, "disk_gb": 50}, ["a.b"], "n")
        await store.set("pkg-test", needs)
        assert await store.get("pkg-test") == needs
        await store.set("pkg-test", packages.RuntimeNeeds())
        assert await store.get("pkg-test") == packages.RuntimeNeeds()
