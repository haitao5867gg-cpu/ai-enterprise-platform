"""All connector configuration is loaded here, using the platform's AEP_ prefix."""

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def validate_identifier(value: str) -> str:
    """Accept PostgreSQL identifiers without silent truncation or control characters.

    Quotes, spaces and Unicode are valid: callers still MUST use sql.Identifier.
    Database names are passed as libpq parameters, never interpolated into SQL.
    """
    if not value or len(value.encode("utf-8")) > 63 or any(ord(char) < 32 for char in value):
        raise ValueError("PostgreSQL identifiers must contain 1-63 UTF-8 bytes and no controls")
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AEP_", env_file=".env", extra="ignore")

    postgres_host: str = "127.0.0.1"
    postgres_port: int = Field(default=5433, ge=1, le=65535)
    postgres_database: str = "aep_source"
    postgres_user: str = "source_reader"
    # Synthetic, local-demo credentials only. Override in .env for other sources.
    postgres_password: SecretStr = SecretStr("local-source-reader")
    postgres_connect_timeout_seconds: int = Field(default=5, ge=1, le=60)
    postgres_query_timeout_ms: int = Field(default=3000, ge=1, le=60000)
    postgres_stats_sample_size: int = Field(default=1000, ge=1, le=10000)
    postgres_mcp_host: str = "127.0.0.1"
    postgres_mcp_port: int = Field(default=8001, ge=1, le=65535)
    postgres_mcp_allowed_hosts: list[str] = [
        "localhost:*",
        "127.0.0.1:*",
        "[::1]:*",
        "connector-postgres:*",
    ]
    postgres_mcp_allowed_origins: list[str] = [
        "http://localhost:*",
        "http://127.0.0.1:*",
        "http://[::1]:*",
    ]

    _valid_names = field_validator("postgres_database", "postgres_user")(validate_identifier)
