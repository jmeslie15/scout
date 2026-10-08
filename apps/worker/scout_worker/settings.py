"""Process settings for the worker (spec section 55).

The worker is an execution-plane process: it legitimately holds Scout's datastore credentials
because it persists the results it produces, and it holds the GitHub identity it needs to fetch a
pinned commit. It holds **no** production credentials, because Scout configures none (Rule 7).

What it deliberately does not have: the webhook secret (it never receives a webhook), the
notification settings (it never notifies), and the operator's required-stage list (it does not
decide what runs). Configuration it cannot use is configuration it should not be able to read.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from scout_domain.enums import SandboxBackendKind
from scout_domain.errors import ConfigurationError

if TYPE_CHECKING:
    from scout_orchestrator.stages.context import StageSettings


__all__ = ["Settings", "get_settings"]


class Settings(BaseSettings):
    """Environment-backed settings for the execution plane."""

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

    database_url: str = "postgresql+asyncpg://scout:scout@localhost:5433/scout"
    redis_url: str = "redis://localhost:6380/0"

    artifact_root: Path = Path("./var/artifacts")
    workspace_root: Path = Path("./var/workspaces")

    dashboard_url: str = "http://localhost:5173"

    #: Stage messages are consumed from their own stream so the worker never competes with the
    #: orchestrator for scheduling messages.
    worker_stream: str = "scout:job-stages"
    worker_consumer_group: str = "worker"
    #: How long a single stage may hold the job lease without a refresh.
    lease_ttl_seconds: int = Field(default=900, ge=30, le=86_400)
    #: How long ``reserve`` blocks before the loop checks for a stop request.
    poll_interval_seconds: float = Field(default=5.0, gt=0.0, le=300.0)
    max_job_runtime_seconds: int = Field(default=1800, ge=60, le=86_400)

    # -- Sandbox -------------------------------------------------------------------
    sandbox_backend: str = SandboxBackendKind.DOCKER.value
    sandbox_image: str = "scout-sandbox-python:3.12"
    sandbox_network: str = "none"

    # -- Git -----------------------------------------------------------------------
    git_base_url: str = "https://github.com"
    git_token: str = ""

    # -- GitHub App (pass-through) --------------------------------------------------
    github_app_id: str = ""
    github_private_key_path: Path | None = None
    github_private_key: str = ""
    github_api_url: str = "https://api.github.com"

    # -- LLM gateway (pass-through) -------------------------------------------------
    llm_default_provider: str = "fake"
    llm_default_model: str = "fake-deterministic"
    llm_analysis_enabled: bool = True
    llm_analyzer_prompt_version: str = "v1"

    # -- Observability --------------------------------------------------------------
    otel_enabled: bool = False
    otel_service_name: str = "scout-worker"

    @field_validator("sandbox_backend")
    @classmethod
    def _validate_backend(cls, value: str) -> str:
        """Reject an unknown sandbox backend at construction."""
        resolved = value.strip().lower()
        if resolved not in {kind.value for kind in SandboxBackendKind}:
            raise ValueError(
                f"unknown sandbox backend {value!r}; expected one of "
                f"{', '.join(kind.value for kind in SandboxBackendKind)}"
            )
        return resolved

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        """Whether this process believes it is running in production."""
        return self.env.lower() in {"production", "prod"}

    def to_stage_settings(self) -> StageSettings:
        """Project the process settings onto the subset the pipeline stages may see."""
        from scout_orchestrator.stages.context import StageSettings

        return StageSettings(
            dashboard_url=self.dashboard_url,
            workspace_root=self.workspace_root,
            artifact_root=self.artifact_root,
            sandbox_backend=self.sandbox_backend,
            sandbox_image=self.sandbox_image,
            sandbox_network=self.sandbox_network,
            git_base_url=self.git_base_url,
            git_token=self.git_token or None,
            llm_analysis_enabled=self.llm_analysis_enabled,
            llm_analyzer_prompt_version=self.llm_analyzer_prompt_version,
            llm_provider=self.llm_default_provider,
            llm_model=self.llm_default_model,
        )

    def validate_runtime_safety(self) -> None:
        """Refuse configurations that are unsafe in production (Rule 7, spec section 55).

        The worker is the process that runs untrusted code, so its checks are about isolation
        rather than about authorization.

        Raises:
            ConfigurationError: when production is requested with host execution or development
                datastore credentials.
        """
        if not self.is_production:
            return

        problems: list[str] = []
        if self.sandbox_backend == SandboxBackendKind.LOCAL.value:
            problems.append(
                "SCOUT_SANDBOX_BACKEND=local executes repository code on the worker host and is "
                "not permitted in production; use the docker backend"
            )
        if "localhost" in self.database_url or "scout:scout@" in self.database_url:
            problems.append("SCOUT_DATABASE_URL must not use development credentials in production")
        if "localhost" in self.redis_url:
            problems.append("SCOUT_REDIS_URL must not point at localhost in production")

        if problems:
            raise ConfigurationError(
                "refusing to start with an unsafe production configuration",
                problems="; ".join(problems),
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process settings singleton."""
    return Settings()
