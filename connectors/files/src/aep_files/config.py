"""Connector-local Settings, following the platform's AEP_ configuration convention."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AEP_", env_file=".env", extra="ignore")

    uploads_dir: Path = Path("/data/uploads")
    files_stats_sample_size: int = Field(default=1000, ge=1, le=10000)
    files_mcp_host: str = "0.0.0.0"
    files_mcp_port: int = Field(default=8003, ge=1, le=65535)
    files_mcp_allowed_hosts: list[str] = [
        "localhost:*",
        "127.0.0.1:*",
        "[::1]:*",
        "connector-files:*",
    ]
    files_mcp_allowed_origins: list[str] = [
        "http://localhost:*",
        "http://127.0.0.1:*",
        "http://[::1]:*",
    ]
