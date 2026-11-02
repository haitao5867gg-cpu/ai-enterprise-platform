"""All connector configuration is loaded here, using the platform's AEP_ prefix."""

import json
from typing import Annotated, Any, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

Provider = Literal["github", "bitbucket"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AEP_", env_file=".env", extra="ignore")

    # github: GitHub or GitHub Enterprise Server; bitbucket: Bitbucket Data Center / Server.
    git_provider: Provider = "github"
    # The REST API. GitHub: https://api.github.com, GitHub Enterprise:
    # https://ghe.example.com/api/v3. Bitbucket: the server, e.g. https://bitbucket.example.com
    # (the connector adds /rest/api/1.0).
    git_api_url: str = "https://api.github.com"
    # Where to clone from, with {repo} for owner/name (GitHub) or PROJECT/slug (Bitbucket).
    # Empty: worked out from the API address (https://github.com/{repo}.git,
    # <server>/scm/{repo}.git).
    git_clone_url: str = ""
    # GitHub: a fine-grained token with Contents and Pull requests read and write.
    # Bitbucket: an HTTP access token with repository write. Never leaves this container.
    git_token: SecretStr = SecretStr("")
    # The repositories the tools may touch, owner/name or PROJECT/slug, as a JSON list or
    # separated by commas. Empty: every tool refuses.
    git_repos: Annotated[list[str], NoDecode] = []
    # Branches this connector pushes start with this; it never pushes anywhere else.
    git_branch_prefix: str = "aep/"
    git_author_name: str = "AEP"
    git_author_email: str = "aep@localhost"
    # Clones are kept here between calls and fetched again before each one.
    git_work_dir: str = "/tmp/aep-git"
    git_timeout_seconds: int = Field(default=120, ge=5, le=1800)
    git_mcp_host: str = "127.0.0.1"
    git_mcp_port: int = Field(default=8006, ge=1, le=65535)
    git_mcp_allowed_hosts: list[str] = [
        "localhost:*",
        "127.0.0.1:*",
        "[::1]:*",
        "connector-git:*",
    ]
    git_mcp_allowed_origins: list[str] = [
        "http://localhost:*",
        "http://127.0.0.1:*",
        "http://[::1]:*",
    ]

    @field_validator("git_repos", mode="before")
    @classmethod
    def _repos(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = json.loads(value) if value.strip().startswith("[") else value.split(",")
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return value

    def missing(self) -> list[str]:
        """The settings still to fill in before a tool can work."""
        missing = []
        if not self.git_token.get_secret_value():
            missing.append("AEP_GIT_TOKEN")
        if not self.git_repos:
            missing.append("AEP_GIT_REPOS")
        return missing

    def clone_url(self, repo: str) -> str:
        if self.git_clone_url:
            return self.git_clone_url.replace("{repo}", repo)
        api = self.git_api_url.rstrip("/")
        if self.git_provider == "bitbucket":
            return f"{api}/scm/{repo.lower()}.git"
        if api == "https://api.github.com":
            return f"https://github.com/{repo}.git"
        return f"{api.removesuffix('/api/v3')}/{repo}.git"
