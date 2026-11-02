"""Eval sets, run feedback, a run's model calls and rerunning from a step on real
Postgres. Runs when AEP_TEST_DATABASE_URL is set (CI sets it)."""

import os
import uuid
from datetime import UTC, datetime

import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from aep import feedback
from aep.eval_sets import EvalSet, PostgresEvalSetStore
from aep.llm import CallRecord
from aep.models import PostgresCallStore
from aep.runtime.flow.store import PostgresFlowAppStore
from aep.runtime.registry import AppContext, default_apps
from aep.runtime.service import RunService
from aep.runtime.store import PostgresRunStore
from aep.runtime.versions import PostgresAppVersionStore
from tests.conftest import SETTINGS, FakeGateway

DATABASE_URL = os.environ.get("AEP_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="AEP_TEST_DATABASE_URL not set")


@pytest.fixture
async def pool():
    pool = AsyncConnectionPool(
        DATABASE_URL,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open(wait=True)
    yield pool
    await pool.close()


async def test_eval_sets_are_kept(pool):
    store = PostgresEvalSetStore(pool)
    await store.setup()
    await store.setup()  # idempotent
    app = f"a-{uuid.uuid4().hex[:8]}"
    assert await store.get(app) is None
    case = {"id": "c1", "title": "一", "input": {"q": "退货"}, "answers": {}, "expect": []}
    at = datetime.now(UTC)
    await store.put(EvalSet(app, 0.8, "qwen", [case], "王搭建", at))
    found = await store.get(app)
    assert (found.app, found.threshold, found.judge_model, found.cases, found.updated_by) == (
        app,
        0.8,
        "qwen",
        [case],
        "王搭建",
    )
    assert found.updated_at == at
    await store.put(EvalSet(app, 0.5, None, [], None))
    found = await store.get(app)
    assert (found.threshold, found.judge_model, found.cases) == (0.5, None, [])
    await store.delete(app)
    assert await store.get(app) is None


async def test_feedback_is_one_per_person(pool):
    store = feedback.PostgresFeedbackStore(pool)
    await store.setup()
    await store.setup()
    run = str(uuid.uuid4())
    await store.put(feedback.Feedback(run, "u1", "good", "对"))
    await store.put(feedback.Feedback(run, "u2", "bad"))
    await store.put(feedback.Feedback(run, "u1", "bad", "其实不对"))
    summary = feedback.summary(await store.for_run(run), "u1")
    assert (summary["good"], summary["bad"]) == (0, 2)
    assert summary["mine"]["note"] == "其实不对"
    await store.delete(run, "u1")
    assert feedback.summary(await store.for_run(run), "u1")["mine"] is None
    assert await store.for_run(str(uuid.uuid4())) == []


async def test_a_runs_calls_in_order(pool):
    store = PostgresCallStore(pool)
    await store.setup()
    run = str(uuid.uuid4())
    await store.record(CallRecord("mock", "chat", run, "draft", 10, 2, 0.01, True, None, 120))
    await store.record(CallRecord("mock", "chat", run, "route", 5, 1, None, True, None, 80))
    found = await store.for_run(run)
    assert [(c.node, c.prompt_tokens, c.cost_usd) for _, c in found] == [
        ("draft", 10, 0.01),
        ("route", 5, None),
    ]
    assert found[0][0] <= found[1][0]


async def test_rerun_from_a_step_is_kept(pool):
    checkpointer = AsyncPostgresSaver(pool)
    await checkpointer.setup()
    store = PostgresRunStore(pool)
    await store.setup()
    versions = PostgresAppVersionStore(pool)
    await versions.setup()
    flows = PostgresFlowAppStore(pool)
    await flows.setup()
    gateway = FakeGateway(["第一版", "第二版"])
    service = RunService(
        store,
        checkpointer,
        default_apps(),
        AppContext(llm_client=gateway.client, settings=SETTINGS),
        versions=versions,
        flows=flows,
    )
    source = await service.create("hello", {"text": "你好"})
    await service.wait_idle()
    await service.resume(source.run_id, "approve", None)
    await service.wait_idle()

    again = await service.rerun_from(source.run_id, "draft", use_draft=False)
    await service.wait_idle()
    view = await service.get(again.run_id)
    assert view.record.start_at == {"node": "draft", "title": "回答", "run_id": source.run_id}
    assert view.record.rerun_of == source.run_id
    assert view.pending["details"]["show"] == "第二版"
