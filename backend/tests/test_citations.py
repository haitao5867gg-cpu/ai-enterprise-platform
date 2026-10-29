"""Which sources a run cites: the [n] in Markdown prose, and the texts a run shows."""

import pytest

from aep.knowledge.citations import cited_numbers
from aep.runtime.flow import graph as flowgraph


@pytest.mark.parametrize(
    ("text", "numbers"),
    [
        ("三十天内可以申请退货。[1]", {1}),
        ("按资料回答。[1][2]", {1, 2}),
        ("`[1]` 是代码", set()),
        ("```python\nrows = arr[3]\n```\n正文 [4]", {4}),
        ("```\n没有闭合的代码 [5]\n一直到结尾 [6]", set()),
        ("[普通链接 [1]](https://example.com)", set()),
        ("[3](https://x)", set()),
        ("![图 [2]](chart.png) 后面 [6]", {6}),
        ("~~~\n[7]\n~~~\n[8]", {8}),
        ("[1,2] 和 [1-3] 和 【1】", set()),
        ("说明 [1]\n\n[1]: x.pdf", {1}),
        ("```\r\ncode [1]\r\n```\r\nSee [2]", {2}),
        ("全角 [１] 和阿拉伯 [١]", set()),
        ("[" + "9" * 5000 + "]", set()),
    ],
)
def test_cited_numbers(text, numbers):
    assert cited_numbers(text) == numbers


def test_cited_reads_result_texts_answers_plans_and_show():
    def llm(node_id: str, **settings) -> dict:
        return {"id": node_id, "type": "llm", "output": {"kind": "text"}} | settings

    config = {
        "graph": {
            "nodes": [
                {"id": "start", "type": "start"},
                {"id": "kb", "type": "knowledge"},
                llm("answer"),
                llm("raw"),
                llm("fenced"),
                llm("constant"),
                {"id": "bare", "type": "llm"},
                llm("quiet", quiet=True),
                llm("extract", output={"kind": "json", "fields": [{"key": "text"}]}),
                {"id": "agent", "type": "agent"},
                {"id": "check_plan", "type": "etl_plan_check"},
                {"id": "check_old", "type": "etl_plan_check"},
                {"id": "end", "type": "end"},
            ]
        }
    }
    context = "[1]《政策.md》退货期限\n三十天内可以退货。"
    state = {
        "vars": {
            "start": {"question": "怎么退货 [13]"},
            "kb": {"hits": [], "context": context},
            "answer": {"text": "可以退货 [2]"},
            "raw": {"text": '{"summary": "方案 [3]"}'},
            "fenced": {"text": '```json\n{"summary": "方案 [4]"}\n```'},
            # The run page does not read this as JSON, so it shows it as text.
            "constant": {"text": '{"score": NaN, "note": "见 [14]"}'},
            "bare": {"text": "没写输出设置的按文本 [15]"},
            "quiet": {"text": "只给后面的节点 [5]"},
            "extract": {"text": "字段 [12]"},
            "agent": {"text": "查过资料了 [6]"},
            "check_plan": {"ok": True, "plan": {"references": [{"index": 7}, {"index": "x"}]}},
            "check_old": {"ok": False, "plan": {"references": [{"index": 8}]}},
        },
        "result": {
            "answer": "结果 [9]",
            "context": context,
            "count": 3,
            "nested": {"text": "[10]"},
        },
    }
    assert flowgraph.cited(config, state, ["给人看的 [11]"]) == {2, 6, 7, 9, 11, 14, 15}
    # Nothing ran yet.
    assert flowgraph.cited(config, {}) == set()
