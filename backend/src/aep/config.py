from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings, read from environment variables prefixed with AEP_."""

    model_config = SettingsConfigDict(
        env_prefix="AEP_", env_file=".env", extra="ignore", populate_by_name=True
    )

    env: str = "local"
    database_url: str = "postgresql://aep:aep@localhost:5432/aep"
    redis_url: str = "redis://localhost:6379/0"
    cors_origins: list[str] = ["http://localhost:3000"]

    # Model gateway (LiteLLM). Every model call goes through it; see infra/litellm.
    llm_gateway_url: str = "http://localhost:4000"
    llm_gateway_key: str = "sk-local-dev"
    default_model: str = "mock"
    llm_timeout_seconds: float = 120.0
    # Each call to a connector (MCP server) tool.
    tool_timeout_seconds: float = 30.0
    # Data sources registered at startup when no source of that name exists yet,
    # as JSON: [{"name": ..., "kind": ..., "connector_url": ..., "description": ...}].
    default_datasources: list[dict[str, str]] = []

    # Databricks dev validation of generated bundles (aep.databricks). Off unless
    # host and token are set. Empty catalog / schema / source_catalog keep the
    # bundle's defaults.
    databricks_host: str = ""
    databricks_token: str = ""
    databricks_catalog: str = ""
    databricks_schema: str = ""
    databricks_source_catalog: str = ""
    # Pipelines run on serverless compute unless a node type or a cluster policy is
    # given; then each update starts a classic job cluster of that type, created
    # under that policy (for users who may create clusters only through one).
    databricks_pipeline_node_type: str = ""
    databricks_pipeline_workers: int = 1
    databricks_pipeline_policy_id: str = ""
    # An existing cluster to run the generated transformations on instead of
    # deploying a pipeline, for users who cannot create pipeline clusters.
    databricks_cluster_id: str = ""
    databricks_cli: str = "databricks"
    databricks_timeout_seconds: float = 3600.0

    # Langfuse tracing. Off unless both keys are set; the names are shared with
    # the gateway, which reports model calls to the same project.
    langfuse_public_key: str = Field("", validation_alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str = Field("", validation_alias="LANGFUSE_SECRET_KEY")
    langfuse_host: str = Field("http://localhost:3100", validation_alias="LANGFUSE_HOST")


@lru_cache
def get_settings() -> Settings:
    return Settings()
