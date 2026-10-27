from functools import lru_cache
from pathlib import Path
from typing import Literal

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
    # Shared with the read-only files connector; each upload gets a UUID directory.
    uploads_dir: Path = Path("/data/uploads")
    files_connector_url: str = "http://connector-files:8003"

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
    # An existing (all-purpose) cluster, for users who cannot create clusters.
    databricks_cluster_id: str = ""
    # How generated code reaches the dev target: "pipeline" (a Lakeflow pipeline,
    # deployed with the bundle CLI), "job" (a notebook job on databricks_cluster_id,
    # deployed through the REST API) or "cluster" (the transformations run once on
    # databricks_cluster_id, nothing deployed or written). Empty means "job" when
    # a cluster is given, otherwise "pipeline".
    databricks_deploy: Literal["", "pipeline", "job", "cluster"] = ""
    # The prod target, deployed after a person approves the release of a bundle that
    # passed on dev. Off unless catalog and schema are set, and not in "cluster" mode.
    # The job runs on databricks_prod_cluster_id (empty: databricks_cluster_id) on
    # this Quartz schedule.
    databricks_prod_catalog: str = ""
    databricks_prod_schema: str = ""
    databricks_prod_cluster_id: str = ""
    databricks_prod_schedule: str = "0 0 2 * * ?"
    databricks_prod_timezone: str = "Asia/Shanghai"
    databricks_cli: str = "databricks"
    databricks_timeout_seconds: float = 3600.0

    # The assets repository (aep.assets): bundles that passed on dev are opened there
    # as pull requests under pipelines/<name>/. Off unless repo (owner/name) and token
    # are set; the token needs contents and pull requests read/write on that repo only.
    assets_repo: str = ""
    assets_github_token: str = ""
    assets_base_branch: str = "main"
    assets_api_url: str = "https://api.github.com"
    # Where people open the workbench, for links back to a run (in PRs, for example).
    web_url: str = "http://localhost:3000"

    # Langfuse tracing. Off unless both keys are set; the names are shared with
    # the gateway, which reports model calls to the same project.
    langfuse_public_key: str = Field("", validation_alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str = Field("", validation_alias="LANGFUSE_SECRET_KEY")
    langfuse_host: str = Field("http://localhost:3100", validation_alias="LANGFUSE_HOST")

    @property
    def databricks_deploy_mode(self) -> Literal["pipeline", "job", "cluster"]:
        if self.databricks_deploy:
            return self.databricks_deploy
        return "job" if self.databricks_cluster_id else "pipeline"

    @property
    def databricks_release_blocker(self) -> str | None:
        """Why bundles cannot be released to prod, or None when they can."""
        if not (self.databricks_host and self.databricks_token):
            return "没有配置 Databricks 工作区"
        if self.databricks_deploy_mode == "cluster":
            return "cluster 模式只在集群上试跑、不部署；上线需要 job 或 pipeline 模式"
        if not (self.databricks_prod_catalog and self.databricks_prod_schema):
            return "没有配置 prod 目标（AEP_DATABRICKS_PROD_CATALOG、AEP_DATABRICKS_PROD_SCHEMA）"
        return None


@lru_cache
def get_settings() -> Settings:
    return Settings()
