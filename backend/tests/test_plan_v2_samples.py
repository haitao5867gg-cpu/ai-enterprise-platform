"""方案第 2 版 on real model replies (docs/api/data-nodes.md): the checks and the platform's
tidying run on what models actually write, not only on hand-made plans.

Fixtures in tests/fixtures/plan_v2/: ``<family>-<n>.txt`` is one model's raw reply, the reply
text only, to the plan v2 prompt of the template etl-lakehouse 第 2 版 for request ``n``
(system: the skill etl-plan-brief and ``OUTPUT_FORMAT_V2``). ``inputs.json`` holds what the
prompts were built from: the requests with the glossary and standards hits they got (numbered
as the knowledge nodes number them), the retail demo's profile (connectors/mysql/seed/
003-retail.sql as the MySQL connector reports it) and the hit chunks of demo/knowledge. The
replies were collected once outside the repo; the tests only read them and never call a model.
``<family>-early-<n>.txt`` answered an earlier wording of the skill; some replies go back for a
rewrite (code in a rule, PySpark in a column's source), which is tested too.
Assertions are about properties that hold for any reasonable reply, never exact texts.
"""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from aep import llm
from aep.etl import answer
from aep.etl.plan import CITATION, CODE
from aep.runtime.flow import nodes
from aep.runtime.flow.nodes import Runtime
from tests.conftest import FakeGateway
from tests.test_flow_data_nodes import EMPTY, assert_human_question, check_node, run, state

FIXTURES = Path(__file__).parent / "fixtures" / "plan_v2"
INPUTS = json.loads((FIXTURES / "inputs.json").read_text())
SAMPLES = sorted(FIXTURES.glob("*-[0-9].txt"))
DEMO = "1"  # the demo request: by month, city and member level; four metrics
# Where a question or a rule has to say it is code, and where only structure was checked.
CODE_PROBLEM = "里有代码"
STRUCTURE = re.compile(r"不能为空|应该是")


def hits(n: str, node: str) -> list[dict[str, Any]]:
    """A knowledge node's ``hits`` output for request ``n``, as the prompt got them."""
    out = []
    for chunk_id, index, score in INPUTS["requests"][n][node]:
        hit = INPUTS["chunks"][chunk_id]
        scores = {"vector": None, "keyword": score, "rerank": None}
        out.append(hit | {"index": index, "score": score, "scores": scores})
    return out


async def checked(path: Path, retry: bool = False) -> tuple[str, dict[str, Any], Any]:
    """Request number, raw JSON and the plan check's outcome on one sample."""
    n = path.stem.rsplit("-", 1)[1]
    text = path.read_text()
    st = state(
        start={"language": "pyspark"},
        plan={"text": text},
        profile={"tables": INPUTS["profile"]},
        glossary={"hits": hits(n, "glossary")},
        standards={"hits": hits(n, "standards")},
    )
    if retry:
        st["branch"] = {"check_plan": "retry"}
    node = check_node(format=2, hits=["{{glossary.hits}}", "{{standards.hits}}"])
    rt = Runtime(FakeGateway().client, "mock", {}, {})
    return n, llm.parse_json(text), await run(node, st, rt)


def written_texts(raw: dict[str, Any]) -> list[str]:
    """The business texts the model wrote (data-nodes.md, 业务文字), as it wrote them."""
    texts: list[Any] = []
    brief = raw.get("brief")
    if isinstance(brief, dict):
        texts += [brief.get("goal"), brief.get("grain")]
        for m in brief.get("metrics") or []:
            if isinstance(m, dict):
                texts += [m.get(k) for k in ("name", "definition", "unit")]
        for d in brief.get("dimensions") or []:
            if isinstance(d, dict):
                texts += [d.get(k) for k in ("name", "definition")]
        texts += [f.get("text") for f in brief.get("filters") or [] if isinstance(f, dict)]
        if isinstance(brief.get("time_range"), dict):
            texts.append(brief["time_range"].get("text"))
    texts += [r.get("text") for r in raw.get("rules") or [] if isinstance(r, dict)]
    for q in raw.get("questions") or []:
        if isinstance(q, dict):
            texts += [q.get("text"), q.get("why")]
            texts += [o.get("label") for o in q.get("options") or [] if isinstance(o, dict)]
    for t in raw.get("targets") or []:
        for c in t.get("columns") or [] if isinstance(t, dict) else []:
            texts.append(c.get("label") if isinstance(c, dict) else None)
    return [t for t in texts if isinstance(t, str)]


def checked_texts(plan: dict[str, Any], generated_brief: bool) -> list[str]:
    """The same business texts in the checked plan (a generated brief is not checked)."""
    texts: list[Any] = []
    brief = plan["brief"]
    if brief is not None and not generated_brief:
        texts += [brief["goal"], brief["grain"]]
        texts += [m[k] for m in brief["metrics"] for k in ("name", "definition", "unit")]
        texts += [d[k] for d in brief["dimensions"] for k in ("name", "definition")]
        texts += [f["text"] for f in brief["filters"]]
        if brief["time_range"] is not None:
            texts.append(brief["time_range"]["text"])
    texts += [r["text"] for r in plan["rules"]]
    for q in plan["questions"]:
        texts += [q["text"], q["why"], *(o["label"] for o in q["options"] or [])]
    return [t for t in texts if isinstance(t, str)]


def has_code(text: str) -> bool:
    return CODE.search(CITATION.sub("", text)) is not None


def bare_source(expr: str) -> str:
    return re.sub(r"[\s`]", "", expr).lower()


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.stem)
async def test_a_real_reply_parses_and_is_checked(path):
    n, raw, out = await checked(path)
    assert isinstance(raw, dict)
    assert "回复不是要求的 JSON" not in out.outputs["problems"]
    if out.branch == "retry":
        # Sent back: every problem is one line the model can act on, and the new outputs
        # are empty.
        problems = out.outputs["problems"].splitlines()
        assert problems and all(p.strip() for p in problems)
        assert {k: out.outputs[k] for k in EMPTY} == EMPTY
        coded = any(has_code(t) for t in written_texts(raw))
        if coded and not any(STRUCTURE.search(p) for p in problems):
            assert any(CODE_PROBLEM in p for p in problems), problems
        return
    assert out.branch == "ok"
    outputs, plan = out.outputs, out.outputs["plan"]
    # A brief, written by the model or generated, and the version 2 shape of the plan.
    assert plan["brief"] is not None and plan["brief"]["goal"].strip()
    assert outputs["brief"] == plan["brief"]
    assert plan["summary"].strip() and plan["assumptions"] == []
    assert outputs["summary"].startswith(f"{len(plan['targets'])} 张目标表")
    assert "## 业务说明" in outputs["message"] and "## 技术方案" in outputs["message"]
    assert all(c["label"] for t in plan["targets"] for c in t["columns"])
    # No code in the business words after the check.
    generated = raw.get("brief") is None
    assert not [t for t in checked_texts(plan, generated) if has_code(t)]
    # Every [n] left points at a reference.
    cited = {hit["index"] for hit in outputs["references"]}
    for text in checked_texts(plan, False):
        assert {int(m) for m in CITATION.findall(text)} <= cited
    ids = {i["id"] for k in ("metrics", "dimensions", "filters") for i in plan["brief"][k]}
    assert set(outputs["uncited"]) <= ids
    # Every question can go to the human node as it is.
    assert len(outputs["questions"]) <= 5
    for q in outputs["questions"]:
        assert_human_question(q)
        assert nodes.field_problem(q, q["suggested"]) is None
    p = nodes.Problems(())
    nodes.check_fields({"fields": outputs["questions"]}, "fields", p, ("select",))
    assert p.found == []
    # What the platform tidied: one sentence each.
    cleaned = outputs["cleaned"]
    assert all(isinstance(c, str) and c.strip() and "\n" not in c for c in cleaned)


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.stem)
async def test_the_answer_of_a_real_reply_adds_up_only_where_it_can(path):
    n, raw, out = await checked(path, retry=True)
    if out.branch != "ok" or out.outputs["answer"] is None:
        pytest.skip("the reply needs a rewrite or has no answer design")
    design, plan = out.outputs["answer"], out.outputs["plan"]
    table = next(t for t in plan["targets"] if t["name"] == design["target"])
    sources = {c["name"]: c["source"] for c in table["columns"]}
    columns = design["columns"]
    assert [c["name"] for c in columns] == design["group_by"] + design["measures"]
    assert [c["role"] for c in columns] == ["dimension"] * len(design["group_by"]) + [
        "metric"
    ] * len(design["measures"])
    assert design["text"].endswith("。")
    if design["group_by"]:
        assert f"前 {design['top_n']} 组" in design["text"]
    for c in columns[len(design["group_by"]) :]:
        source = bare_source(answer._unwrap(sources[c["name"]]))
        assert c["agg"] in ("sum", "ratio", "none")
        if "(distinct" in source or source.startswith(("avg(", "min(", "max(")):
            assert c["agg"] == "none", c
        if c["agg"] == "ratio":
            for side in (c["numerator"], c["denominator"]):
                assert answer.additivity(side, sources)[0] == "sum"
        else:
            assert "numerator" not in c and "denominator" not in c
    if n == DEMO:
        # Sales add up whatever else the model chose.
        assert "sum" in {c["agg"] for c in columns if c["role"] == "metric"}
