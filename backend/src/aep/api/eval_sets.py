"""Eval sets of apps (docs/api/evals.md, "评测集")."""

from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from aep.api.apps import Catalog, _not_found
from aep.auth import who
from aep.eval_sets import MAX_CASES, MAX_CHECKS
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


class Case(Strict):
    id: Annotated[str, StringConstraints(pattern=r"^[a-z0-9_-]{1,60}$")]
    title: Annotated[str, StringConstraints(strip_whitespace=True, max_length=100)] | None = None
    input: dict[str, Any] = Field(default_factory=dict)
    answers: dict[str, Answer] = Field(default_factory=dict)
    expect: list[Check] = Field(default_factory=list, max_length=MAX_CHECKS)
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
