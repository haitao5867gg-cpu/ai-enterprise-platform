"""App registry: each platform app is a named builder that returns a LangGraph graph."""

from collections.abc import Callable
from dataclasses import dataclass, field

from langgraph.graph import StateGraph
from openai import AsyncOpenAI

from aep.config import Settings
from aep.datasources import DataSourceStore, MemoryDataSourceStore
from aep.tools.mcp import McpTools


@dataclass(frozen=True)
class AppContext:
    """What an app's nodes may use.

    Model calls go through ``llm_client`` only; data sources are read through
    their connectors with ``tools`` (look the connector up in ``datasources``).
    """

    llm_client: AsyncOpenAI
    settings: Settings
    datasources: DataSourceStore = field(default_factory=MemoryDataSourceStore)
    tools: McpTools = field(default_factory=McpTools)


AppBuilder = Callable[[AppContext], StateGraph]


def default_apps() -> dict[str, AppBuilder]:
    from aep.runtime.apps import etl, hello

    return {"hello": hello.build, "etl": etl.build}
