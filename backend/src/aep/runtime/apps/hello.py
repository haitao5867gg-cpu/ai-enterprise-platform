"""``hello``: answer with a model, ask the user to approve, redo with their feedback.

The smallest app that exercises everything a real one needs: a model call
through the gateway, a human approval that pauses the run, and a loop back
when the user rejects.
"""

import operator
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from aep import llm
from aep.runtime.registry import AppContext

SYSTEM_PROMPT = "你是一个乐于助人的助手，用中文简洁地回答。"

APPROVAL = {
    "kind": "approval",
    "prompt": "确认这个回答吗？驳回时可以写下修改意见。",
    "options": ["approve", "reject"],
}


class HelloState(TypedDict, total=False):
    input: dict[str, Any]
    messages: Annotated[list[dict[str, str]], operator.add]
    draft: str
    feedback: str | None
    result: dict[str, Any] | None


def build(ctx: AppContext) -> StateGraph:
    async def draft(state: HelloState) -> dict[str, Any]:
        prompt = [{"role": "system", "content": SYSTEM_PROMPT}]
        prompt.append({"role": "user", "content": state["input"].get("text", "")})
        if state.get("feedback"):
            prompt.append({"role": "assistant", "content": state.get("draft", "")})
            prompt.append({"role": "user", "content": f"请根据意见修改：{state['feedback']}"})
        reply = await llm.chat(ctx.llm_client, prompt, model=ctx.settings.default_model)
        new = [{"role": "assistant", "content": reply.text}]
        if not state.get("messages"):
            new.insert(0, {"role": "user", "content": state["input"].get("text", "")})
        return {"draft": reply.text, "messages": new}

    def review(state: HelloState) -> dict[str, Any]:
        answer = interrupt(APPROVAL)
        if answer.get("decision") == "approve":
            return {
                "messages": [{"role": "user", "content": "确认"}],
                "result": {"answer": state["draft"]},
            }
        comment = answer.get("comment") or "请换一种说法"
        return {"messages": [{"role": "user", "content": f"驳回：{comment}"}], "feedback": comment}

    def after_review(state: HelloState) -> str:
        return END if state.get("result") else "draft"

    graph = StateGraph(HelloState)
    graph.add_node("draft", draft)
    graph.add_node("review", review)
    graph.add_edge(START, "draft")
    graph.add_edge("draft", "review")
    graph.add_conditional_edges("review", after_review, ["draft", END])
    return graph
