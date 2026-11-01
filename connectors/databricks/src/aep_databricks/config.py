"""All connector configuration is loaded here, using the platform's AEP_ prefix."""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AEP_", env_file=".env", extra="ignore")

    # The workspace, e.g. https://adb-1234.5.databricks.azure.cn. Empty: not configured.
    databricks_read_host: str = ""
    # A token whose identity has only USE CATALOG, USE SCHEMA and SELECT: the real boundary.
    databricks_read_token: SecretStr = SecretStr("")
    # Where statements run: a SQL warehouse when set, else an existing, running cluster.
    databricks_read_warehouse_id: str = ""
    databricks_read_cluster_id: str = ""
    # The catalogs the tools may read (JSON list). Empty: every tool refuses.
    databricks_read_catalogs: list[str] = []
    databricks_read_timeout_seconds: int = Field(default=150, ge=5, le=3600)
    databricks_read_stats_sample: int = Field(default=1000, ge=1, le=10000)
    databricks_mcp_host: str = "127.0.0.1"
    databricks_mcp_port: int = Field(default=8005, ge=1, le=65535)
    databricks_mcp_allowed_hosts: list[str] = [
        "localhost:*",
        "127.0.0.1:*",
        "[::1]:*",
        "connector-databricks:*",
    ]
    databricks_mcp_allowed_origins: list[str] = [
        "http://localhost:*",
        "http://127.0.0.1:*",
        "http://[::1]:*",
    ]

    def missing(self) -> list[str]:
        """The settings still to fill in before a statement can run."""
        missing = []
        if not self.databricks_read_host.strip():
            missing.append("AEP_DATABRICKS_READ_HOST")
        if not self.databricks_read_token.get_secret_value():
            missing.append("AEP_DATABRICKS_READ_TOKEN")
        if not (self.databricks_read_warehouse_id or self.databricks_read_cluster_id):
            missing.append("AEP_DATABRICKS_READ_WAREHOUSE_ID 或 AEP_DATABRICKS_READ_CLUSTER_ID")
        if not self.databricks_read_catalogs:
            missing.append("AEP_DATABRICKS_READ_CATALOGS")
        return missing
