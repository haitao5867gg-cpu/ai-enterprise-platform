"""Skills and app versions on real Postgres, and a run keeping its version across a restart.

Runs when AEP_TEST_DATABASE_URL points at a disposable database (CI sets it).
"""

import os
import uuid
from contextlib import asynccontextmanager

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from aep.runtime.registry import AppContext, default_apps
from aep.runtime.service import RunService
from aep.runtime.store import PostgresRunStore
from aep.runtime.versions import NoDraft, PostgresAppVersionStore, VersionNotFound
from aep.skills import NoDraft as NoSkillDraft
from aep.skills import PostgresSkillStore, SkillContent, SkillExists
from tests.conftest import SETTINGS, FakeGateway

DATABASE_URL = os.environ.get("AEP_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="AEP_TEST_DATABASE_URL not set")


@asynccontextmanager
async def database():
    pool = AsyncConnectionPool(
        DATABASE_URL,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open(wait=True)
    try:
        yield pool
    finally:
        await pool.close()


async def test_skill_versions():
    name = f"terms-{uuid.uuid4().hex[:8]}"
    async with database() as pool:
        store = PostgresSkillStore(pool)
        await store.setup()
        await store.register_builtins()
        assert await store.register_builtins() == 0
        assert (await store.version("etl-plan", 1)).note == "平台内置"

        await store.create(name, SkillContent("术语", "GMV 是成交总额。", {"a.md": "参考"}))
        with pytest.raises(SkillExists):
            await store.create(name, SkillContent("术语", "重名"))
        assert (await store.get(name)).published_version is None
        first = await store.publish(name, "第一版", "张三")
        assert (first.published_version, first.draft) == (1, None)
        await store.save_draft(name, SkillContent("术语", "改过"))
        second = await store.publish(name, "第二版", None)
        assert [v.version for v in second.versions] == [2, 1]
        assert (await store.version(name, 1)).references == {"a.md": "参考"}
        with pytest.raises(NoSkillDraft):
            await store.publish(name, "没有草稿", None)
        assert name in [s.name for s in await store.all()]


async def test_app_versions():
    app = f"app-{uuid.uuid4().hex[:8]}"
    async with database() as pool:
        store = PostgresAppVersionStore(pool)
        await store.setup()
        await store.ensure(app, {"nodes": {}})
        await store.ensure(app, {"nodes": {"ignored": {}}})
        assert await store.current(app) == 1

        draft = await store.save_draft(app, {"nodes": {"a": {"model": "qwen"}}}, "张三")
        assert await store.set_draft_eval(app, draft.revision, {"status": "passed"})
        resaved = await store.save_draft(app, {"nodes": {"a": {"model": "deepseek"}}}, None)
        assert (await store.draft(app)).eval is None
        assert not await store.set_draft_eval(app, draft.revision, {"status": "passed"})
        assert await store.set_draft_eval(app, resaved.revision, {"status": "passed"})

        published = await store.publish(app, "换模型", "张三")
        assert (published.version, published.eval) == (2, {"status": "passed"})
        assert await store.current(app) == 2
        assert await store.draft(app) is None
        with pytest.raises(NoDraft):
            await store.publish(app, "再来", None)
        with pytest.raises(NoDraft):
            await store.discard_draft(app)

        await store.activate(app, 1)
        assert await store.current(app) == 1
        with pytest.raises(VersionNotFound):
            await store.activate(app, 9)
        assert [v.version for v in await store.versions(app)] == [2, 1]


@asynccontextmanager
async def platform(gateway: FakeGateway):
    async with database() as pool:
        checkpointer = AsyncPostgresSaver(pool)
        await checkpointer.setup()
        store = PostgresRunStore(pool)
        await store.setup()
        skills = PostgresSkillStore(pool)
        await skills.setup()
        await skills.register_builtins()
        versions = PostgresAppVersionStore(pool)
        await versions.setup()
        yield RunService(
            store,
            checkpointer,
            default_apps(),
            AppContext(llm_client=gateway.client, settings=SETTINGS),
            versions=versions,
            skills=skills,
        )


async def test_a_waiting_run_keeps_its_version_across_a_restart():
    gateway = FakeGateway(replies=["第一版", "第二版"])
    async with platform(gateway) as service:
        await service.ensure_versions("hello")
        before = await service.versions.current("hello")
        config = {"nodes": {"draft": {"model": "qwen", "skills": [], "params": {}}}}
        await service.versions.save_draft("hello", config, None)
        published = await service.versions.publish("hello", "换模型", None)
        record = await service.create("hello", {"text": "你好"})
        await service.wait_idle()
        assert record.app_version == published.version
        # Someone rolls back while the run waits.
        await service.versions.activate("hello", before)

    async with platform(gateway) as restarted:
        assert (await restarted.get(record.run_id)).record.app_version == published.version
        await restarted.resume(record.run_id, "reject", "再短一点")
        await restarted.wait_idle()
    assert [r["body"]["model"] for r in gateway.requests] == ["qwen", "qwen"]
