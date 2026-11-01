"""The project store on real Postgres, and runs keeping their project snapshot.

Runs when AEP_TEST_DATABASE_URL points at a disposable database (CI sets it).
"""

import os
import uuid

import pytest
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from aep.projects import DuplicateName, PostgresProjectStore, ProjectBody, ProjectExists
from aep.runtime.store import PostgresRunStore
from tests.test_projects import project

DATABASE_URL = os.environ.get("AEP_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="AEP_TEST_DATABASE_URL not set")


async def test_project_store_and_run_snapshots():
    pool = AsyncConnectionPool(
        DATABASE_URL,
        open=False,
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
    )
    await pool.open(wait=True)
    try:
        store = PostgresProjectStore(pool)
        await store.setup()
        suffix = uuid.uuid4().hex[:8]
        body = ProjectBody.model_validate(
            project(project_id=f"p-{suffix}", name=f"项目 {suffix}")
        ).body()
        made = await store.create(body)
        assert made.body == body
        with pytest.raises(ProjectExists):
            await store.create(body)
        with pytest.raises(DuplicateName):
            await store.create(body | {"project_id": f"q-{suffix}"})

        changed = await store.replace(body | {"description": "改过"})
        assert changed is not None and changed.body["description"] == "改过"
        assert changed.created_at == made.created_at and changed.updated_at >= made.updated_at
        assert (await store.get(body["project_id"])).body["description"] == "改过"
        assert body["project_id"] in [p.project_id for p in await store.all()]
        assert await store.replace(body | {"project_id": "nope-x"}) is None

        runs = PostgresRunStore(pool)
        await runs.setup()
        snapshot = {"project_id": body["project_id"], "name": body["name"]}
        record = await runs.create("hello", {}, project=snapshot)
        assert (await runs.get(record.run_id)).project == snapshot

        assert await store.delete(body["project_id"])
        assert not await store.delete(body["project_id"])
        assert await store.get(body["project_id"]) is None
    finally:
        await pool.close()
