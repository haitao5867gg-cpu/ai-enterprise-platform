"""Eval cases imported from a spreadsheet, and the per-field accuracy of an eval
(docs/api/evals.md, "从表格导入" and "按字段的准确率")."""

import io
import json
from datetime import date, datetime

import pytest
from openpyxl import Workbook

from aep import eval_import, eval_sets
from aep.api import eval_sets as api_eval_sets
from aep.runtime import catalog
from tests.test_eval_sets import support
from tests.test_flow_apps import Platform, flow


@pytest.fixture(autouse=True)
def fast_polling(monkeypatch):
    monkeypatch.setattr(catalog, "POLL_SECONDS", 0.01)


def xlsx(rows: list[list]) -> bytes:
    book = Workbook()
    sheet = book.active
    for row in rows:
        sheet.append(row)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


async def upload(p: Platform, app: str, name: str, data: bytes, mode: str | None = None):
    form = {"mode": mode} if mode else {}
    return await p.api.post(f"/apps/{app}/eval-set/import", files={"file": (name, data)}, data=form)


HEADER = [
    "id",
    "Title",
    "input.question",
    "answer.ask.order_id",
    "answer.review",
    "expect.order",
    "CONTAINS.answer",
    "judge.answer",
    None,
]


async def test_cases_come_from_a_table():
    async with Platform(["回答"]) as p:
        await p.new_app("support", support())
        table = [
            [],  # blank rows before the header are skipped
            HEADER,
            ["refund", " 退货 ", "我要退货", "SO-1", "approve", "SO-1", "退货；7 天|七天;", "礼貌"],
            [None, None, None, None, None, None, None, None, None],
            [None, "数字", "多少钱", 3.0, None, 2.5, None, None],
            [None, "日期", datetime(2026, 11, 5), date(2026, 11, 6), None, True, None, None],
        ]
        resp = await upload(p, "support", "cases.XLSX", xlsx(table))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["imported"] == 3 and body["source"] == "platform"
        refund, number, day = body["cases"]
        assert refund == {
            "id": "refund",
            "title": "退货",
            "input": {"question": "我要退货"},
            "answers": {"ask": {"values": {"order_id": "SO-1"}}, "review": {"decision": "approve"}},
            "expect": [
                {"kind": "equals", "path": "order", "value": "SO-1"},
                {"kind": "contains", "path": "answer", "values": ["退货", "7 天|七天"]},
                {"kind": "judge", "path": "answer", "criteria": "礼貌", "reference": None},
            ],
            "history": [],
            "turn": None,
            "restored": {},
            "from_run": None,
        }
        # Rows are counted from the one under the header; empty rows keep their number.
        assert (number["id"], day["id"]) == ("row-3", "row-4")
        assert number["answers"] == {"ask": {"values": {"order_id": 3}}}
        assert number["expect"] == [{"kind": "equals", "path": "order", "value": 2.5}]
        assert day["input"] == {"question": "2026-11-05"}
        assert day["answers"] == {"ask": {"values": {"order_id": "2026-11-06"}}}
        assert day["expect"] == [{"kind": "equals", "path": "order", "value": True}]

        # A csv appends (text only); GB18030 is read too.
        csv = "id,input.question,expect.order\nextra,我要换货,SO-2\n".encode("gb18030")
        more = await upload(p, "support", "more.csv", csv)
        assert more.status_code == 200, more.text
        assert more.json()["imported"] == 1
        assert [c["id"] for c in more.json()["cases"]] == ["refund", "row-3", "row-4", "extra"]
        assert more.json()["cases"][-1]["expect"][0]["value"] == "SO-2"

        # Replace keeps the threshold and judge model.
        await p.api.put(
            "/apps/support/eval-set",
            json={"cases": more.json()["cases"], "threshold": 0.6, "judge_model": "qwen"},
        )
        bom = "﻿id,input.question\nonly,问题\n".encode()
        replaced = await upload(p, "support", "x.csv", bom, mode="replace")
        assert replaced.status_code == 200, replaced.text
        assert [c["id"] for c in replaced.json()["cases"]] == ["only"]
        assert (replaced.json()["threshold"], replaced.json()["judge_model"]) == (0.6, "qwen")
        audit = (await p.api.get("/apps/support/eval-set")).json()
        assert [c["id"] for c in audit["cases"]] == ["only"]


async def test_a_table_that_cannot_be_imported_changes_nothing(monkeypatch):
    async with Platform(["回答"]) as p:
        await p.new_app("support", support())
        await p.api.put("/apps/support/eval-set", json={"cases": [{"id": "kept", "input": {}}]})

        def csv(text: str) -> bytes:
            return text.encode()

        refused = [
            ("a.txt", csv("id\nx\n"), 422, "只能导入 .xlsx 或 .csv 文件"),
            ("a.xlsx", b"not a workbook", 422, "读不出这个文件："),
            ("a.csv", csv("id,备注\nx,y\n"), 422, "第 2 列「备注」认不出来：列名要写成 id、title"),
            ("a.csv", csv("expect\nx\n"), 422, "第 1 列「expect」认不出来"),
            ("a.csv", csv("input.Question\nx\n"), 422, "第 1 列「input.Question」认不出来"),
            ("a.csv", csv("answer.ask.Order\nx\n"), 422, "第 1 列「answer.ask.Order」认不出来"),
            ("a.csv", csv("expect.a,EXPECT.a\n1,2\n"), 422, "第 2 列「EXPECT.a」和第 1 列重复了"),
            ("a.csv", csv("id\n\n"), 422, "表里没有用例"),
            ("a.csv", b"", 422, "表里没有用例"),
            ("a.csv", csv("id,\nx,y\n"), 422, "第 1 行第 2 列有值，但这一列没有列名"),
            (
                "a.csv",
                csv("id\nok\nA 1\n"),
                422,
                "第 2 行：用例 id「A 1」只能用小写字母、数字、- 和 _，最多 60 个字符",
            ),
            ("a.csv", csv("id\nkept\n"), 422, "第 1 行：用例 id「kept」已经有了"),
            ("a.csv", csv("id\nx\nx\n"), 422, "第 2 行：用例 id「x」已经有了"),
            ("a.csv", csv("title\n" + "长" * 101 + "\n"), 422, "第 1 行：标题最多 100 个字"),
            (
                "a.csv",
                csv(",".join(f"expect.f{i}" for i in range(21)) + "\n" + ",".join("1" * 21) + "\n"),
                422,
                "第 1 行：检查最多 20 项",
            ),
            ("a.csv", csv("id\n" + "\n" * 2), 422, "表里没有用例"),
        ]
        for name, data, code, message in refused:
            resp = await upload(p, "support", name, data)
            assert resp.status_code == code, (name, data, resp.text)
            assert resp.json()["detail"].startswith(message), (message, resp.json())

        monkeypatch.setattr(api_eval_sets, "MAX_CASES", 2)
        full = await upload(p, "support", "a.csv", csv("id\nn1\nn2\n"))
        assert full.status_code == 422
        assert full.json()["detail"] == "导入后有 3 条用例，一个评测集最多 2 条"
        assert (await upload(p, "support", "a.csv", csv("id\nn1\nn2\n"), "replace")).is_success

        monkeypatch.setattr(eval_import, "MAX_BYTES", 10)
        big = await upload(p, "support", "a.csv", csv("id\n" + "x" * 20 + "\n"))
        assert (big.status_code, big.json()["detail"]) == (413, "文件不能超过 5 MB")

        bad_mode = await upload(p, "support", "a.csv", csv("id\nx\n"), mode="merge")
        assert bad_mode.status_code == 422
        missing = await p.api.post("/apps/support/eval-set/import", data={"mode": "append"})
        assert missing.json()["detail"] == "请选择一个 .xlsx 或 .csv 文件"

        etl = await upload(p, "etl", "a.csv", csv("id\nx\n"))
        assert etl.status_code == 409
        assert (await upload(p, "nope", "a.csv", csv("id\nx\n"))).status_code == 404

        english = await p.api.post(
            "/apps/support/eval-set/import",
            files={"file": ("a.txt", b"x")},
            headers={"Accept-Language": "en"},
        )
        assert english.json()["detail"] == "Only .xlsx or .csv files can be imported"
        assert [c["id"] for c in (await p.api.get("/apps/support/eval-set")).json()["cases"]] == [
            "n1",
            "n2",
        ]


# Per-field accuracy


def classify() -> dict:
    """A model labels the question; the result is the label and a note."""
    return flow(
        {
            "id": "label",
            "type": "llm",
            "title": "分类",
            "prompt": "{{start.question}}",
            "output": {
                "kind": "json",
                "fields": [
                    {"key": "kind", "type": "string", "description": "类别"},
                    {"key": "note", "type": "string", "description": "说明"},
                ],
            },
        },
        {
            "id": "end",
            "type": "end",
            "title": "结束",
            "outputs": [
                {"key": "kind", "from": ["{{label.kind}}"]},
                {"key": "note", "from": ["{{label.note}}"]},
            ],
        },
        edges=[{"from": "start", "to": "label"}, {"from": "label", "to": "end"}],
    )


async def test_an_eval_counts_each_field():
    reply = json.dumps({"kind": "售后", "note": "固定说明"}, ensure_ascii=False)
    async with Platform([reply]) as p:
        await p.new_app("triage", classify())
        table = [
            ["id", "input.question", "expect.kind", "contains.note", "expect.missing"],
            ["a", "退货", "售后", "说明", None],
            ["b", "维修", "售后", "没有这个词", None],
            ["c", "价格", "咨询", None, None],
            ["d", "发票", "咨询", None, 1],
        ]
        assert (await upload(p, "triage", "t.xlsx", xlsx(table))).status_code == 200
        await p.api.put("/apps/triage/draft", json={"config": classify()})
        assert (await p.api.post("/apps/triage/draft/eval", json={})).status_code == 202
        await p.catalog.wait_idle()
        result = (await p.api.get("/apps/triage")).json()["draft"]["eval"]
        assert result["case_count"] == 4
        first = result["cases"][0]["checks"]
        assert first[0] == {"kind": "completed", "name": "运行完成", "passed": True, "detail": ""}
        assert first[1] == {
            "kind": "equals",
            "name": "kind 等于 售后",
            "passed": True,
            "detail": "",
            "path": "kind",
            "expected": "售后",
            "actual": "售后",
        }
        assert first[2] | {"name": ""} == {
            "kind": "contains",
            "name": "",
            "passed": True,
            "detail": "",
            "path": "note",
        }
        missing = result["cases"][3]["checks"][2]
        assert (missing["path"], missing["expected"], "actual" in missing) == ("missing", 1, False)
        assert result["fields"] == [
            {
                "path": "kind",
                "kind": "equals",
                "total": 4,
                "passed": 2,
                "rate": 0.5,
                "values": [
                    {"value": "售后", "total": 2, "passed": 2},
                    {"value": "咨询", "total": 2, "passed": 0},
                ],
                "errors": [{"expected": "咨询", "actual": "售后", "count": 2}],
            },
            {"path": "note", "kind": "contains", "total": 2, "passed": 1, "rate": 0.5},
            {
                "path": "missing",
                "kind": "equals",
                "total": 1,
                "passed": 0,
                "rate": 0.0,
                "values": [{"value": 1, "total": 1, "passed": 0}],
                "errors": [{"expected": 1, "actual": None, "count": 1}],
            },
        ]


def test_the_summary_counts_finished_cases_only():
    def check(path, passed, expected=None, actual=None, kind="equals"):
        out = {"kind": kind, "name": "", "passed": passed, "detail": "", "path": path}
        if kind == "equals":
            out |= {"expected": expected, "actual": actual}
        return out

    long = "x" * 300
    cases = [
        {"status": "done", "checks": [{"kind": "completed"}, check("a", True, 1, 1)]},
        {"status": "error", "checks": [check("a", False, 2, None)]},
        {"status": "running", "checks": [check("a", False, 3, 4)]},
        {"status": "pending", "checks": []},
        # Results kept before fields were: no path, not counted.
        {"status": "done", "checks": [{"kind": "equals", "passed": True}]},
        {"status": "done", "checks": [check("a", False, {"x": 1}, "y")]},
        {"status": "done", "checks": [check(None, True, kind="judge")]},
    ]
    assert eval_sets.field_summary(cases) == [
        {
            "path": "a",
            "kind": "equals",
            "total": 3,
            "passed": 1,
            "rate": 0.333,
            "values": [
                {"value": 1, "total": 1, "passed": 1},
                {"value": 2, "total": 1, "passed": 0},
            ],
            "errors": [{"expected": 2, "actual": None, "count": 1}],
        },
        {"path": None, "kind": "judge", "total": 1, "passed": 1, "rate": 1.0},
    ]
    assert eval_sets.shown(long) == "x" * 200 + "…"
    assert eval_sets.shown({"a": [1, 2]}) == '{"a": [1, 2]}'
    assert eval_sets.shown(3) == 3 and eval_sets.shown(None) is None
    many = [{"status": "done", "checks": [check("k", False, f"e{i}", "x")]} for i in range(12)]
    assert len(eval_sets.field_summary(many)[0]["errors"]) == eval_sets.MAX_ERRORS
