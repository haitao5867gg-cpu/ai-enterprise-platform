"""The asset catalog (docs/api/catalog.md): every asset in one list with its owner, tags,
users and quality; proposing one to the assets repository through the Git connector."""

from pathlib import Path
from typing import Any

import httpx
import yaml
from langgraph.checkpoint.memory import InMemorySaver

from aep import eval_sets
from aep.asset_catalog import AssetCatalog, MemoryAssetMetaStore
from aep.datasources import MemoryDataSourceStore
from aep.knowledge.store import Document, MemoryKnowledgeStore
from aep.main import app
from aep.runtime.catalog import AppCatalog
from aep.runtime.registry import AppContext, default_apps
from aep.runtime.service import RunService
from aep.runtime.store import MemoryRunStore
from aep.skills import MemorySkillStore, SkillContent
from aep.tools.mcp import McpTools
from tests import fake_connector, fake_git
from tests.conftest import FakeGateway
from tests.test_flow_apps import end, flow


class Catalog:
    def __init__(self, tmp: Path, replies: list[str] | None = None, repo: bool = True) -> None:
        self.git = fake_git.FakeGit()
        settings = self.git.settings() if repo else self.git.settings(assets_git_url="")
        servers = {fake_connector.URL: fake_connector.build(), fake_git.URL: self.git.server}
        self.gateway = FakeGateway(replies or [])
        self.sources = MemoryDataSourceStore()
        self.skills = MemorySkillStore()
        self.knowledge = MemoryKnowledgeStore()
        self.store = MemoryRunStore()
        tools = McpTools(5, resolve=lambda url: servers[url])
        self.service = RunService(
            self.store,
            InMemorySaver(),
            default_apps(),
            AppContext(
                self.gateway.client,
                settings,
                datasources=self.sources,
                tools=tools,
                knowledge=self.knowledge,
            ),
            skills=self.skills,
        )
        self.apps = AppCatalog(self.service, self.sources, settings, models=self.models)
        self.meta = MemoryAssetMetaStore()
        repo_client = self.git.repo() if repo else None
        self.catalog = AssetCatalog(self.apps, self.sources, self.meta, repo_client, settings)
        self.tmp = tmp

    async def models(self) -> list[str]:
        return ["mock"]

    async def __aenter__(self) -> "Catalog":
        shop = await self.sources.create("示例库", "postgres", fake_connector.URL)
        self.shop = shop.datasource_id
        content = SkillContent("说话的语气", "要客气。", {"example.md": "您好"})
        await self.skills.create("tone", content)
        await self.skills.publish("tone", "第一版", "李四")
        kb = await self.knowledge.create("退货政策", "售后口径")
        self.kb = kb.kb_id
        text = self.tmp / "policy.md"
        text.write_text("# 退货\n\n7 天无理由。\n", encoding="utf-8")
        await self.knowledge.add_documents(
            self.kb,
            [
                Document("d1", self.kb, "policy.md", "md", 10, str(text), status="ready"),
                Document("d2", self.kb, "scan.pdf", "pdf", 10, str(self.tmp / "x.pdf"), "error"),
            ],
        )
        app.state.run_service = self.service
        app.state.catalog = self.apps
        app.state.skills = self.skills
        app.state.datasources = self.sources
        app.state.knowledge = self.knowledge
        app.state.asset_catalog = self.catalog
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        )
        self.api = await self._client.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self._client.__aexit__(*exc)

    async def publish(self, name: str, config: dict, **create: Any) -> None:
        if (await self.api.get(f"/apps/{name}")).status_code == 404:
            made = await self.api.post("/apps", json={"app": name, "title": name, **create})
            assert made.status_code == 201, made.text
        resp = await self.api.put(f"/apps/{name}/draft", json={"config": config})
        assert resp.status_code == 200, resp.text
        resp = await self.api.post(f"/apps/{name}/draft/publish", json={"note": "改"})
        assert resp.status_code == 200, resp.text

    async def items(self, **params: str) -> list[dict]:
        resp = await self.api.get("/catalog", params=params)
        assert resp.status_code == 200, resp.text
        return resp.json()["items"]

    async def one(self, kind: str, asset_id: str) -> dict:
        resp = await self.api.get(f"/catalog/{kind}/{asset_id}")
        assert resp.status_code == 200, resp.text
        return resp.json()


LOOKUP = flow(
    {"id": "ask", "type": "llm", "title": "回答", "prompt": "{{start.question}}"},
    {
        "id": "end",
        "type": "end",
        "title": "结束",
        "outputs": [{"key": "reply", "from": ["{{ask.text}}"]}],
    },
    edges=[{"from": "start", "to": "ask"}, {"from": "ask", "to": "end"}],
)


def worker(shop: str, kb: str) -> dict:
    nodes = [
        {
            "id": "say",
            "type": "llm",
            "title": "写回复",
            "prompt": "{{start.question}}",
            "skills": [{"name": "tone", "version": 1}],
        },
        {
            "id": "look",
            "type": "knowledge",
            "title": "检索政策",
            "knowledge_bases": [kb],
            "query": "{{start.question}}",
        },
        {
            "id": "tables",
            "type": "tool",
            "title": "看表",
            "datasource": shop,
            "tool": "list_tables",
            "args": {"schema": "public"},
        },
        {
            "id": "call",
            "type": "app",
            "title": "问一下",
            "app": "lookup",
            "input": {"question": "{{start.question}}"},
            "outputs": ["reply"],
        },
    ]
    order = ["start", "say", "look", "tables", "call", "end"]
    edges = [{"from": a, "to": b} for a, b in zip(order, order[1:], strict=False)]
    return flow(*nodes, end("{{call.reply}}"), edges=edges)


def by(items: list[dict], kind: str, asset_id: str) -> dict:
    return next(item for item in items if item["kind"] == kind and item["id"] == asset_id)


async def test_every_asset_with_its_users_and_quality(tmp_path):
    async with Catalog(tmp_path, replies=["好的"] * 20) as c:
        await c.publish("lookup", LOOKUP)
        await c.publish("worker", worker(c.shop, c.kb))
        made = await c.api.post(
            "/apps",
            json={"app": "qa", "title": "问答", "from": {"template": "qa-approval"}},
        )
        assert made.status_code == 201, made.text
        for _ in range(3):
            run = await c.api.post("/runs", json={"app": "lookup", "input": {"question": "x"}})
            assert run.status_code == 201, run.text
        await c.service.wait_idle()

        items = await c.items()
        kinds = [item["kind"] for item in items]
        assert kinds == sorted(
            kinds, key=["template", "app", "skill", "knowledge", "tool", "eval_set"].index
        )

        template = by(items, "template", "qa-approval")
        assert [u["app"] for u in template["used_by"]] == ["qa"]
        assert template["used_by"][0]["via"] == "从模板新建"
        assert template["owner"] == "平台内置" and not template["can_propose"]

        lookup = by(items, "app", "lookup")
        assert lookup["used_by"] == [
            {"app": "worker", "title": "worker", "version": 2, "via": "问一下"}
        ]
        assert lookup["version"] == 2 and lookup["link"] == "/apps/lookup"
        assert lookup["quality"] == {
            "score": 1.0,
            "basis": "近 30 天运行成功率",
            "detail": "成功 3 次，失败 0 次",
        }
        assert lookup["facts"] == {"kind": "flow", "runs_30d": 3, "success_rate": 1.0}
        assert by(items, "app", "worker")["quality"]["score"] is None
        assert by(items, "app", "etl")["can_propose"] is False

        tone = by(items, "skill", "tone")
        assert tone["used_by"] == [
            {"app": "worker", "title": "worker", "version": 2, "via": "写回复"}
        ]
        assert tone["owner"] == "李四" and tone["version"] == 1
        assert tone["quality"]["score"] is None

        kb = by(items, "knowledge", c.kb)
        assert [u["via"] for u in kb["used_by"]] == ["检索政策"]
        assert kb["quality"] == {
            "score": 0.5,
            "basis": "文档处理成功率",
            "detail": "2 个文档，1 个处理失败",
        }
        assert kb["facts"]["documents"] == 2

        shop = by(items, "tool", c.shop)
        assert [(u["app"], u["via"]) for u in shop["used_by"]] == [("worker", "看表")]
        assert shop["facts"] == {"type": "postgres"}
        assert not shop["can_propose"]

        assert [i["id"] for i in await c.items(kind="skill")] == [
            s.name for s in await c.skills.all() if s.name in {i["id"] for i in items}
        ]
        assert [i["id"] for i in await c.items(q="退货")] == [c.kb]
        assert (await c.api.get("/catalog", params={"kind": "nope"})).status_code == 422
        assert (await c.api.get("/catalog/skill/nope")).status_code == 404
        assert (await c.api.get("/catalog/nope/x")).status_code == 404


async def test_owner_and_tags_are_kept_until_the_asset_goes(tmp_path):
    async with Catalog(tmp_path) as c:
        await c.publish("lookup", LOOKUP)
        resp = await c.api.put(
            "/catalog/app/lookup",
            json={"owner": " 王五 ", "tags": ["客服", "客服", " 退换组 "], "actor": "张三"},
        )
        assert resp.status_code == 200, resp.text
        item = resp.json()
        assert item["owner"] == "王五" and item["owner_set"]
        assert item["tags"] == ["客服", "退换组"]
        assert [i["id"] for i in await c.items(q="退换组")] == ["lookup"]
        assert [i["id"] for i in await c.items(q="王五")] == ["lookup"]

        bad = await c.api.put("/catalog/app/lookup", json={"tags": ["长" * 21]})
        assert bad.status_code == 422
        bad = await c.api.put("/catalog/app/lookup", json={"tags": [" "]})
        assert bad.status_code == 422 and bad.json()["detail"] == "每个标签 1–20 个字符"
        missing = await c.api.put("/catalog/app/nope", json={"owner": "x"})
        assert missing.status_code == 404

        back = await c.api.put("/catalog/app/lookup", json={"owner": None, "tags": []})
        assert back.json()["owner"] is None and not back.json()["owner_set"]

        await c.api.put("/catalog/app/lookup", json={"owner": "王五"})
        assert (await c.api.delete("/apps/lookup")).status_code == 204
        await c.items()
        assert c.meta.items == {}


async def test_proposing_assets_to_the_repository(tmp_path):
    async with Catalog(tmp_path) as c:
        await c.publish("lookup", LOOKUP)
        await c.apps.eval_sets.put(
            eval_sets.EvalSet(
                "lookup", 0.8, cases=[{"id": "c1", "input": {"question": "x"}, "checks": []}]
            )
        )

        made = await c.api.post(
            "/catalog/app/lookup/pull-request", json={"note": "第一次入库", "actor": "张三"}
        )
        assert made.status_code == 201, made.text
        pr = made.json()
        assert pr["created"] and pr["branch"] == "aep/catalog/app/lookup"
        assert sorted(pr["changed"]) == ["apps/lookup/app.yaml", "apps/lookup/eval-set.yaml"]
        tool, args = c.git.calls[-1]
        assert tool == "open_pull_request"
        assert args["title"] == "资产：应用「lookup」" and args["replace_directory"]
        assert "第一次入库" in args["body"] and "提交的人：张三" in args["body"]
        files = c.git.files(pr["branch"])
        assert yaml.safe_load(files["apps/lookup/app.yaml"])["exported_from"] == {
            "app": "lookup",
            "version": 2,
        }
        assert yaml.safe_load(files["apps/lookup/eval-set.yaml"])["threshold"] == 0.8
        repo = (await c.one("app", "lookup"))["repo"]
        assert repo["number"] == pr["number"] and repo["by"] == "张三"

        again = (await c.api.post("/catalog/app/lookup/pull-request")).json()
        assert again["number"] == pr["number"] and not again["created"]

        skill = (await c.api.post("/catalog/skill/tone/pull-request")).json()
        files = c.git.files(skill["branch"])
        head, body = files["skills/tone/SKILL.md"].split("---\n")[1:]
        assert yaml.safe_load(head) == {"name": "tone", "description": "说话的语气", "version": 1}
        assert body.strip() == "要客气。"
        assert files["skills/tone/references/example.md"] == "您好"

        kb = (await c.api.post(f"/catalog/knowledge/{c.kb}/pull-request")).json()
        files = c.git.files(kb["branch"])
        assert files[f"knowledge/{c.kb}/docs/policy.md"].startswith("# 退货")
        readme = files[f"knowledge/{c.kb}/README.md"]
        assert "| scan.pdf | pdf | error | 只列在清单里 | v1 |  | 长期有效 |" in readme

        evals = (await c.api.post("/catalog/eval_set/lookup/pull-request")).json()
        assert list(c.git.files(evals["branch"])).count("evals/lookup/eval-set.yaml") == 1

        for path, said in [
            (f"/catalog/tool/{c.shop}/pull-request", "数据源和工具不提交到资产仓库"),
            ("/catalog/template/qa-approval/pull-request", "应用模板不提交到资产仓库"),
            (
                "/catalog/app/etl/pull-request",
                "「自然语言 ETL 工作流」是代码应用，不提交到资产仓库",
            ),
        ]:
            refused = await c.api.post(path)
            assert refused.status_code == 409, path
            assert refused.json()["detail"] == said

        c.git.fail("open_pull_request", "GitHub 拒绝了访问（403），请检查令牌的权限：nope")
        failed = await c.api.post("/catalog/skill/tone/pull-request")
        assert failed.status_code == 502
        assert failed.json()["detail"] == "GitHub 拒绝了访问（403），请检查令牌的权限：nope"


async def test_without_a_repository(tmp_path):
    async with Catalog(tmp_path, repo=False) as c:
        listed = (await c.api.get("/catalog")).json()
        assert listed["repo_ready"] is False
        resp = await c.api.post("/catalog/skill/tone/pull-request")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "没配资产仓库（AEP_ASSETS_REPO、AEP_ASSETS_GIT_URL）"
        await c.skills.create("draft-only", SkillContent("还没发布", "正文"))
        resp = await c.api.post("/catalog/skill/draft-only/pull-request")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "「draft-only」还没有发布的版本"
