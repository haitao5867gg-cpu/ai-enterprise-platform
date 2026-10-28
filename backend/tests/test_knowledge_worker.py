import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from aep.knowledge.store import Chunk, Document, MemoryKnowledgeStore
from aep.knowledge.worker import KnowledgeWorker


async def queued_document(store, tmp_path, text="文档内容" * 600):
    base = await store.create(
        "测试", chunk_size=200, chunk_overlap=0, embedding_model="company-embed"
    )
    path = tmp_path / "doc.txt"
    path.write_text(text, encoding="utf-8")
    [document] = await store.add_documents(
        base.kb_id,
        [Document(str(uuid4()), base.kb_id, path.name, "txt", path.stat().st_size, str(path))],
    )
    return document


async def test_worker_refreshes_after_every_batch_and_retries_twice(gateway, tmp_path, monkeypatch):
    store = MemoryKnowledgeStore()
    document = await queued_document(store, tmp_path)
    worker = KnowledgeWorker(store, gateway.client)
    gateway.embedding_statuses = [500, 500, 200, 200]
    original_touch = store.touch
    timestamps = []

    async def touch(claim):
        current = await original_touch(claim)
        timestamps.append(current.updated_at)
        return current

    delays = AsyncMock()
    monkeypatch.setattr(store, "touch", touch)
    monkeypatch.setattr("aep.knowledge.worker.asyncio.sleep", delays)
    assert await worker.process_once()
    ready = await store.get_document(document.kb_id, document.document_id)
    assert ready.status == "ready" and ready.chunk_count == 12
    assert [call.args[0] for call in delays.await_args_list] == [2, 4]
    assert len(timestamps) == 2 and timestamps[1] > timestamps[0] > document.updated_at


async def test_periodic_recovery_catches_a_document_not_stale_at_startup(
    gateway, tmp_path, monkeypatch
):
    store = MemoryKnowledgeStore()
    document = await queued_document(store, tmp_path)
    claim = await store.claim()
    worker = KnowledgeWorker(store, gateway.client)
    assert await worker.recover() == 0
    ticks = 0

    async def advance_time(seconds):
        nonlocal ticks
        assert seconds == 60
        ticks += 1
        if ticks == 2:
            raise asyncio.CancelledError
        store._documents[document.document_id] = replace(
            claim, updated_at=datetime.now(UTC) - timedelta(minutes=11)
        )

    monkeypatch.setattr("aep.knowledge.worker.asyncio.sleep", advance_time)
    with pytest.raises(asyncio.CancelledError):
        await worker._recover_loop()
    assert (await store.get_document(document.kb_id, document.document_id)).status == "queued"
    assert worker.wakeup.is_set()


async def test_stale_claims_cannot_write_after_reindex_or_deletion(gateway, tmp_path):
    store = MemoryKnowledgeStore()
    document = await queued_document(store, tmp_path)
    old = await store.claim()
    chunks = [
        Chunk(
            str(uuid4()), document.document_id, document.kb_id, 0, "旧片段", embedding=[1.0] * 1024
        )
    ]
    await store.requeue(document.kb_id, document.document_id)
    new = await store.claim()
    assert await store.touch(old) is None
    await store.replace_chunks(old, chunks)
    await store.fail(old, "旧任务失败")
    assert await store.get_document(document.kb_id, document.document_id) == new
    assert await store.chunks(document.document_id, 0, 50) == (0, [])
    await store.delete_document(document.kb_id, document.document_id)
    await store.replace_chunks(new, chunks)
    await store.fail(new, "已删除")
    assert await store.get_document(document.kb_id, document.document_id) is None


async def test_worker_run_recovers_waits_for_upload_event_and_cancels(
    gateway, tmp_path, monkeypatch
):
    store = MemoryKnowledgeStore()
    worker = KnowledgeWorker(store, gateway.client)
    idle = asyncio.Event()
    completed = asyncio.Event()
    recovered = asyncio.Event()
    original_once = worker.process_once
    original_recover = worker.recover

    async def once():
        processed = await original_once()
        (completed if processed else idle).set()
        return processed

    async def recover():
        result = await original_recover()
        recovered.set()
        return result

    monkeypatch.setattr(worker, "process_once", once)
    monkeypatch.setattr(worker, "recover", recover)
    task = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(idle.wait(), 1)
        await asyncio.wait_for(recovered.wait(), 1)
        document = await queued_document(store, tmp_path)
        worker.wakeup.set()
        await asyncio.wait_for(completed.wait(), 1)
        assert (await store.get_document(document.kb_id, document.document_id)).status == "ready"
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_loop_survives_store_error(gateway, monkeypatch):
    worker = KnowledgeWorker(MemoryKnowledgeStore(), gateway.client)
    failed = asyncio.Event()

    async def fail():
        failed.set()
        raise RuntimeError("temporary database outage")

    monkeypatch.setattr(worker, "process_once", fail)
    task = asyncio.create_task(worker.run())
    try:
        await asyncio.wait_for(failed.wait(), 1)
        assert not task.done()
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
