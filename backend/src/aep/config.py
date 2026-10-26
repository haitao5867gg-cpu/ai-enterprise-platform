from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings, read from environment variables prefixed with AEP_."""

    model_config = SettingsConfigDict(env_prefix="AEP_", env_file=".env", extra="ignore")

    env: str = "local"
    database_url: str = "postgresql://aep:aep@localhost:5432/aep"
    redis_url: str = "redis://localhost:6379/0"
    cors_origins: list[str] = ["http://localhost:3000"]

    # Model gateway (LiteLLM). Every model call goes through it; see infra/litellm.
    llm_gateway_url: str = "http://localhost:4000"
    llm_gateway_key: str = "sk-local-dev"
    default_model: str = "mock"
    llm_timeout_seconds: float = 120.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
