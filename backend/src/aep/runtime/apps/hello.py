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
from aep.runtime.design import AppDesign, NodeDesign
from aep.runtime.registry import AppContext

SYSTEM_PROMPT = "你是一个乐于助人的助手，用中文简洁地回答。"

DESIGN = AppDesign(
    title="问答示例",
    summary="用模型回答一个问题，请人确认；驳回时按意见重写。",
    pattern="固定流程的工作流：回答、确认，驳回就回到回答。",
    knowledge="没有接知识库，只用模型自身的知识。",
    skills="没有使用 Skill。",
)

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
    graph.add_node(
        "draft",
        draft,
        metadata=NodeDesign(
            title="回答",
            kind="model",
            summary="模型回答用户的问题；被驳回时带着意见重写。",
            inputs="问题；驳回时还有上一版回答和意见",
            outputs="回答",
            prompt=SYSTEM_PROMPT,
        ).metadata(),
    )
    graph.add_node(
        "review",
        review,
        metadata=NodeDesign(
            title="确认",
            kind="human",
            summary="运行暂停，等人确认回答；驳回就回到“回答”。",
            inputs="回答",
            outputs="确认或驳回的决定",
        ).metadata(),
    )
    graph.add_edge(START, "draft")
    graph.add_edge("draft", "review")
    graph.add_conditional_edges("review", after_review, ["draft", END])
    return graph
