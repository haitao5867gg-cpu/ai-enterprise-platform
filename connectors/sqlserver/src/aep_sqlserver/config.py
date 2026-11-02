"""All connector configuration is loaded here, using the platform's AEP_ prefix."""

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def validate_identifier(value: str) -> str:
    """SQL Server names (sysname) have at most 128 characters.

    Quotes, spaces and Unicode are valid; callers MUST still use quote_identifier.
    """
    if (
        not value
        or len(value) > 128
        or value != value.strip()
        or any(ord(char) < 32 or 0xD800 <= ord(char) <= 0xDFFF for char in value)
    ):
        raise ValueError(
            "SQL Server identifiers require 1-128 characters, no controls or outer spaces"
        )
    return value


def quote_identifier(value: str) -> str:
    """Quote one identifier with brackets, doubling literal ]; never quote a dotted path."""
    return "[" + validate_identifier(value).replace("]", "]]") + "]"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AEP_", env_file=".env", extra="ignore")

    sqlserver_host: str = "127.0.0.1"
    sqlserver_port: int = Field(default=1433, ge=1, le=65535)
    sqlserver_database: str = "source"
    sqlserver_user: str = Field(default="source_reader", min_length=1, max_length=128)
    # Synthetic, local-demo credentials only. Override in .env for other sources.
    sqlserver_password: SecretStr = SecretStr("Local-Source-Reader1")
    sqlserver_connect_timeout_seconds: int = Field(default=5, ge=1, le=60)
    sqlserver_query_timeout_seconds: int = Field(default=5, ge=1, le=60)
    sqlserver_stats_sample_size: int = Field(default=1000, ge=1, le=10000)
    sqlserver_mcp_host: str = "127.0.0.1"
    sqlserver_mcp_port: int = Field(default=8007, ge=1, le=65535)
    sqlserver_mcp_allowed_hosts: list[str] = [
        "localhost:*",
        "127.0.0.1:*",
        "[::1]:*",
        "connector-sqlserver:*",
    ]
    sqlserver_mcp_allowed_origins: list[str] = [
        "http://localhost:*",
        "http://127.0.0.1:*",
        "http://[::1]:*",
    ]

    _valid_database = field_validator("sqlserver_database")(validate_identifier)
