"""One sequential processing worker per API process, backed by database claims."""

import asyncio
import logging
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from openai import AsyncOpenAI

from aep import llm
from aep.knowledge.chunk import chunk
from aep.knowledge.extract import extract
from aep.knowledge.keywords import chunk_keywords
from aep.knowledge.store import Chunk, Document, KnowledgeStore

log = logging.getLogger(__name__)


class KnowledgeWorker:
    def __init__(
        self,
        store: KnowledgeStore,
        client: AsyncOpenAI,
        retry_delays: tuple[float, float] = (2, 4),
    ) -> None:
        self.store = store
        # The worker owns the two retries, rather than multiplying SDK retries.
        self.client = client.with_options(max_retries=0)
        self.retry_delays = retry_delays
        self.wakeup = asyncio.Event()

    async def recover(self) -> int:
        return await self.store.recover_stale(datetime.now(UTC) - timedelta(minutes=10))

    async def _recover_loop(self) -> None:
        while True:
            try:
                if await self.recover():
                    self.wakeup.set()
            except Exception:
                log.exception("could not recover stale knowledge documents")
            try:
                while await self.store.backfill_keywords(limit=500) == 500:
                    pass
            except Exception:
                log.exception("could not backfill knowledge keywords")
            await asyncio.sleep(60)

    async def run(self) -> None:
        # Recovery continues even while a long document is being processed.
        recovery = asyncio.create_task(self._recover_loop())
        try:
            while True:
                self.wakeup.clear()
                try:
                    if await self.process_once():
                        continue
                except Exception:
                    log.exception("knowledge processing iteration failed")
                with suppress(TimeoutError):
                    await asyncio.wait_for(self.wakeup.wait(), timeout=2)
        finally:
            recovery.cancel()
            with suppress(asyncio.CancelledError):
                await recovery

    async def process_once(self) -> bool:
        """Claim and process one document; also used directly by tests without polling."""
        document = await self.store.claim()
        if document is None:
            return False
        await self._process(document)
        return True

    async def _process(self, document: Document) -> None:
        try:
            base = await self.store.get(document.kb_id)
            if base is None:
                return
            paragraphs = await asyncio.to_thread(extract, Path(document.path), document.kind)
            pieces = chunk(paragraphs, document.chunk_size, document.chunk_overlap)
            chunks: list[Chunk] = []
            for start in range(0, len(pieces), llm.EMBEDDING_BATCH_SIZE):
                batch = pieces[start : start + llm.EMBEDDING_BATCH_SIZE]
                keywords = await asyncio.to_thread(
                    lambda batch=batch: [chunk_keywords(p.text, p.heading) for p in batch]
                )
                for attempt in range(3):
                    try:
                        vectors = await llm.embed(
                            self.client, [p.text for p in batch], base.embedding_model
                        )
                        break
                    except Exception:
                        if attempt == 2:
                            raise
                        await asyncio.sleep(self.retry_delays[attempt])
                current = await self.store.touch(document)
                if current is None:
                    return  # Deleted, reindexed, or reclaimed by another worker.
                document = current
                chunks.extend(
                    Chunk(
                        chunk_id=str(uuid4()),
                        document_id=document.document_id,
                        kb_id=document.kb_id,
                        position=start + i,
                        text=piece.text,
                        heading=piece.heading,
                        page=piece.page,
                        embedding=vector,
                        keywords=keywords[i],
                    )
                    for i, (piece, vector) in enumerate(zip(batch, vectors, strict=True))
                )
            await self.store.replace_chunks(document, chunks)
        except Exception as exc:
            log.exception("knowledge document %s failed", document.document_id)
            await self.store.fail(document, str(exc))
