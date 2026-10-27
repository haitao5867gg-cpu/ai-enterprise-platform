"""All connector configuration is loaded here, using the platform's AEP_ prefix."""

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def validate_identifier(value: str) -> str:
    """MySQL schema/table/column names have at most 64 BMP characters.

    Quotes, spaces and Unicode are valid; callers MUST still use quote_identifier.
    MySQL disallows supplementary characters and trailing spaces in these names.
    """
    if (
        not value
        or len(value) > 64
        or value.endswith(" ")
        or any(
            ord(char) < 32 or ord(char) > 0xFFFF or 0xD800 <= ord(char) <= 0xDFFF for char in value
        )
    ):
        raise ValueError(
            "MySQL identifiers require 1-64 BMP characters, no controls/trailing spaces"
        )
    return value


def quote_identifier(value: str) -> str:
    """Quote one identifier, including literal backticks; never quote a dotted path."""
    return "`" + validate_identifier(value).replace("`", "``") + "`"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AEP_", env_file=".env", extra="ignore")

    mysql_host: str = "127.0.0.1"
    mysql_port: int = Field(default=3307, ge=1, le=65535)
    mysql_database: str = "sales"
    mysql_user: str = Field(default="source_reader", min_length=1, max_length=32)
    # Synthetic, local-demo credentials only. Override in .env for other sources.
    mysql_password: SecretStr = SecretStr("local-source-reader")
    mysql_connect_timeout_seconds: int = Field(default=5, ge=1, le=60)
    mysql_query_timeout_ms: int = Field(default=3000, ge=1, le=60000)
    mysql_stats_sample_size: int = Field(default=1000, ge=1, le=10000)
    mysql_mcp_host: str = "127.0.0.1"
    mysql_mcp_port: int = Field(default=8002, ge=1, le=65535)
    mysql_mcp_allowed_hosts: list[str] = [
        "localhost:*",
        "127.0.0.1:*",
        "[::1]:*",
        "connector-mysql:*",
    ]
    mysql_mcp_allowed_origins: list[str] = [
        "http://localhost:*",
        "http://127.0.0.1:*",
        "http://[::1]:*",
    ]

    _valid_database = field_validator("mysql_database")(validate_identifier)
