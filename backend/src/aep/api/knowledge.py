"""Knowledge base management, original documents and retrieval tests."""

import asyncio
import shutil
from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Annotated, Any
from urllib.parse import quote

import openai
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
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
from aep.auth import who
from aep.config import Settings, get_settings
from aep.knowledge.governance import DOC_TEMPLATES, MAX_CASES, regression_out, run_regression
from aep.knowledge.rerank import RerankError
from aep.knowledge.retrieval import Retrieval, defaults
from aep.knowledge.search import search
from aep.knowledge.store import (
    MAX_DOCUMENTS,
    Chunk,
    Document,
    DocumentLimit,
    DuplicateName,
    HitStats,
    KnowledgeBase,
    KnowledgeStore,
    MissingBase,
    ModelInUse,
    ReplaceKind,
    today_in,
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
    validity_counts: dict[str, int]
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
    owner: str | None
    valid_from: date | None
    valid_until: date | None
    validity: str
    version: int
    uploaded_by: str | None
    hits_30d: int
    last_hit_at: datetime | None


class UpdateDocument(BaseModel):
    owner: Annotated[str, StringConstraints(strip_whitespace=True, max_length=50)] | None = None
    valid_from: date | None = None
    valid_until: date | None = None


class VersionOut(BaseModel):
    version: int
    size_bytes: int
    uploaded_by: str | None
    uploaded_at: datetime


CaseId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_-]{1,40}$")]
DocName = Annotated[str, StringConstraints(min_length=1, max_length=255)]


class Case(NoNulls):
    id: CaseId
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    expected: list[DocName] = Field(min_length=1, max_length=5)


class RegressionBody(NoNulls):
    cases: list[Case] = Field(max_length=MAX_CASES)
    retrieval: Retrieval = Field(default_factory=lambda: Retrieval(mode="hybrid"))
    top_k: int = Field(5, ge=1, le=20)
    min_score: float = Field(0.3, ge=0, le=1)
    actor: str | None = Field(None, max_length=50)

    @field_validator("retrieval")
    @classmethod
    def valid_retrieval(cls, value: Retrieval, info: ValidationInfo) -> Retrieval:
        return Retrieval.model_validate(
            value.model_dump(), context={"top_k": info.data.get("top_k", 5)}
        )


class RunRegression(NoNulls):
    actor: str | None = Field(None, max_length=50)


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
    include_inactive: bool = False

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


def _today(settings: Settings) -> date:
    return today_in(settings.schedule_timezone)


def _since() -> datetime:
    return datetime.now(UTC) - timedelta(days=30)


def _document_out(document: Document, today: date, stats: HitStats | None = None) -> DocumentOut:
    stats = stats or HitStats()
    return DocumentOut(
        **asdict(document),
        validity=document.validity(today),
        hits_30d=stats.hits_30d,
        last_hit_at=stats.last_hit_at,
    )


async def _documents_out(
    store: KnowledgeStore, kb: str, documents: list[Document], settings: Settings
) -> list[DocumentOut]:
    stats = await store.hit_stats(kb, _since())
    today = _today(settings)
    return [_document_out(d, today, stats.get(d.document_id)) for d in documents]


async def _rerank_allowed(retrieval: Retrieval, top_k: int, catalog: Catalog) -> None:
    if not retrieval.rerank:
        return
    try:
        Retrieval.model_validate(
            retrieval.model_dump(),
            context={"top_k": top_k, "rerank_models": await catalog.rerank_models()},
        )
    except ValidationError as exc:
        raise HTTPException(422, str(exc.errors()[0].get("ctx", {}).get("error", exc))) from exc


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


async def _embedding_models(request: Request, settings: Settings) -> list[str]:
    """The configured embedding models and those added on the model page."""
    models = getattr(request.app.state, "models", None)
    return await models.embedding_aliases() if models else list(settings.embedding_models)


def _default_embedding(request: Request, settings: Settings) -> str:
    models = getattr(request.app.state, "models", None)
    return models.defaults()["embedding"] if models else settings.default_embedding_model


async def _model(model: str, settings: Settings, request: Request) -> None:
    if model not in await _embedding_models(request, settings):
        raise HTTPException(422, f"没有可用的向量模型别名「{model}」")


def _wake(request: Request) -> None:
    request.app.state.knowledge_worker.wakeup.set()


@router.get("/options")
async def options(settings: Config, catalog: Catalog, request: Request) -> dict[str, Any]:
    return {
        "embedding_models": await _embedding_models(request, settings),
        "default_embedding_model": _default_embedding(request, settings),
        "chunking": Chunking().model_dump(),
        "retrieval": defaults(),
        "rerank_models": await catalog.rerank_models(),
    }


@router.get("/doc-templates")
async def doc_templates() -> list[dict[str, str]]:
    return DOC_TEMPLATES


@router.get("")
async def list_bases(store: Store) -> list[BaseOut]:
    return [_base_out(base) for base in await store.all()]


@router.post("", status_code=201)
async def create_base(
    body: CreateBase, store: Store, settings: Config, request: Request
) -> BaseOut:
    model = (
        body.embedding_model
        if body.embedding_model is not None
        else _default_embedding(request, settings)
    )
    await _model(model, settings, request)
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
async def update_base(
    kb: str, body: UpdateBase, store: Store, settings: Config, request: Request
) -> BaseOut:
    await _base(store, kb)
    changes = body.model_dump(exclude_unset=True)
    if "embedding_model" in changes:
        await _model(changes["embedding_model"], settings, request)
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
async def list_documents(kb: str, store: Store, settings: Config) -> list[DocumentOut]:
    await _base(store, kb)
    return await _documents_out(store, kb, await store.documents(kb), settings)


@router.post("/{kb}/documents", status_code=202)
async def upload_documents(
    kb: str,
    store: Store,
    settings: Config,
    request: Request,
    files: Annotated[list[UploadFile] | None, File()] = None,
    replace: Annotated[str | None, Form()] = None,
    uploaded_by: Annotated[str | None, Form(max_length=50)] = None,
) -> list[DocumentOut]:
    base = await _base(store, kb)
    if not files:
        raise HTTPException(422, "请选择至少一个 PDF、DOCX、MD 或 TXT 文件")
    if len(files) > MAX_FILES:
        raise HTTPException(422, "单次最多上传 20 个文件")
    replacing = (replace or "").strip().lower() == "true"
    before = {d.name: d for d in await store.documents(kb)} if replacing else {}
    try:
        documents = await asyncio.to_thread(
            validate_files, files, settings.knowledge_dir, base.kb_id
        )
    except UploadError as exc:
        raise HTTPException(422, str(exc)) from exc
    if replacing:
        names = [d.name for d in documents]
        if twice := next((name for name in names if names.count(name) > 1), None):
            raise HTTPException(422, f"这次上传里有两个「{twice}」，替换时每个文件名只能传一个")
    new = sum(d.name not in before for d in documents)
    if base.document_count + new > MAX_DOCUMENTS:
        raise HTTPException(422, "一个知识库最多 500 个文档")
    registered = False
    try:
        await asyncio.to_thread(save_files, files, documents)
        added = await store.add_documents(
            base.kb_id, documents, replace=replacing, uploaded_by=who(request, uploaded_by)
        )
        registered = True
    except (DocumentLimit, ReplaceKind) as exc:
        raise HTTPException(422, str(exc)) from exc
    except MissingBase as exc:
        raise HTTPException(404, "没有这个知识库") from exc
    finally:
        if not registered:
            await asyncio.to_thread(remove_files, documents)
    # A replaced document keeps its id; its previous original file goes.
    old = [
        before[d.name]
        for d in added
        if d.name in before
        and before[d.name].document_id == d.document_id
        and before[d.name].path != d.path
    ]
    await asyncio.to_thread(remove_files, old)
    _wake(request)
    return await _documents_out(store, kb, added, settings)


@router.get("/{kb}/documents/{doc}")
async def get_document(kb: str, doc: str, store: Store, settings: Config) -> DocumentOut:
    document = await _document(store, kb, doc)
    return (await _documents_out(store, kb, [document], settings))[0]


@router.patch("/{kb}/documents/{doc}")
async def update_document(
    kb: str, doc: str, body: UpdateDocument, store: Store, settings: Config
) -> DocumentOut:
    document = await _document(store, kb, doc)
    changes = body.model_dump(exclude_unset=True)
    if "owner" in changes:
        changes["owner"] = changes["owner"] or None
    start = changes.get("valid_from", document.valid_from)
    end = changes.get("valid_until", document.valid_until)
    if start is not None and end is not None and end < start:
        raise HTTPException(422, "失效日期不能早于生效日期")
    updated = await store.update_document(kb, doc, **changes)
    if updated is None:
        raise HTTPException(404, "知识库里没有这篇文档")
    return (await _documents_out(store, kb, [updated], settings))[0]


@router.get("/{kb}/documents/{doc}/versions")
async def document_versions(kb: str, doc: str, store: Store) -> list[VersionOut]:
    document = await _document(store, kb, doc)
    versions = await store.versions(doc)
    if not versions:
        # Documents from before versions were kept have just their first upload.
        return [
            VersionOut(
                version=1,
                size_bytes=document.size_bytes,
                uploaded_by=document.uploaded_by,
                uploaded_at=document.created_at,
            )
        ]
    return [VersionOut(**asdict(version)) for version in versions]


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
async def reindex_document(
    kb: str, doc: str, store: Store, request: Request, settings: Config
) -> DocumentOut:
    await _document(store, kb, doc)
    await store.requeue(kb, doc)
    _wake(request)
    return await get_document(kb, doc, store, settings)


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
    await _rerank_allowed(body.retrieval, body.top_k, catalog)
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
            include_inactive=body.include_inactive,
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


@router.get("/{kb}/regression")
async def get_regression(kb: str, store: Store) -> dict[str, Any]:
    base = await _base(store, kb)
    return regression_out(await store.get_regression(kb), base)


@router.put("/{kb}/regression")
async def save_regression(
    kb: str, body: RegressionBody, store: Store, request: Request, catalog: Catalog
) -> dict[str, Any]:
    await _base(store, kb)
    await _rerank_allowed(body.retrieval, body.top_k, catalog)
    ids = [case.id for case in body.cases]
    if twice := next((key for key in ids if ids.count(key) > 1), None):
        raise HTTPException(422, f"用例编号「{twice}」重复了")
    names = {d.name for d in await store.documents(kb)}
    for case in body.cases:
        if missing := next((name for name in case.expected if name not in names), None):
            raise HTTPException(422, f"知识库里没有文档「{missing}」")
    try:
        record = await store.save_regression(
            kb,
            [
                {**case.model_dump(), "expected": list(dict.fromkeys(case.expected))}
                for case in body.cases
            ],
            body.retrieval.model_dump(),
            body.top_k,
            body.min_score,
            who(request, body.actor),
        )
    except MissingBase as exc:
        raise HTTPException(404, "没有这个知识库") from exc
    return regression_out(record, await _base(store, kb))


@router.post("/{kb}/regression/run")
async def run_regression_now(
    kb: str,
    store: Store,
    request: Request,
    settings: Config,
    body: RunRegression | None = None,
) -> dict[str, Any]:
    await _base(store, kb)
    record = await store.get_regression(kb)
    if record is None or not record.cases:
        raise HTTPException(409, "先加检索用例再运行回归")
    last_run = await run_regression(
        store,
        request.app.state.llm_client,
        record,
        settings=settings,
        by=who(request, body.actor if body else None),
    )
    try:
        record = await store.save_regression_run(kb, last_run)
    except MissingBase as exc:
        raise HTTPException(404, "没有这个知识库") from exc
    return regression_out(record, await _base(store, kb))
