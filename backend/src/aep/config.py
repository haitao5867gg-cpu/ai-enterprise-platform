from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime settings, read from environment variables prefixed with AEP_."""

    model_config = SettingsConfigDict(env_prefix="AEP_", env_file=".env", extra="ignore")

    env: str = "local"
    database_url: str = "postgresql://aep:aep@localhost:5432/aep"
    redis_url: str = "redis://localhost:6379/0"
    cors_origins: list[str] = ["http://localhost:3000"]


@lru_cache
def get_settings() -> Settings:
    return Settings()
