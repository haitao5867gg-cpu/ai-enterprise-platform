"""Connector-local Settings, following the platform's AEP_ configuration convention."""

from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AEP_", env_file=".env", extra="ignore")

    # The media volume, mounted where the platform mounts it (docs/api/media.md).
    media_dir: Path = Path("/data/media")
    stock_mcp_host: str = "0.0.0.0"
    stock_mcp_port: int = Field(default=8009, ge=1, le=65535)
    # Only in the server's .env, never in code or chat. Without it the connector starts
    # and every tool call says the key is missing.
    stock_pixabay_key: SecretStr = SecretStr("")
    # Points at a fake service in tests.
    stock_pixabay_url: str = "https://pixabay.com/api/videos/"
    # Pixabay asks for its answers to be cached for 24 hours; downloaded files and
    # thumbnails are kept as long.
    stock_cache_hours: float = Field(default=24, gt=0)
    # How long a download is waited for before it is handed back as a job: under the
    # platform's tool call timeout (30 s by default).
    stock_wait_seconds: float = Field(default=15, ge=0, le=25)
    stock_max_mb: int = Field(default=300, ge=1, le=4096)
    # Downloads at the same time; more wait their turn.
    stock_max_downloads: int = Field(default=3, ge=1, le=16)
    stock_mcp_allowed_hosts: list[str] = [
        "localhost:*",
        "127.0.0.1:*",
        "[::1]:*",
        "connector-stock:*",
    ]
    stock_mcp_allowed_origins: list[str] = [
        "http://localhost:*",
        "http://127.0.0.1:*",
        "http://[::1]:*",
    ]
