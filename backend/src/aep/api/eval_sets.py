"""Eval sets of apps (docs/api/evals.md, "评测集")."""

import asyncio
from typing import Annotated, Any, Literal

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

from aep import conversations as chat
from aep import eval_import
from aep.api.apps import Catalog, _invalid, _not_found
from aep.auth import who
from aep.eval_sets import MAX_CASES, MAX_CHECKS
from aep.runtime.appconfig import ConfigError
from aep.runtime.catalog import AppNotFound, CaseFromRunProblem, EvalSetReadOnly

router = APIRouter(prefix="/apps", tags=["eval sets"])

READ_ONLY = "这个应用的评测集在仓库里维护，页面上不能改"

Path = Annotated[str, StringConstraints(min_length=1, max_length=200, pattern=r"^[^\s.][^\s]*$")]
Word = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Equals(Strict):
    kind: Literal["equals"]
    path: Path | None = None
    value: Any = None


class Contains(Strict):
    kind: Literal["contains", "excludes"]
    path: Path | None = None
    values: list[Word] = Field(min_length=1, max_length=20)


class Judge(Strict):
    kind: Literal["judge"]
    path: Path | None = None
    criteria: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]
    reference: Any = None


Check = Annotated[Equals | Contains | Judge, Field(discriminator="kind")]


class Answer(Strict):
    decision: Annotated[str, StringConstraints(min_length=1, max_length=100)] | None = None
    comment: Annotated[str, StringConstraints(max_length=1000)] | None = None
    values: dict[str, Any] | None = None


class Turn(Strict):
    """One earlier turn of a case's conversation."""

    message: Annotated[str, StringConstraints(min_length=1, max_length=chat.MAX_MESSAGE)]
    # None (or empty): the turn had no reply, and its history has only the message.
    reply: Annotated[str, StringConstraints(max_length=chat.HISTORY_CHARS)] | None = None


class Case(Strict):
    id: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_-]{1,60}$")]
    title: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    answers: dict[str, Answer] = Field(default_factory=dict)
    expect: list[Check] = Field(default_factory=list, max_length=MAX_CHECKS)
    history: list[Turn] = Field(default_factory=list, max_length=chat.MAX_HISTORY_TURNS)
    turn: int | None = Field(default=None, ge=1, le=10000)
    restored: dict[str, str] = Field(default_factory=dict)
    from_run: str | None = None


class PutEvalSet(Strict):
    cases: list[Case] = Field(max_length=MAX_CASES)
    threshold: float | None = Field(default=None, ge=0, le=1)
    judge_model: Annotated[str, StringConstraints(min_length=1, max_length=100)] | None = None


class FromRun(Strict):
    run_id: str
    rating: Literal["good", "bad"] = "good"


def _case(case: Case) -> dict[str, Any]:
    out = case.model_dump(mode="json")
    out["title"] = out["title"] or None
    out["answers"] = {
        node: {k: v for k, v in answer.items() if v is not None}
        for node, answer in out["answers"].items()
    }
    return out


def _turn_problems(i: int, case: Case) -> list[dict[str, Any]]:
    """What is wrong with a case's history and restored tokens (docs/api/evals.md)."""
    turns = [t.model_dump() for t in case.history]
    found: list[dict[str, Any]] = []
    if chat.history_chars(turns) > chat.HISTORY_CHARS:
        msg = f"历史的消息和回答合计最多 {chat.HISTORY_CHARS} 字"
        found.append({"loc": ["body", "cases", i, "history"], "msg": msg, "type": "value_error"})
    if set(case.restored) - chat.tokens_in(turns):
        msg = "还原表里有历史里没有的标记"
        found.append({"loc": ["body", "cases", i, "restored"], "msg": msg, "type": "value_error"})
    return found


@router.get("/{app}/eval-set")
async def get_eval_set(app: str, catalog: Catalog) -> dict[str, Any]:
    try:
        return await catalog.get_eval_set(app)
    except AppNotFound as exc:
        raise _not_found(app) from exc


@router.put("/{app}/eval-set")
async def put_eval_set(
    app: str, body: PutEvalSet, catalog: Catalog, request: Request
) -> dict[str, Any]:
    seen: set[str] = set()
    for i, case in enumerate(body.cases):
        if case.id in seen:
            raise RequestValidationError(
                [
                    {
                        "loc": ["body", "cases", i, "id"],
                        "msg": f"用例 id {case.id} 重复了",
                        "type": "value_error",
                    }
                ]
            )
        seen.add(case.id)
    problems = [p for i, case in enumerate(body.cases) for p in _turn_problems(i, case)]
    if problems:
        raise RequestValidationError(problems)
    try:
        return await catalog.put_eval_set(
            app,
            [_case(c) for c in body.cases],
            body.threshold,
            body.judge_model,
            who(request, None),
        )
    except AppNotFound as exc:
        raise _not_found(app) from exc
    except EvalSetReadOnly as exc:
        raise HTTPException(409, READ_ONLY) from exc
    except ConfigError as exc:
        raise _invalid(exc) from exc


@router.post("/{app}/eval-set/cases/from-run", status_code=status.HTTP_201_CREATED)
async def case_from_run(
    app: str, body: FromRun, catalog: Catalog, request: Request
) -> dict[str, Any]:
    try:
        return await catalog.case_from_run(app, body.run_id, body.rating, who(request, None))
    except AppNotFound as exc:
        raise _not_found(app) from exc
    except EvalSetReadOnly as exc:
        raise HTTPException(409, READ_ONLY) from exc
    except CaseFromRunProblem as exc:
        raise HTTPException(exc.status, str(exc)) from exc


def _row_problem(number: int, case: dict[str, Any], exc: ValidationError) -> str:
    """One sentence on what is wrong with a table row (docs/api/evals.md, "从表格导入")."""
    error = exc.errors()[0]
    loc = [str(part) for part in error["loc"]]
    where = f"第 {number} 行："
    if loc[:1] == ["id"]:
        return where + f"用例 id「{case.get('id')}」只能用小写字母、数字、- 和 _，最多 60 个字符"
    if loc[:1] == ["title"]:
        return where + "标题最多 100 个字"
    if loc[:1] == ["expect"] and len(loc) == 1:
        return where + f"检查最多 {MAX_CHECKS} 项"
    if loc[:1] == ["expect"]:
        return where + "检查的写法不对：" + error["msg"]
    return where + ".".join(loc) + " " + error["msg"]


@router.post("/{app}/eval-set/import")
async def import_eval_set(
    app: str,
    catalog: Catalog,
    request: Request,
    file: Annotated[UploadFile | None, File()] = None,
    mode: Annotated[str, Form()] = "append",
) -> dict[str, Any]:
    try:
        current = await catalog.get_eval_set(app)
    except AppNotFound as exc:
        raise _not_found(app) from exc
    if not current["editable"]:
        raise HTTPException(409, READ_ONLY)
    if file is None:
        raise HTTPException(422, "请选择一个 .xlsx 或 .csv 文件")
    if mode not in ("append", "replace"):
        raise HTTPException(422, "mode 只能是 append 或 replace")
    data = await file.read(eval_import.MAX_BYTES + 1)
    try:
        rows = await asyncio.to_thread(eval_import.cases, file.filename or "", data)
    except eval_import.ImportProblem as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    kept = current["cases"] if mode == "append" else []
    taken = {c["id"] for c in kept}
    added: list[dict[str, Any]] = []
    for number, raw in rows:
        try:
            case = Case.model_validate(raw)
        except ValidationError as exc:
            raise HTTPException(422, _row_problem(number, raw, exc)) from exc
        if case.id in taken:
            raise HTTPException(422, f"第 {number} 行：用例 id「{case.id}」已经有了")
        taken.add(case.id)
        added.append(_case(case))
    total = len(kept) + len(added)
    if total > MAX_CASES:
        raise HTTPException(422, f"导入后有 {total} 条用例，一个评测集最多 {MAX_CASES} 条")
    try:
        saved = await catalog.put_eval_set(
            app,
            [*kept, *added],
            current["threshold"],
            current["judge_model"],
            who(request, None),
        )
    except AppNotFound as exc:
        raise _not_found(app) from exc
    except EvalSetReadOnly as exc:
        raise HTTPException(409, READ_ONLY) from exc
    except ConfigError as exc:
        raise _invalid(exc) from exc
    return saved | {"imported": len(added)}
