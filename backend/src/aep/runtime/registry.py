"""App registry: each platform app is a named builder that returns a LangGraph graph."""

from collections.abc import Callable
from dataclasses import dataclass

from langgraph.graph import StateGraph
from openai import AsyncOpenAI

from aep.config import Settings


@dataclass(frozen=True)
class AppContext:
    """What an app's nodes may use. Model calls go through ``llm_client`` only."""

    llm_client: AsyncOpenAI
    settings: Settings


AppBuilder = Callable[[AppContext], StateGraph]


def default_apps() -> dict[str, AppBuilder]:
    from aep.runtime.apps import hello

    return {"hello": hello.build}
