"""App registry: the code apps (each a named builder that returns a LangGraph graph) and
the flow apps the platform ships."""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from langgraph.graph import StateGraph
from openai import AsyncOpenAI

from aep.assets import AssetRepo, AssetStore, MemoryAssetStore
from aep.config import Settings
from aep.databricks import DevTarget
from aep.datasources import DataSourceStore, MemoryDataSourceStore
from aep.tools.mcp import McpTools


@dataclass(frozen=True)
class AppContext:
    """What an app's nodes may use.

    Model calls go through ``llm_client`` only; data sources are read through
    their connectors with ``tools`` (look the connector up in ``datasources``).
    ``databricks`` is the workspace's dev target, or None when none is configured;
    ``assets`` is the assets repository (None when not configured) and
    ``asset_store`` the index of what went there.
    """

    llm_client: AsyncOpenAI
    settings: Settings
    datasources: DataSourceStore = field(default_factory=MemoryDataSourceStore)
    tools: McpTools = field(default_factory=McpTools)
    databricks: DevTarget | None = None
    assets: AssetRepo | None = None
    asset_store: AssetStore = field(default_factory=MemoryAssetStore)


AppBuilder = Callable[[AppContext], StateGraph]


def default_apps() -> dict[str, Any]:
    """The apps the platform ships: code apps by builder, flow apps by configuration
    (``aep.runtime.flow.builtin.BuiltinFlow``)."""
    from aep.runtime.apps import etl
    from aep.runtime.flow.builtin import HELLO

    return {"hello": HELLO, "etl": etl.build}
