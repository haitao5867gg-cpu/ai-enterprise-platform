"""Flow apps the platform ships (``hello``) and the templates new apps start from."""

import copy
from dataclasses import dataclass
from typing import Any

HELLO_SYSTEM = "你是一个乐于助人的助手，用中文简洁地回答。"


def _qa_approval() -> dict[str, Any]:
    """Answer, ask a person, rewrite with their comment when they reject."""
    return {
        "schema_version": 1,
        "env": {"vars": [], "secrets": []},
        "graph": {
            "nodes": [
                {
                    "id": "start",
                    "type": "start",
                    "title": "开始",
                    "inputs": [
                        {"key": "text", "label": "问题", "type": "textarea", "required": True}
                    ],
                },
                {
                    "id": "draft",
                    "type": "llm",
                    "title": "回答",
                    "model": None,
                    "skills": [],
                    "system": HELLO_SYSTEM,
                    "prompt": (
                        "问题：{{start.text}}\n上一版回答：{{draft.text}}\n"
                        "修改意见：{{review.comment}}\n"
                        "（没有上一版时直接回答问题；有修改意见时按意见重写上一版。）"
                    ),
                    "output": {"kind": "text"},
                    "max_visits": 5,
                },
                {
                    "id": "review",
                    "type": "human",
                    "title": "确认",
                    "mode": "approval",
                    "prompt": "确认这个回答吗？驳回时可以写下修改意见。",
                    "show": "{{draft.text}}",
                    "options": [
                        {"key": "approve", "label": "确认"},
                        {"key": "reject", "label": "驳回"},
                    ],
                    "comment_required": [],
                    "max_visits": 5,
                },
                {
                    "id": "end",
                    "type": "end",
                    "title": "结束",
                    "outputs": [{"key": "answer", "from": ["{{draft.text}}"]}],
                },
            ],
            "edges": [
                {"from": "start", "to": "draft"},
                {"from": "draft", "to": "review"},
                {"from": "review", "branch": "approve", "to": "end"},
                {"from": "review", "branch": "reject", "to": "draft"},
            ],
        },
    }


def _intent_routing() -> dict[str, Any]:
    """After-sales questions get a drafted reply a person confirms; product questions
    are answered directly; anything else is handed to a person."""
    return {
        "schema_version": 1,
        "env": {"vars": [], "secrets": []},
        "graph": {
            "nodes": [
                {
                    "id": "start",
                    "type": "start",
                    "title": "开始",
                    "inputs": [
                        {
                            "key": "question",
                            "label": "客户问题",
                            "type": "textarea",
                            "required": True,
                        }
                    ],
                },
                {
                    "id": "route",
                    "type": "classifier",
                    "title": "判断问题类型",
                    "model": None,
                    "skills": [],
                    "text": "{{start.question}}",
                    "classes": [
                        {
                            "key": "after_sales",
                            "label": "售后",
                            "description": "退换货、维修、投诉",
                        },
                        {"key": "product", "label": "产品咨询", "description": "功能、价格、规格"},
                    ],
                },
                {
                    "id": "draft",
                    "type": "llm",
                    "title": "起草售后回复",
                    "model": None,
                    "skills": [],
                    "system": "你是售后客服，语气礼貌，先致歉再给出处理办法。",
                    "prompt": "客户问：{{start.question}}\n上次的修改意见：{{review.comment}}",
                    "output": {"kind": "text"},
                },
                {
                    "id": "review",
                    "type": "human",
                    "title": "客服确认",
                    "mode": "approval",
                    "prompt": "确认这条回复吗？",
                    "show": "{{draft.text}}",
                    "options": [
                        {"key": "approve", "label": "确认"},
                        {"key": "reject", "label": "驳回"},
                    ],
                    "comment_required": ["reject"],
                },
                {
                    "id": "answer",
                    "type": "llm",
                    "title": "回答咨询",
                    "model": None,
                    "skills": [],
                    "prompt": "{{start.question}}",
                    "output": {"kind": "text"},
                },
                {
                    "id": "handoff",
                    "type": "template",
                    "title": "提示转人工",
                    "template": "这个问题已转给人工客服。",
                },
                {
                    "id": "end",
                    "type": "end",
                    "title": "结束",
                    "outputs": [
                        {
                            "key": "reply",
                            "from": ["{{draft.text}}", "{{answer.text}}", "{{handoff.text}}"],
                        }
                    ],
                },
            ],
            "edges": [
                {"from": "start", "to": "route"},
                {"from": "route", "branch": "after_sales", "to": "draft"},
                {"from": "route", "branch": "product", "to": "answer"},
                {"from": "route", "branch": "other", "to": "handoff"},
                {"from": "draft", "to": "review"},
                {"from": "review", "branch": "approve", "to": "end"},
                {"from": "review", "branch": "reject", "to": "draft"},
                {"from": "answer", "to": "end"},
                {"from": "handoff", "to": "end"},
            ],
        },
    }


def blank() -> dict[str, Any]:
    """A new app: start → model → end, runnable as it is."""
    return {
        "schema_version": 1,
        "env": {"vars": [], "secrets": []},
        "graph": {
            "nodes": [
                {
                    "id": "start",
                    "type": "start",
                    "title": "开始",
                    "inputs": [
                        {"key": "question", "label": "问题", "type": "textarea", "required": True}
                    ],
                },
                {
                    "id": "answer",
                    "type": "llm",
                    "title": "回答",
                    "model": None,
                    "skills": [],
                    "prompt": "{{start.question}}",
                    "output": {"kind": "text"},
                },
                {
                    "id": "end",
                    "type": "end",
                    "title": "结束",
                    "outputs": [{"key": "answer", "from": ["{{answer.text}}"]}],
                },
            ],
            "edges": [{"from": "start", "to": "answer"}, {"from": "answer", "to": "end"}],
        },
    }


@dataclass(frozen=True)
class Template:
    id: str
    title: str
    summary: str
    config: dict[str, Any]
    needs: tuple[str, ...] = ()

    def describe(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "summary": self.summary,
            "needs": list(self.needs),
            "config": copy.deepcopy(self.config),
        }


TEMPLATES = {
    t.id: t
    for t in (
        Template(
            "qa-approval",
            "问答加确认",
            "模型回答问题，请人确认；驳回时按意见重写。",
            _qa_approval(),
        ),
        Template(
            "intent-routing",
            "意图分类路由",
            "按问题类型分流：售后问题起草回复并请客服确认，产品咨询直接回答，其他问题转人工。",
            _intent_routing(),
        ),
    )
}


@dataclass(frozen=True)
class BuiltinFlow:
    """A flow app the platform ships; its config is the app's version 1."""

    title: str
    summary: str
    config: dict[str, Any]

    def upgrade(self, config: dict[str, Any]) -> dict[str, Any]:
        """A configuration saved before the app was a flow app (a code app's
        ``{"nodes": {...}}``), carried over: model and skills per node."""
        if "graph" in config or "nodes" not in config:
            return config
        out = copy.deepcopy(self.config)
        for node in out["graph"]["nodes"]:
            old = (config.get("nodes") or {}).get(node["id"])
            if old and node["type"] in ("llm", "classifier"):
                node["model"] = old.get("model")
                node["skills"] = list(old.get("skills") or [])
        return out


HELLO = BuiltinFlow("问答示例", "用模型回答一个问题，请人确认；驳回时按意见重写。", _qa_approval())
