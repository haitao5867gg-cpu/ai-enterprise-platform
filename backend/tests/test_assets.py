"""Bundles go to the assets repository as pull requests, and the asset index records them."""

import httpx
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from aep.assets import AssetRepoError, MemoryAssetStore
from aep.datasources import MemoryDataSourceStore
from aep.main import app
from aep.runtime.apps.etl import _shorten
from aep.runtime.registry import AppContext, default_apps
from aep.runtime.service import RunService
from aep.runtime.store import MemoryRunStore
from aep.tools.mcp import McpTools
from tests import fake_connector, fake_databricks, fake_github
from tests.conftest import FakeGateway
from tests.test_databricks_job import PASSED
from tests.test_etl import PYSPARK_BODY, decide, plan, reply, start
from tests.test_release import PROD, as_prod_job

FILES = {"databricks.yml": "bundle: {}\n", "src/pipeline.py": "x = 1\n", "README.md": "# a\n"}


async def test_opens_a_pr_with_the_bundle_under_its_directory():
    github = fake_github.FakeGitHub()
    repo = github.repo()

    first = await repo.open_pr("pipelines/shop", FILES, "aep/shop/v1", "shop v1", "说明", "msg")

    assert (first.number, first.branch) == (1, "aep/shop/v1")
    assert first.url.endswith("/pull/1")
    files = github.files("aep/shop/v1")
    assert files["README.md"] == "# assets\n"  # the rest of the repository is kept
    assert files["pipelines/shop/src/pipeline.py"] == "x = 1\n"
    assert github.pulls[1]["base"] == "main" and github.pulls[1]["body"] == "说明"

    # The next version replaces the directory: a file it no longer has goes away.
    github.merge(1)
    smaller = {k: v for k, v in FILES.items() if k != "README.md"}
    second = await repo.open_pr("pipelines/shop", smaller, "aep/shop/v2", "shop v2", "第二版", "m")
    assert second.number == 2
    assert "pipelines/shop/README.md" not in github.files("aep/shop/v2")
    assert "pipelines/shop/src/pipeline.py" in github.files("aep/shop/v2")


async def test_a_retried_step_reuses_its_branch_and_pr():
    github = fake_github.FakeGitHub()
    repo = github.repo()
    await repo.open_pr("pipelines/shop", FILES, "aep/shop/v1", "shop v1", "第一次", "m")
    changed = FILES | {"src/pipeline.py": "x = 2\n"}

    again = await repo.open_pr("pipelines/shop", changed, "aep/shop/v1", "shop v1", "重试", "m")

    assert again.number == 1 and len(github.pulls) == 1
    assert github.pulls[1]["body"] == "重试"
    assert github.files("aep/shop/v1")["pipelines/shop/src/pipeline.py"] == "x = 2\n"


async def test_errors_say_what_github_answered():
    empty = fake_github.FakeGitHub(empty=True)
    with pytest.raises(AssetRepoError, match="读不到分支 main（空仓库请先建一个带 README"):
        await empty.repo().open_pr("pipelines/shop", FILES, "b", "t", "", "m")

    github = fake_github.FakeGitHub()
    github.fail("/pulls", 403, "Resource not accessible by personal access token")
    with pytest.raises(AssetRepoError, match="403.*Resource not accessible"):
        await github.repo().open_pr("pipelines/shop", FILES, "b", "t", "", "m")


async def test_versions_are_reserved_once_per_run_and_revision():
    store = MemoryAssetStore()

    assert await store.reserve("shop", "run-a", "r1") == 1
    assert await store.reserve("shop", "run-b", "r1") == 2  # another run: the next version
    assert await store.reserve("shop", "run-a", "r1") == 1  # the same step again: the same
    assert await store.reserve("shop", "run-a", "r2") == 3  # new code: a new version
    assert await store.reserve("other", "run-a", "r1") == 1
    assert [(a.version, a.status) for a in await store.find("shop")] == [
        (3, "pending"),
        (2, "pending"),
        (1, "pending"),
    ]


def services(workspace, github, assets=None, runs=1, **settings):
    gateway = FakeGateway(
        [
            reply(plan(assumptions=["数量为负的订单视为无效"])),
            reply({"targets": {"order_facts": PYSPARK_BODY}}),
        ]
        * runs
    )
    store, sources = MemoryRunStore(), MemoryDataSourceStore()
    assets = assets or MemoryAssetStore()
    connector = fake_connector.build()
    values = {"databricks_cluster_id": fake_databricks.CLUSTER_ID} | PROD | settings
    ctx = AppContext(
        gateway.client,
        workspace.settings(**values),
        datasources=sources,
        tools=McpTools(5, resolve=lambda url: connector),
        databricks=as_prod_job(workspace),
        assets=github.repo() if github is not None else None,
        asset_store=assets,
    )
    return RunService(store, InMemorySaver(), default_apps(), ctx), sources, assets


async def test_each_passing_version_becomes_a_pr_with_its_approval_trail(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    workspace.job_outputs = [PASSED, PASSED]
    github = fake_github.FakeGitHub()
    service, sources, assets = services(workspace, github)
    shop = await sources.create("示例库", "postgres", fake_connector.URL)

    run_id = await start(service, shop.datasource_id, name="shop_orders")
    await service.resume(run_id, "approve", None, actor="张三")
    await service.wait_idle()
    view = await service.get(run_id)

    assert view.record.status == "waiting", view.record.error
    assert view.pending["details"]["type"] == "etl_release"
    asset = view.result["asset"]
    assert asset == {
        "status": "proposed",
        "repo": fake_github.REPO,
        "path": "pipelines/shop_orders",
        "version": 1,
        "pr_number": 1,
        "pr_url": f"https://github.test/{fake_github.REPO}/pull/1",
        "branch": f"aep/shop_orders/v1-{run_id[:8]}",
        "commit": github.refs[f"aep/shop_orders/v1-{run_id[:8]}"],
    }
    pr = github.pulls[1]
    assert pr["title"].startswith("shop_orders v1：把订单和客户整理成订单明细宽表")
    body = pr["body"]
    assert "## 需求\n\n做一张订单明细宽表" in body
    assert "- 数量为负的订单视为无效" in body  # the assumptions the user confirmed
    assert "状态：passed，方式：job，第 1 次" in body
    assert "| 方案确认 | 通过 | 张三 |" in body
    assert f"- 平台运行：http://localhost:3000/runs/{run_id}" in body
    files = github.files(f"aep/shop_orders/v1-{run_id[:8]}")
    for path, content in view.result["files"].items():
        assert files[f"pipelines/shop_orders/{path}"] == content
    assert "已在资产仓库 acme/aep-assets 开 PR #1" in view.messages[-1]["content"]

    # Turned down: the PR says so, and the rewritten code that passes is version 2.
    view = await decide(service, run_id, "reject", "城市统一成大写")
    assert view.record.status == "waiting", view.record.error
    (note,) = github.comments[1]
    assert note.startswith("**上线审批：驳回（未署名，") and "意见：城市统一成大写" in note
    assert view.result["asset"]["version"] == 2 and view.result["asset"]["pr_number"] == 2
    assert "| 上线审批 | 驳回 |" in github.pulls[2]["body"]

    await service.resume(run_id, "approve", "可以上线", actor="李四")
    await service.wait_idle()
    view = await service.get(run_id)

    assert view.record.status == "done", view.record.error
    assert view.result["release"]["status"] == "deployed"
    assert view.result["asset"]["status"] == "released"
    (note,) = github.comments[2]
    assert note.startswith("**上线审批：通过（李四，") and "意见：可以上线" in note
    assert "已上线到 prod：shop_orders 部署为 Job 101" in note
    found = [(a.version, a.status, a.pr_number, a.run_id) for a in await assets.find("shop_orders")]
    assert found == [(2, "released", 2, run_id), (1, "rejected", 1, run_id)]

    # GET /assets finds them by name.
    app.state.asset_store = assets
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        listed = (await client.get("/assets", params={"name": "shop_orders"})).json()
        other = (await client.get("/assets", params={"name": "nothing"})).json()
    assert [(a["version"], a["status"], a["pr_number"]) for a in listed] == [
        (2, "released", 2),
        (1, "rejected", 1),
    ]
    assert listed[0]["commit"] == github.refs[f"aep/shop_orders/v2-{run_id[:8]}"]
    assert other == []


class Crash(BaseException):
    """The process going away: not an error the run records, so the run stays running."""


class CrashOnFirstRecord(MemoryAssetStore):
    def __init__(self) -> None:
        super().__init__()
        self.crashed = False

    async def record(self, name, version, published):
        if not self.crashed:
            self.crashed = True
            raise Crash
        return await super().record(name, version, published)


async def test_a_publish_cut_short_reuses_its_version_and_pr_when_recovered(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    workspace.job_outputs = [PASSED]
    github = fake_github.FakeGitHub()
    service, sources, assets = services(workspace, github, CrashOnFirstRecord())
    shop = await sources.create("示例库", "postgres", fake_connector.URL)

    # The PR is open but the step never finished: the run is left running.
    run_id = await start(service, shop.datasource_id, name="shop_orders")
    await service.resume(run_id, "approve", None)
    await service.wait_idle()
    assert (await service.get(run_id)).record.status == "running"
    assert len(github.pulls) == 1

    assert await service.recover() == 1
    await service.wait_idle()
    view = await service.get(run_id)

    assert view.record.status == "waiting", view.record.error
    assert view.result["asset"]["version"] == 1 and view.result["asset"]["pr_number"] == 1
    assert len(github.pulls) == 1  # the same PR, not a second one
    found = [(a.version, a.status, a.pr_number) for a in await assets.find()]
    assert found == [(1, "proposed", 1)]


async def test_without_an_assets_repository_the_run_says_so(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    workspace.job_outputs = [PASSED]
    service, sources, _ = services(workspace, None, databricks_prod_catalog="")
    shop = await sources.create("示例库", "postgres", fake_connector.URL)

    run_id = await start(service, shop.datasource_id, name="shop_orders")
    view = await decide(service, run_id, "approve")

    assert view.record.status == "done", view.record.error
    assert view.result["asset"]["status"] == "skipped"
    assert "AEP_ASSETS_REPO" in view.result["asset"]["reason"]


async def test_a_trial_run_does_not_publish(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    workspace.job_outputs = [PASSED]
    github = fake_github.FakeGitHub()
    service, sources, _ = services(workspace, github)
    shop = await sources.create("示例库", "postgres", fake_connector.URL)

    run_id = await start(service, shop.datasource_id, name="shop_orders", release=False)
    view = await decide(service, run_id, "approve")

    assert view.record.status == "done", view.record.error
    assert view.result["asset"] == {"status": "skipped", "reason": "这次运行只试跑，不提交资产"}
    assert github.pulls == {}


async def test_a_failed_publish_is_reported_and_the_release_still_waits(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    workspace.job_outputs = [PASSED]
    github = fake_github.FakeGitHub()
    github.fail("/git/trees", 403, "Resource not accessible by personal access token")
    service, sources, assets = services(workspace, github)
    shop = await sources.create("示例库", "postgres", fake_connector.URL)

    run_id = await start(service, shop.datasource_id, name="shop_orders")
    view = await decide(service, run_id, "approve")

    assert view.record.status == "waiting", view.record.error
    asset = view.result["asset"]
    assert asset["status"] == "failed" and "403" in asset["errors"][0]
    assert "提交到资产仓库 acme/aep-assets 失败" in view.messages[-1]["content"]
    ((version, status, pr),) = [(a.version, a.status, a.pr_number) for a in await assets.find()]
    assert (version, status, pr) == (1, "pending", None)  # reserved, no PR

    view = await decide(service, run_id, "approve")
    assert view.record.status == "done", view.record.error
    assert view.result["release"]["status"] == "deployed"
    assert github.comments == {}  # no PR to note the decision on


async def test_a_failed_pr_comment_keeps_the_release_outcome(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    workspace.job_outputs = [PASSED]
    github = fake_github.FakeGitHub()
    service, sources, assets = services(workspace, github)
    shop = await sources.create("示例库", "postgres", fake_connector.URL)
    run_id = await start(service, shop.datasource_id, name="shop_orders")
    await decide(service, run_id, "approve")

    github.fail("/comments", 502, "Bad Gateway")
    view = await decide(service, run_id, "approve")

    assert view.record.status == "done", view.record.error
    assert view.result["release"]["status"] == "deployed"
    asset = view.result["asset"]
    assert asset["status"] == "released" and "502" in asset["errors"][0]
    assert [a.status for a in await assets.find()] == ["released"]


async def test_unnamed_runs_for_the_same_table_are_versions_of_one_asset(tmp_path):
    workspace = fake_databricks.FakeWorkspace(tmp_path)
    workspace.job_outputs = [PASSED, PASSED]
    github = fake_github.FakeGitHub()
    service, sources, assets = services(workspace, github, runs=2)
    shop = await sources.create("示例库", "postgres", fake_connector.URL)

    for _ in range(2):
        run_id = await start(service, shop.datasource_id)
        view = await decide(service, run_id, "approve")
        assert view.record.status == "waiting", view.record.error

    assert view.result["bundle"] == "order_facts"  # named after its target table
    assert view.result["asset"]["path"] == "pipelines/order_facts"
    assert [a.version for a in await assets.find("order_facts")] == [2, 1]
    assert github.pulls[2]["title"].startswith("order_facts v2：")


def test_titles_are_cut_at_punctuation():
    assert _shorten("短的。", 60) == "短的。"
    long = (
        "从 orders 关联 customers 和 products，筛选已支付订单，清洗城市和状态，过滤脏数据并按天汇总"
    )
    assert _shorten(long, 45) == "从 orders 关联 customers 和 products，筛选已支付订单…"
    assert _shorten("a" * 50, 40) == "a" * 40 + "…"
