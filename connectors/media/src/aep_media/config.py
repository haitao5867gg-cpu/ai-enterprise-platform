"""Connector-local Settings, following the platform's AEP_ configuration convention."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AEP_", env_file=".env", extra="ignore")

    # The media volume, mounted where the platform mounts it (docs/api/media.md).
    media_dir: Path = Path("/data/media")
    media_mcp_host: str = "0.0.0.0"
    media_mcp_port: int = Field(default=8008, ge=1, le=65535)
    # Renders at the same time; more wait in a queue.
    media_max_jobs: int = Field(default=1, ge=1, le=16)
    # How long checking a render's sources may take: the job must be handed back
    # before the platform's tool call (30 s by default) gives up.
    media_check_seconds: float = Field(default=20, gt=0, le=600)
    # The longest video a render may make.
    media_max_seconds: int = Field(default=600, ge=1, le=7200)
    # Work files, cached shots and job records older than this are removed.
    media_work_hours: float = Field(default=24, gt=0)
    # ffmpeg's own threads per process; 0 lets it pick.
    media_ffmpeg_threads: int = Field(default=0, ge=0, le=64)
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    media_mcp_allowed_hosts: list[str] = [
        "localhost:*",
        "127.0.0.1:*",
        "[::1]:*",
        "connector-media:*",
    ]
    media_mcp_allowed_origins: list[str] = [
        "http://localhost:*",
        "http://127.0.0.1:*",
        "http://[::1]:*",
    ]
