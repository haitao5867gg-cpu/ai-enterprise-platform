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

    # Langfuse tracing. Off unless both keys are set; the names are shared with
    # the gateway, which reports model calls to the same project.
    langfuse_public_key: str = Field("", validation_alias="LANGFUSE_PUBLIC_KEY")
    langfuse_secret_key: str = Field("", validation_alias="LANGFUSE_SECRET_KEY")
    langfuse_host: str = Field("http://localhost:3100", validation_alias="LANGFUSE_HOST")


@lru_cache
def get_settings() -> Settings:
    return Settings()
