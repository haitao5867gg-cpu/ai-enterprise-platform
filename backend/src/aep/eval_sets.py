"""Flow apps' eval sets: the cases builders keep on the platform, and how a case's run
is scored (docs/api/evals.md). The code app ``etl`` keeps its file (``aep.evals``).

A case is an input, the answers to give where the run waits for a person, and checks
on the run's result. ``AppCatalog`` runs the cases of a draft and gates publishing on
their average score; this module knows nothing of runs being started.
"""

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any, Protocol

from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from aep.evals import Check
from aep.runtime.store import DEFAULT_TENANT

MAX_CASES = 50
MAX_CHECKS = 20
MAX_ANSWERS = 10  # how many times a case's run may wait for a person
SHORT_TEXT = 50  # longer texts become a judge check when a run is saved as a case
SAME_MEANING = "意思和参考答案一致"
CASE_ID = re.compile(r"^[a-z0-9_-]{1,60}$")


@dataclass
class EvalSet:
    app: str
    threshold: float
    judge_model: str | None = None
    cases: list[dict[str, Any]] = field(default_factory=list)
    updated_by: str | None = None
    updated_at: datetime | None = None


class EvalSetStore(Protocol):
    async def setup(self) -> None: ...
    async def get(self, app: str) -> EvalSet | None: ...
    async def put(self, value: EvalSet) -> None: ...
    async def delete(self, app: str) -> None: ...


class MemoryEvalSetStore:
    def __init__(self) -> None:
        self.sets: dict[str, EvalSet] = {}

    async def setup(self) -> None:
        pass

    async def get(self, app: str) -> EvalSet | None:
        found = self.sets.get(app)
        return replace(found, cases=json.loads(json.dumps(found.cases))) if found else None

    async def put(self, value: EvalSet) -> None:
        self.sets[value.app] = replace(value, cases=json.loads(json.dumps(value.cases)))

    async def delete(self, app: str) -> None:
        self.sets.pop(app, None)


SCHEMA = """
CREATE TABLE IF NOT EXISTS eval_sets (
    tenant_id    text NOT NULL DEFAULT 'default',
    app          text NOT NULL,
    threshold    double precision NOT NULL,
    judge_model  text,
    cases        jsonb NOT NULL,
    updated_by   text,
    updated_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, app)
)"""


class PostgresEvalSetStore:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def setup(self) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(SCHEMA)

    async def get(self, app: str) -> EvalSet | None:
        async with self._pool.connection() as conn:
            cur = await conn.execute(
                "SELECT app, threshold, judge_model, cases, updated_by, updated_at"
                " FROM eval_sets WHERE tenant_id = %s AND app = %s",
                (DEFAULT_TENANT, app),
            )
            row = await cur.fetchone()
        return EvalSet(**row) if row else None

    async def put(self, value: EvalSet) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "INSERT INTO eval_sets"
                " (tenant_id, app, threshold, judge_model, cases, updated_by, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON CONFLICT (tenant_id, app) DO UPDATE SET threshold = EXCLUDED.threshold,"
                " judge_model = EXCLUDED.judge_model, cases = EXCLUDED.cases,"
                " updated_by = EXCLUDED.updated_by, updated_at = EXCLUDED.updated_at",
                (
                    DEFAULT_TENANT,
                    value.app,
                    value.threshold,
                    value.judge_model,
                    Jsonb(value.cases),
                    value.updated_by,
                    value.updated_at or datetime.now(UTC),
                ),
            )

    async def delete(self, app: str) -> None:
        async with self._pool.connection() as conn:
            await conn.execute(
                "DELETE FROM eval_sets WHERE tenant_id = %s AND app = %s", (DEFAULT_TENANT, app)
            )


# Scoring


_MISSING = object()


def at_path(result: Any, path: str | None) -> Any:
    """The value at ``a.b`` in the result, or a marker when it is not there."""
    value = result
    for part in (path or "").split(".") if path else []:
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif isinstance(value, list) and part.isdigit() and int(part) < len(value):
            value = value[int(part)]
        else:
            return _MISSING
    return value


def as_text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _found(text: str, word: str) -> bool:
    """The word, or one of its ``a|b`` alternatives, is in the text (any case)."""
    return any(w.strip().lower() in text.lower() for w in word.split("|") if w.strip())


def check_name(check: dict[str, Any]) -> str:
    where = check.get("path") or "结果"
    kind = check["kind"]
    if kind == "equals":
        shown = as_text(check.get("value"))
        return f"{where} 等于 {shown[:40]}{'…' if len(shown) > 40 else ''}"
    if kind == "contains":
        return f"{where} 包含 {'、'.join(check.get('values') or [])}"
    if kind == "excludes":
        return f"{where} 不含 {'、'.join(check.get('values') or [])}"
    return f"模型判断 {where}：{check.get('criteria', '')[:40]}"


Judge = Callable[[str, str, str | None], Awaitable[tuple[bool, str]]]


async def score_flow_case(case: dict[str, Any], run: dict[str, Any], judge: Judge) -> list[Check]:
    """``run`` has the run's ``status``, ``error`` and ``result``."""
    done = run.get("status") == "done"
    reason = str(run.get("error") or "运行没有完成")[:300]
    checks = [Check("completed", "运行完成", done, "" if done else reason)]
    result = run.get("result")
    for check in case.get("expect") or []:
        name = check_name(check)
        kind = check["kind"]
        if not done:
            checks.append(Check(kind, name, False, "运行没有完成"))
            continue
        value = at_path(result, check.get("path"))
        if value is _MISSING:
            checks.append(Check(kind, name, False, f"结果里没有 {check.get('path')}"))
            continue
        if kind == "equals":
            ok = value == check.get("value")
            detail = "" if ok else f"实际是 {as_text(value)[:200]}"
            checks.append(Check(kind, name, ok, detail))
        elif kind in ("contains", "excludes"):
            text = as_text(value)
            hits = [w for w in check.get("values") or [] if _found(text, w)]
            if kind == "contains":
                missing = [w for w in check.get("values") or [] if w not in hits]
                detail = f"没有出现 {'、'.join(missing)}" if missing else ""
                checks.append(Check(kind, name, not missing, detail))
            else:
                detail = f"出现了 {'、'.join(hits)}" if hits else ""
                checks.append(Check(kind, name, not hits, detail))
        else:
            reference = check.get("reference")
            ok, why = await judge(
                as_text(value),
                check.get("criteria") or SAME_MEANING,
                None if reference is None else as_text(reference),
            )
            checks.append(Check(kind, name, ok, why[:300]))
    return checks


JUDGE_PROMPT = """你在给一个 AI 应用的输出打分。只判断它是否满足标准，不要改写它。

标准：{criteria}
{reference}
输出：
{answer}

只回答一个 JSON 对象：{{"pass": true 或 false, "reason": "一句话理由"}}"""


def judge_messages(answer: str, criteria: str, reference: str | None) -> list[dict[str, str]]:
    ref = f"参考答案：\n{reference}\n" if reference is not None else ""
    prompt = JUDGE_PROMPT.format(criteria=criteria, reference=ref, answer=answer[:8000])
    return [{"role": "user", "content": prompt}]


def judge_reply(reply: Any) -> tuple[bool, str]:
    """``{"pass", "reason"}`` from the judge model's parsed reply."""
    if not isinstance(reply, dict) or not isinstance(reply.get("pass"), bool):
        raise ValueError("模型的回答里没有 pass")
    return reply["pass"], str(reply.get("reason") or "")


# Saving a run as a case


def suggested_checks(
    result: Any,
    skip: set[str] | frozenset[str] = frozenset(),
    worded: set[str] | frozenset[str] = frozenset(),
) -> list[dict]:
    """Checks that a run with this result passes: equals for short values, a judge
    against the run's own answer for long texts. ``skip`` are fields that record how the
    run went (an agent's ``steps``, ``hits``), different on every run; ``worded`` are
    texts a model wrote, worded differently on every run, so they get a judge however
    short they are."""
    if not isinstance(result, dict):
        return [{"kind": "equals", "path": None, "value": result}] if result is not None else []
    checks: list[dict[str, Any]] = []
    for key, value in result.items():
        if key in skip:
            continue
        if isinstance(value, str) and (
            len(value) > SHORT_TEXT or (key in worded and value.strip())
        ):
            checks.append(
                {"kind": "judge", "path": key, "criteria": SAME_MEANING, "reference": value}
            )
        else:
            checks.append({"kind": "equals", "path": key, "value": value})
        if len(checks) == MAX_CHECKS:
            break
    return checks


def case_id(run_id: str, taken: set[str]) -> str:
    base = "run-" + re.sub(r"[^a-z0-9]", "", run_id.lower())[-8:]
    found, n = base, 1
    while found in taken:
        n += 1
        found = f"{base}-{n}"
    return found
