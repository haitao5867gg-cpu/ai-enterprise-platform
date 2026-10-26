"""The ETL eval set runner: ``aep-eval`` (``make eval MODEL=...``).

Each case in ``evals/etl/cases.yaml`` is a request on the sample database and what
a good outcome has (see the comment at the top of that file). The runner starts
one ``etl`` run per case through the Runs API of a running platform, confirms the
plan, lets the run go as far as the Databricks dev target (never prod: the runs
ask for ``release: false``) and scores every expectation:

- plan: tables, columns, assumptions and so on, read from the proposed plan;
- code: the run finished and the generated code passed the static checks;
- result: with Databricks, the dev run passed and tables wrote the expected rows.

A case's score is the share of its checks that passed; checks that could not run
(no Databricks, or the run failed before that layer) are skipped, not failed.
The report is printed as a table and written as JSON.
"""

import argparse
import asyncio
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import yaml

POLL_SECONDS = 5.0
ACTOR = "aep-eval"


@dataclass
class Check:
    layer: str
    name: str
    passed: bool | None  # None: skipped
    detail: str = ""


@dataclass
class CaseResult:
    id: str
    run_id: str | None = None
    status: str = "error"
    error: str | None = None
    trace_id: str | None = None
    seconds: float = 0.0
    checks: list[Check] = field(default_factory=list)

    @property
    def score(self) -> float | None:
        scored = [c.passed for c in self.checks if c.passed is not None]
        return sum(scored) / len(scored) if scored else None


def load_cases(path: Path) -> list[dict[str, Any]]:
    cases = yaml.safe_load(path.read_text()) or []
    if not isinstance(cases, list):
        raise ValueError(f"{path} 应该是一个用例列表")
    ids = [c.get("id") for c in cases]
    for case in cases:
        if not case.get("id") or not case.get("request"):
            raise ValueError(f"用例缺少 id 或 request：{case}")
    if len(set(ids)) != len(ids):
        raise ValueError("用例 id 重复")
    return cases


def _has(names: list[str], word: str) -> bool:
    """Some name contains the word, or one of its ``a|b`` alternatives."""
    return any(w.lower() in n.lower() for w in word.split("|") for n in names)


def _matching(plan: dict[str, Any], words: list[str]) -> list[str]:
    """Target tables whose column names contain every word."""
    return [
        t["name"]
        for t in plan.get("targets", [])
        if all(_has([c["name"] for c in t.get("columns", [])], w) for w in words)
    ]


PLAN_CHECKS = (
    "sources", "targets", "layered", "columns", "mention", "forbid", "assumptions", "load",
)  # fmt: skip


def score_plan(expect: dict[str, Any], plan: dict[str, Any] | None) -> list[Check]:
    """Checks on the proposed plan; all skipped when there was none."""
    names = [k for k in PLAN_CHECKS if k in expect]
    if plan is None:
        return [Check("plan", n, None, "没有方案") for n in names]
    targets = plan.get("targets", [])
    checks = []
    if "sources" in expect:
        used = {s for t in targets for s in t.get("sources", [])}
        missing = [s for s in expect["sources"] if s not in used]
        checks.append(Check("plan", "sources", not missing, f"缺少 {missing}" if missing else ""))
    if "targets" in expect:
        low, high = expect["targets"].get("min", 1), expect["targets"].get("max", 10**6)
        ok = low <= len(targets) <= high
        checks.append(Check("plan", "targets", ok, f"{len(targets)} 张目标表"))
    if "layered" in expect:
        own = {t["name"] for t in targets}
        layered = any(s in own for t in targets for s in t.get("sources", []))
        checks.append(Check("plan", "layered", layered == bool(expect["layered"])))
    if "columns" in expect:
        columns = [c["name"] for t in targets for c in t.get("columns", [])]
        missing = [w for w in expect["columns"] if not _has(columns, w)]
        checks.append(
            Check("plan", "columns", not missing, f"没有列名含 {missing}" if missing else "")
        )
    if "mention" in expect:
        text = json.dumps(plan, ensure_ascii=False)
        missing = [w for w in expect["mention"] if not _has([text], w)]
        checks.append(Check("plan", "mention", not missing, f"没提到 {missing}" if missing else ""))
    if "forbid" in expect:
        # What the tables compute: column expressions, and quality rules without the names
        # of the output columns they check. Not the names, logic or assumptions.
        computed = []
        for t in targets:
            columns = [c.get("name", "") for c in t.get("columns", [])]
            computed += [c.get("source", "") for c in t.get("columns", [])]
            own = re.compile(r"\b(?:" + "|".join(map(re.escape, columns)) + r")\b", re.I)
            for q in t.get("quality_rules", []):
                # Odd parts are string literals: kept whole, names are dropped only outside.
                parts = re.split(r"('(?:[^']|'')*')", q.get("condition", ""))
                computed.append(
                    "".join(p if i % 2 else own.sub("", p) for i, p in enumerate(parts))
                )
        text = json.dumps(computed, ensure_ascii=False)
        found = [w for w in expect["forbid"] if _has([text], w)]
        checks.append(Check("plan", "forbid", not found, f"用了 {found}" if found else ""))
    if "assumptions" in expect:
        text = "\n".join(plan.get("assumptions") or [])
        missing = [w for w in expect["assumptions"] if not _has([text], w)]
        detail = f"假设里没提到 {missing}" if missing else ""
        checks.append(Check("plan", "assumptions", not missing, detail))
    if "load" in expect:
        want = expect["load"]
        ok = any(all(t.get("load", {}).get(k) == v for k, v in want.items()) for t in targets)
        checks.append(Check("plan", "load", ok))
    return checks


def score_result(expect: dict[str, Any], run: dict[str, Any] | None) -> list[Check]:
    """Checks on the finished run: the code layer, then the dev run on Databricks."""
    result = (run or {}).get("result") or {}
    done = bool(run) and run.get("status") == "done" and bool(result)
    rows = expect.get("rows") or []
    if not done:
        reason = (run or {}).get("error") or "run 没有完成"
        return [
            Check("code", "checks", False, str(reason)[:300]),
            Check("result", "dev_run", None, "run 没有完成"),
            *[Check("result", f"rows {r['columns']}", None, "run 没有完成") for r in rows],
        ]
    problems = (result.get("checks") or {}).get("problems") or []
    checks = [
        Check(
            "code",
            "checks",
            not problems and (result.get("checks") or {}).get("passed") is True,
            "；".join(problems)[:300],
        )
    ]
    verification = result.get("verification") or {}
    if verification.get("status") in (None, "skipped"):
        reason = verification.get("reason") or "没有试跑"
        checks.append(Check("result", "dev_run", None, reason))
        checks += [Check("result", f"rows {r['columns']}", None, reason) for r in rows]
        return checks
    passed = verification["status"] == "passed"
    errors = "；".join(verification.get("errors") or [])[:300]
    checks.append(Check("result", "dev_run", passed, errors))
    tables = {t["name"]: t for t in verification.get("tables") or []}
    plan = result.get("plan") or {}
    for want in rows:
        label = f"rows {want['columns']}"
        names = [n for n in _matching(plan, want["columns"]) if n in tables]
        if not names:
            checks.append(Check("result", label, False, "没有列名符合的目标表"))
            continue
        low, high = want.get("min", 0), want.get("max", 10**12)
        counts = {n: tables[n].get("rows") for n in names}
        ok = any(c is not None and low <= c <= high for c in counts.values())
        detail = "，".join(f"{n} {c} 行" for n, c in counts.items())
        checks.append(Check("result", label, ok, detail))
    return checks


class Runner:
    def __init__(
        self,
        api: httpx.AsyncClient,
        datasource: str | None = None,
        model: str | None = None,
        timeout: float = 1800.0,
    ) -> None:
        self._api = api
        self._datasource = datasource
        self._model = model
        self._timeout = timeout

    async def _datasource_id(self) -> str:
        sources = (await self._api.get("/datasources")).raise_for_status().json()
        if not sources:
            raise RuntimeError("平台里没有登记数据源")
        if self._datasource is None:
            return sources[0]["datasource_id"]
        for s in sources:
            if self._datasource in (s["name"], s["datasource_id"]):
                return s["datasource_id"]
        raise RuntimeError(f"找不到数据源 {self._datasource}")

    async def _wait(self, run_id: str, deadline: float) -> dict[str, Any]:
        while True:
            run = (await self._api.get(f"/runs/{run_id}")).raise_for_status().json()
            if run["status"] in ("waiting", "done", "error"):
                return run
            if time.monotonic() > deadline:
                raise TimeoutError(f"run {run_id} 超过 {self._timeout:g} 秒没有结束")
            await asyncio.sleep(POLL_SECONDS)

    async def run_case(self, case: dict[str, Any], datasource_id: str) -> CaseResult:
        out = CaseResult(case["id"])
        started = time.monotonic()
        deadline = started + self._timeout
        expect = case.get("expect") or {}
        plan: dict[str, Any] | None = None
        run: dict[str, Any] | None = None
        inp: dict[str, Any] = {
            "datasource_id": datasource_id,
            "request": case["request"],
            "name": "eval_" + case["id"].replace("-", "_"),
            "release": False,
            "eval": case["id"],
        }
        if self._model:
            inp["models"] = {"plan": self._model, "codegen": self._model}
        try:
            created = await self._api.post("/runs", json={"app": "etl", "input": inp})
            out.run_id = created.raise_for_status().json()["run_id"]
            run = await self._wait(out.run_id, deadline)
            pending = run.get("pending") or {}
            if (
                run["status"] == "waiting"
                and (pending.get("details") or {}).get("type") == "etl_plan"
            ):
                plan = pending["details"]["plan"]
                body = {"decision": "approve", "actor": ACTOR}
                resp = await self._api.post(f"/runs/{out.run_id}/resume", json=body)
                resp.raise_for_status()
                run = await self._wait(out.run_id, deadline)
            if run["status"] == "waiting":
                run = {**run, "status": "error", "error": "run 停在了意外的确认步骤"}
        except (httpx.HTTPError, TimeoutError, KeyError) as exc:
            out.error = str(exc) or type(exc).__name__
            run = {"status": "error", "error": out.error}
        out.status = run.get("status", "error")
        out.error = out.error or run.get("error")
        out.trace_id = run.get("trace_id")
        out.checks = score_plan(expect, plan) + score_result(expect, run)
        out.seconds = round(time.monotonic() - started, 1)
        return out

    async def run(self, cases: list[dict[str, Any]], parallel: int = 3) -> list[CaseResult]:
        datasource_id = await self._datasource_id()
        gate = asyncio.Semaphore(parallel)

        async def one(case: dict[str, Any]) -> CaseResult:
            async with gate:
                return await self.run_case(case, datasource_id)

        return list(await asyncio.gather(*(one(c) for c in cases)))


def summary(results: list[CaseResult]) -> dict[str, Any]:
    layers: dict[str, list[bool]] = {}
    for r in results:
        for c in r.checks:
            if c.passed is not None:
                layers.setdefault(c.layer, []).append(c.passed)
    scores = [r.score for r in results if r.score is not None]
    return {
        "score": round(sum(scores) / len(scores), 3) if scores else None,
        "layers": {k: round(sum(v) / len(v), 3) for k, v in layers.items()},
        "cases": len(results),
    }


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def render(results: list[CaseResult], model: str | None) -> str:
    lines = [f"ETL 评测（模型：{model or '平台默认'}）", ""]
    lines.append(f"{'用例':<30} {'状态':<6} {'得分':>5}  未通过")
    for r in results:
        failed = [f"{c.layer}.{c.name}" + (f"（{c.detail}）" if c.detail else "")
                  for c in r.checks if c.passed is False]  # fmt: skip
        lines.append(f"{r.id:<30} {r.status:<6} {_pct(r.score):>5}  {'；'.join(failed)}")
    total = summary(results)
    layers = "，".join(f"{k} {_pct(v)}" for k, v in total["layers"].items())
    lines += ["", f"总分 {_pct(total['score'])}（{layers}）"]
    skipped = {c.detail for r in results for c in r.checks if c.passed is None and c.detail}
    if skipped:
        lines.append("跳过的检查：" + "；".join(sorted(skipped)))
    return "\n".join(lines)


def report(results: list[CaseResult], model: str | None) -> dict[str, Any]:
    return {
        "at": datetime.now(UTC).isoformat(),
        "model": model,
        **summary(results),
        "results": [asdict(r) | {"score": r.score} for r in results],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aep-eval", description="运行 ETL 评测集并打分")
    parser.add_argument("cases", type=Path, help="评测集 YAML，如 evals/etl/cases.yaml")
    parser.add_argument("--api", default="http://localhost:8000", help="平台 API 地址")
    parser.add_argument("--model", help="模型别名，用于方案和代码生成（默认用平台的默认模型）")
    parser.add_argument("--datasource", help="数据源名称或 id（默认第一个）")
    parser.add_argument("--only", nargs="*", help="只跑这些用例 id")
    parser.add_argument("--parallel", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=1800.0, help="每个用例最多等多少秒")
    parser.add_argument("--out", type=Path, help="JSON 报告路径（默认 evals/reports/ 下）")
    args = parser.parse_args(argv)

    cases = load_cases(args.cases)
    if args.only:
        cases = [c for c in cases if c["id"] in args.only]

    async def go() -> list[CaseResult]:
        async with httpx.AsyncClient(base_url=args.api, timeout=60) as api:
            runner = Runner(api, args.datasource, args.model, args.timeout)
            return await runner.run(cases, args.parallel)

    results = asyncio.run(go())
    print(render(results, args.model))
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    out = args.out or args.cases.parent.parent / "reports" / f"etl-{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report(results, args.model), ensure_ascii=False, indent=1))
    print(f"报告：{out}")
    return 0 if all(r.status == "done" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
