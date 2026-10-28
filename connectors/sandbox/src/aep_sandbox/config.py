"""Connector-local Settings, following the platform's AEP_ configuration convention."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AEP_", env_file=".env", extra="ignore")

    sandbox_mcp_host: str = "0.0.0.0"
    sandbox_mcp_port: int = Field(default=8004, ge=1, le=65535)
    # Per run: address space (pandas and numpy take about 150 MB of it on import),
    # result size, kept log tail, files written in the work directory.
    sandbox_memory_mb: int = Field(default=512, ge=128, le=4096)
    sandbox_max_output_bytes: int = Field(default=1 << 20, ge=1024)
    sandbox_max_log_chars: int = Field(default=4000, ge=0)
    sandbox_max_file_bytes: int = Field(default=16 << 20, ge=0)
    sandbox_max_timeout_seconds: int = Field(default=30, ge=1, le=300)
    # Runs at the same time; more wait for a free slot.
    sandbox_parallel: int = Field(default=4, ge=1, le=64)
    # Each run gets a fresh directory under this one, removed afterwards.
    sandbox_work_dir: Path | None = None
    # The relay the API calls (see aep_sandbox.relay), and where it forwards to.
    sandbox_relay_port: int = Field(default=8004, ge=1, le=65535)
    sandbox_relay_target_host: str = "connector-sandbox"
    sandbox_relay_target_port: int = Field(default=8004, ge=1, le=65535)
    sandbox_mcp_allowed_hosts: list[str] = [
        "localhost:*",
        "127.0.0.1:*",
        "[::1]:*",
        "connector-sandbox:*",
        "sandbox-relay:*",
    ]
    sandbox_mcp_allowed_origins: list[str] = [
        "http://localhost:*",
        "http://127.0.0.1:*",
        "http://[::1]:*",
    ]
