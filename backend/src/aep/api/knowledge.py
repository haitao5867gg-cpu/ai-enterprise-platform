"""Knowledge base management, original documents and retrieval tests."""

import asyncio
import shutil
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from time import perf_counter
from typing import Annotated, Any
from urllib.parse import quote

import openai
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse
from pydantic import (
    BaseModel,
    Field,
    StringConstraints,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from aep.api.apps import Catalog
from aep.api.validation import NoNulls, chinese_errors
from aep.config import Settings, get_settings
from aep.knowledge.rerank import RerankError
from aep.knowledge.retrieval import Retrieval, defaults
from aep.knowledge.search import search
from aep.knowledge.store import (
    MAX_DOCUMENTS,
    Chunk,
    Document,
    DocumentLimit,
    DuplicateName,
    KnowledgeBase,
    KnowledgeStore,
    MissingBase,
    ModelInUse,
)
from aep.knowledge.uploads import (
    MAX_FILES,
    UploadError,
    remove_files,
    save_files,
    validate_files,
)

router = APIRouter(prefix="/knowledge-bases", tags=["knowledge"], route_class=chinese_errors())


def get_store(request: Request) -> KnowledgeStore:
    return request.app.state.knowledge


Store = Annotated[KnowledgeStore, Depends(get_store)]
Config = Annotated[Settings, Depends(get_settings)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=50)]


class Chunking(NoNulls):
    size: int = Field(800, ge=200, le=2000)
    overlap: int = Field(100, ge=0)

    @model_validator(mode="after")
    def valid_overlap(self) -> "Chunking":
        if self.overlap > self.size // 2:
            raise ValueError("重叠字符数不能超过片段大小的一半")
        return self


class CreateBase(NoNulls):
    name: Name
    description: str = Field("", max_length=500)
    embedding_model: str | None = None
    chunking: Chunking = Field(default_factory=Chunking)


class UpdateBase(NoNulls):
    name: Name | None = None
    description: str | None = Field(None, max_length=500)
    embedding_model: str | None = None
    chunking: Chunking | None = None


class BaseOut(BaseModel):
    kb_id: str
    name: str
    description: str
    embedding_model: str
    chunking: Chunking
    document_count: int
    chunk_count: int
    status_counts: dict[str, int]
    created_at: datetime
    updated_at: datetime


class DocumentOut(BaseModel):
    document_id: str
    kb_id: str
    name: str
    kind: str
    size_bytes: int
    status: str
    error: str | None
    chunk_count: int
    created_at: datetime
    updated_at: datetime


class ChunkOut(BaseModel):
    chunk_id: str
    document_id: str
    position: int
    text: str
    heading: str
    page: int | None


class ChunkPage(BaseModel):
    total: int
    items: list[ChunkOut]


class SearchBody(NoNulls):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    top_k: int = Field(5, ge=1, le=20)
    min_score: float = Field(0.3, ge=0, le=1)
    retrieval: Retrieval = Field(default_factory=Retrieval)

    @field_validator("retrieval")
    @classmethod
    def valid_retrieval(cls, value: Retrieval, info: ValidationInfo) -> Retrieval:
        return Retrieval.model_validate(
            value.model_dump(), context={"top_k": info.data.get("top_k", 5)}
        )


def _base_out(base: KnowledgeBase) -> BaseOut:
    values = asdict(base)
    values["chunking"] = Chunking(size=base.chunk_size, overlap=base.chunk_overlap)
    return BaseOut(**values)


def _document_out(document: Document) -> DocumentOut:
    return DocumentOut(**asdict(document))


def _chunk_out(chunk: Chunk) -> ChunkOut:
    return ChunkOut(**{field: getattr(chunk, field) for field in ChunkOut.model_fields})


async def _base(store: KnowledgeStore, kb: str) -> KnowledgeBase:
    base = await store.get(kb)
    if base is None:
        raise HTTPException(404, "没有这个知识库")
    return base


async def _document(store: KnowledgeStore, kb: str, doc: str) -> Document:
    await _base(store, kb)
    document = await store.get_document(kb, doc)
    if document is None:
        raise HTTPException(404, "知识库里没有这篇文档")
    return document


def _model(model: str, settings: Settings) -> None:
    if model not in settings.embedding_models:
        raise HTTPException(422, f"没有可用的向量模型别名「{model}」")


def _wake(request: Request) -> None:
    request.app.state.knowledge_worker.wakeup.set()


@router.get("/options")
async def options(settings: Config, catalog: Catalog) -> dict[str, Any]:
    return {
        "embedding_models": settings.embedding_models,
        "default_embedding_model": settings.default_embedding_model,
        "chunking": Chunking().model_dump(),
        "retrieval": defaults(),
        "rerank_models": await catalog.rerank_models(),
    }


@router.get("")
async def list_bases(store: Store) -> list[BaseOut]:
    return [_base_out(base) for base in await store.all()]


@router.post("", status_code=201)
async def create_base(body: CreateBase, store: Store, settings: Config) -> BaseOut:
    model = (
        body.embedding_model
        if body.embedding_model is not None
        else settings.default_embedding_model
    )
    _model(model, settings)
    try:
        base = await store.create(
            body.name, body.description, model, body.chunking.size, body.chunking.overlap
        )
    except DuplicateName as exc:
        raise HTTPException(409, f"已存在名为「{body.name}」的知识库") from exc
    return _base_out(base)


@router.get("/{kb}")
async def get_base(kb: str, store: Store) -> BaseOut:
    return _base_out(await _base(store, kb))


@router.patch("/{kb}")
async def update_base(kb: str, body: UpdateBase, store: Store, settings: Config) -> BaseOut:
    await _base(store, kb)
    changes = body.model_dump(exclude_unset=True)
    if "embedding_model" in changes:
        _model(changes["embedding_model"], settings)
    if "chunking" in changes:
        changes.pop("chunking")
        chunking = body.chunking.model_dump()
        changes.update(chunk_size=chunking["size"], chunk_overlap=chunking["overlap"])
    try:
        base = await store.update(kb, **changes)
    except DuplicateName as exc:
        raise HTTPException(409, "已存在同名知识库") from exc
    except ModelInUse as exc:
        raise HTTPException(409, "知识库有文档后不能更改向量模型") from exc
    if base is None:
        raise HTTPException(404, "没有这个知识库")
    return _base_out(base)


@router.delete("/{kb}", status_code=204)
async def delete_base(kb: str, store: Store, settings: Config, catalog: Catalog) -> Response:
    base = await _base(store, kb)
    users = await catalog.knowledge_users(kb)
    if users:
        raise HTTPException(409, "知识库被这些应用引用：" + "、".join(users))
    await store.delete(kb)
    await asyncio.to_thread(shutil.rmtree, settings.knowledge_dir / base.kb_id, True)
    return Response(status_code=204)


@router.get("/{kb}/documents")
async def list_documents(kb: str, store: Store) -> list[DocumentOut]:
    await _base(store, kb)
    return [_document_out(document) for document in await store.documents(kb)]


@router.post("/{kb}/documents", status_code=202)
async def upload_documents(
    kb: str,
    store: Store,
    settings: Config,
    request: Request,
    files: Annotated[list[UploadFile] | None, File()] = None,
) -> list[DocumentOut]:
    base = await _base(store, kb)
    if not files:
        raise HTTPException(422, "请选择至少一个 PDF、DOCX、MD 或 TXT 文件")
    if len(files) > MAX_FILES:
        raise HTTPException(422, "单次最多上传 20 个文件")
    if base.document_count + len(files) > MAX_DOCUMENTS:
        raise HTTPException(422, "一个知识库最多 500 个文档")
    try:
        documents = await asyncio.to_thread(
            validate_files, files, settings.knowledge_dir, base.kb_id
        )
    except UploadError as exc:
        raise HTTPException(422, str(exc)) from exc
    registered = False
    try:
        await asyncio.to_thread(save_files, files, documents)
        added = await store.add_documents(base.kb_id, documents)
        registered = True
    except DocumentLimit as exc:
        raise HTTPException(422, str(exc)) from exc
    except MissingBase as exc:
        raise HTTPException(404, "没有这个知识库") from exc
    finally:
        if not registered:
            await asyncio.to_thread(remove_files, documents)
    _wake(request)
    return [_document_out(document) for document in added]


@router.get("/{kb}/documents/{doc}")
async def get_document(kb: str, doc: str, store: Store) -> DocumentOut:
    return _document_out(await _document(store, kb, doc))


@router.delete("/{kb}/documents/{doc}", status_code=204)
async def delete_document(kb: str, doc: str, store: Store) -> Response:
    document = await _document(store, kb, doc)
    await store.delete_document(kb, doc)
    await asyncio.to_thread(remove_files, [document])
    return Response(status_code=204)


@router.get("/{kb}/documents/{doc}/file")
async def download_document(kb: str, doc: str, store: Store) -> FileResponse:
    document = await _document(store, kb, doc)
    if not await asyncio.to_thread(Path(document.path).is_file):
        raise HTTPException(404, "原文件不存在")
    return FileResponse(
        document.path,
        headers={
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(document.name, safe='')}"
        },
    )


@router.get("/{kb}/documents/{doc}/chunks")
async def list_chunks(
    kb: str,
    doc: str,
    store: Store,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ChunkPage:
    await _document(store, kb, doc)
    total, items = await store.chunks(doc, offset, limit)
    return ChunkPage(total=total, items=[_chunk_out(item) for item in items])


@router.post("/{kb}/documents/{doc}/reindex", status_code=202)
async def reindex_document(kb: str, doc: str, store: Store, request: Request) -> DocumentOut:
    await _document(store, kb, doc)
    await store.requeue(kb, doc)
    _wake(request)
    return _document_out(await _document(store, kb, doc))


@router.post("/{kb}/reindex", status_code=202)
async def reindex_base(kb: str, store: Store, request: Request) -> dict[str, int]:
    await _base(store, kb)
    queued = await store.requeue(kb)
    _wake(request)
    return {"queued": queued}


@router.post("/{kb}/search")
async def search_base(
    kb: str, body: SearchBody, store: Store, request: Request, settings: Config, catalog: Catalog
) -> dict[str, Any]:
    if body.retrieval.rerank:
        try:
            Retrieval.model_validate(
                body.retrieval.model_dump(),
                context={
                    "top_k": body.top_k,
                    "rerank_models": await catalog.rerank_models(),
                },
            )
        except ValidationError as exc:
            raise HTTPException(422, str(exc.errors()[0].get("ctx", {}).get("error", exc))) from exc
    await _base(store, kb)
    started = perf_counter()
    try:
        hits = await search(
            store,
            request.app.state.llm_client,
            [kb],
            body.query,
            body.top_k,
            body.min_score,
            body.retrieval,
            settings=settings,
        )
    except MissingBase as exc:
        raise HTTPException(404, "没有这个知识库") from exc
    except RerankError as exc:
        raise HTTPException(502, str(exc)) from exc
    except (openai.OpenAIError, ValueError) as exc:
        raise HTTPException(502, f"向量模型调用失败：{exc}") from exc
    return {
        "hits": [asdict(hit) for hit in hits],
        "duration_ms": int((perf_counter() - started) * 1000),
    }
