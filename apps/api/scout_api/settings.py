"""Process settings for the API (spec section 55: configuration, never hardcoded secrets).

Settings are read from the environment only. There is deliberately no YAML or TOML
configuration file: one source of truth for process configuration means one place to audit,
and it makes the container image identical between environments.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from pydantic import Field, computed_field
from pydantic_settings import BaseSettings, SettingsConfigDict

from scout_domain.errors import ConfigurationError

__all__ = ["Settings", "get_settings"]


class Settings(BaseSettings):
    """Environment-backed settings for the control-plane API."""

    model_config = SettingsConfigDict(
        env_prefix="SCOUT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    env: str = "development"
    log_level: str = "INFO"
    log_json: bool = False
    version: str = "0.1.0"

    #: Public base URL of this API, used in links sent to GitHub and to humans.
    public_url: str = "http://localhost:8000"
    #: Base URL of the dashboard, used for the check run's "details" link.
    dashboard_url: str = "http://localhost:5173"

    database_url: str = "postgresql+asyncpg://scout:scout@localhost:5433/scout"
    redis_url: str = "redis://localhost:6380/0"

    artifact_root: Path = Path("./var/artifacts")
    workspace_root: Path = Path("./var/workspaces")

    # -- GitHub App ---------------------------------------------------------------
    github_app_id: str = ""
    github_app_slug: str = ""
    github_private_key_path: Path | None = None
    github_webhook_secret: str = ""
    github_api_url: str = "https://api.github.com"

    # -- Concurrency limits (spec section 52) -------------------------------------
    max_concurrent_jobs: int = Field(default=2, ge=1, le=64)
    max_job_runtime_seconds: int = Field(default=1800, ge=60, le=86_400)

    # -- Webhooks -----------------------------------------------------------------
    #: When true, a delivery whose signature cannot be verified is rejected. Disabling
    #: this is only legitimate in a local test harness and is refused in production.
    webhook_require_signature: bool = True

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        return self.env.lower() in {"production", "prod"}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def github_app_configured(self) -> bool:
        """Whether a real GitHub App is configured.

        When false the API runs with the in-memory fake client so the whole platform stays
        exercisable offline. That is the shipped default.
        """
        if not self.github_app_id:
            return False
        if self.github_private_key_path is not None:
            return True
        return bool(os.environ.get("SCOUT_GITHUB_PRIVATE_KEY"))

    def validate_runtime_safety(self) -> None:
        """Refuse configurations that are unsafe in production (Rule 7, spec section 55).

        Raises:
            ConfigurationError: when production is requested without the secrets and
                verification that production requires.
        """
        if not self.is_production:
            return

        problems: list[str] = []
        if not self.github_webhook_secret:
            problems.append("SCOUT_GITHUB_WEBHOOK_SECRET must be set in production")
        if not self.webhook_require_signature:
            problems.append("SCOUT_WEBHOOK_REQUIRE_SIGNATURE=false is not permitted in production")
        if not self.github_app_configured:
            problems.append("a GitHub App must be configured in production")
        if "localhost" in self.database_url or "scout:scout@" in self.database_url:
            problems.append("SCOUT_DATABASE_URL must not use development credentials in production")

        if problems:
            raise ConfigurationError(
                "refusing to start with an unsafe production configuration",
                problems="; ".join(problems),
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process settings singleton.

    Cached so that the same values are used by the app factory, the readiness probe, and
    every request. Tests override this with ``app.dependency_overrides``.
    """
    return Settings()
