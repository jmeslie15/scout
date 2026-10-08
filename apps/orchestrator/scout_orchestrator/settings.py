"""Process settings for the orchestrator (spec section 55).

Configuration is read from the environment only, exactly as the API does it: one source
of truth for process configuration means one place to audit, and it keeps the container
image identical between environments.

The GitHub and LLM pass-through fields exist because the orchestrator is a *control plane*
process: it mints the installation token the sandbox's git fetch uses, and it decides
whether a model call is attempted at all. It never passes either credential into a
sandbox, which is what :class:`scout_domain.models.sandbox.SandboxPolicy` enforces.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import Field, computed_field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from scout_domain.enums import JobStatus, SandboxBackendKind
from scout_domain.errors import ConfigurationError
from scout_domain.state.machine import VERIFICATION_PIPELINE
from scout_domain.state.retry import RetryPolicy

if TYPE_CHECKING:
    from scout_orchestrator.stages.context import StageSettings

__all__ = [
    "EXECUTION_MODE_IN_PROCESS",
    "EXECUTION_MODE_WORKER",
    "Settings",
    "get_settings",
    "parse_stage_names",
]

#: The orchestrator executes stages itself. The offline default, and the mode the
#: vertical slice and single-process development use.
EXECUTION_MODE_IN_PROCESS = "in_process"
#: The orchestrator dispatches one message per stage to a ``scout_worker`` process.
EXECUTION_MODE_WORKER = "worker"

_EXECUTION_MODES = frozenset({EXECUTION_MODE_IN_PROCESS, EXECUTION_MODE_WORKER})


def parse_stage_names(raw: str, *, variable: str) -> frozenset[JobStatus]:
    """Parse a comma-separated list of :class:`JobStatus` names.

    Why a bare string rather than a typed list field: the value arrives from the
    environment, where a list is a comma-separated string, and resolving it here means the
    error message can name the offending token and the permitted values. A typo in an
    operator floor must fail loudly at start-up, not silently protect nothing.

    Raises:
        ConfigurationError: when a name is not a ``JobStatus`` member.
    """
    parsed: set[JobStatus] = set()
    unknown: list[str] = []
    for token in raw.split(","):
        cleaned = token.strip().upper()
        if not cleaned:
            continue
        try:
            parsed.add(JobStatus(cleaned))
        except ValueError:
            unknown.append(cleaned)

    if unknown:
        raise ConfigurationError(
            f"{variable} contains unknown job status name(s): {', '.join(sorted(unknown))}",
            variable=variable,
            permitted=", ".join(status.value for status in JobStatus),
        )
    return frozenset(parsed)


class Settings(BaseSettings):
    """Environment-backed settings for the orchestrator."""

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

    #: Public base URL of the control plane, used in links sent to humans.
    public_url: str = "http://localhost:8000"
    #: Dashboard root; the check run's "details" link is built from it.
    dashboard_url: str = "http://localhost:5173"

    database_url: str = "postgresql+asyncpg://scout:scout@localhost:5433/scout"
    redis_url: str = "redis://localhost:6380/0"

    artifact_root: Path = Path("./var/artifacts")
    workspace_root: Path = Path("./var/workspaces")

    # -- Scheduling ----------------------------------------------------------------
    max_concurrent_jobs: int = Field(default=2, ge=1, le=64)
    max_job_runtime_seconds: int = Field(default=1800, ge=60, le=86_400)
    #: How long a claimed job lease stays valid without a refresh. Long enough to cover
    #: a slow stage, short enough that a crashed process's job is reclaimed promptly.
    lease_ttl_seconds: int = Field(default=900, ge=30, le=86_400)
    #: How long ``reserve`` blocks waiting for a message before the loop sweeps leases.
    worker_poll_interval_seconds: float = Field(default=5.0, gt=0.0, le=300.0)
    #: How many messages one reservation may claim. Kept small: the loop is sequential and
    #: a large batch would sit unprocessed while the first job runs.
    reserve_batch_size: int = Field(default=1, ge=1, le=64)
    #: The number of consecutive unreachable-datastore errors tolerated before the loop
    #: exits non-zero so a supervisor can restart it.
    max_consecutive_loop_errors: int = Field(default=10, ge=1, le=1000)

    #: ``in_process`` (the orchestrator runs stages) or ``worker`` (it dispatches them).
    execution_mode: str = EXECUTION_MODE_IN_PROCESS
    #: The stream and consumer group a ``worker`` deployment consumes stage messages from.
    worker_stream: str = "scout:job-stages"
    worker_consumer_group: str = "worker"

    #: Stages the *operator* requires. A repository may not disable these.
    required_stages: str = ""

    # -- Retry (spec section 51) ---------------------------------------------------
    retry_max_attempts: int = Field(default=3, ge=1, le=20)
    retry_base_delay_seconds: float = Field(default=2.0, gt=0.0, le=3600.0)
    retry_max_delay_seconds: float = Field(default=300.0, gt=0.0, le=86_400.0)
    retry_jitter_ratio: float = Field(default=0.15, ge=0.0, lt=1.0)

    # -- Sandbox -------------------------------------------------------------------
    sandbox_backend: str = SandboxBackendKind.DOCKER.value
    sandbox_image: str = "scout-sandbox-python:3.12"
    sandbox_network: str = "none"

    # -- Git -----------------------------------------------------------------------
    #: Base URL the pinned commit is fetched from. Overridable so an offline harness can
    #: point at a local ``file://`` remote without any code change.
    git_base_url: str = "https://github.com"
    #: Installation token used for ``git fetch``. Passed per command, never persisted.
    git_token: str = ""

    # -- GitHub App (pass-through) --------------------------------------------------
    github_app_id: str = ""
    github_app_slug: str = ""
    github_private_key_path: Path | None = None
    github_private_key: str = ""
    github_webhook_secret: str = ""
    github_api_url: str = "https://api.github.com"

    # -- LLM gateway (pass-through) -------------------------------------------------
    llm_default_provider: str = "fake"
    llm_default_model: str = "fake-deterministic"
    #: Whether the repository analyzer may call the model gateway. When the gateway or its
    #: prompt templates are unavailable the deterministic summary is used and the report
    #: says so, so this flag only records the operator's intent.
    llm_analysis_enabled: bool = True
    #: Prompt version requested from the prompt library for repository analysis.
    llm_analyzer_prompt_version: str = "v1"

    # -- Notifications --------------------------------------------------------------
    notifications_enabled: bool = False
    google_chat_webhook_url: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    email_from: str = "scout@edrevel.ai"
    email_to: str = ""

    # -- Observability --------------------------------------------------------------
    otel_enabled: bool = False
    otel_exporter_endpoint: str = "http://localhost:4318"
    otel_service_name: str = "scout-orchestrator"

    @field_validator("execution_mode")
    @classmethod
    def _validate_execution_mode(cls, value: str) -> str:
        """Reject an unknown execution mode at construction, naming the alternatives."""
        resolved = value.strip().lower()
        if resolved not in _EXECUTION_MODES:
            raise ValueError(
                f"unknown execution mode {value!r}; expected one of "
                f"{', '.join(sorted(_EXECUTION_MODES))}"
            )
        return resolved

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

    # -- Derived values -------------------------------------------------------------

    @computed_field  # type: ignore[prop-decorator]
    @property
    def is_production(self) -> bool:
        """Whether this process believes it is running in production."""
        return self.env.lower() in {"production", "prod"}

    @computed_field  # type: ignore[prop-decorator]
    @property
    def github_app_configured(self) -> bool:
        """Whether a real GitHub App is configured rather than the in-memory fake."""
        if not self.github_app_id:
            return False
        return bool(self.github_private_key or self.github_private_key_path)

    @property
    def required_stage_set(self) -> frozenset[JobStatus]:
        """The operator's non-skippable stages, parsed from ``SCOUT_REQUIRED_STAGES``."""
        return parse_stage_names(self.required_stages, variable="SCOUT_REQUIRED_STAGES")

    def retry_policy(self) -> RetryPolicy:
        """Build the retry policy from settings (spec section 51)."""
        return RetryPolicy(
            max_attempts=self.retry_max_attempts,
            base_delay_seconds=self.retry_base_delay_seconds,
            max_delay_seconds=self.retry_max_delay_seconds,
            jitter_ratio=self.retry_jitter_ratio,
        )

    def to_stage_settings(self) -> StageSettings:
        """Project the process settings onto the subset the pipeline stages may see.

        The projection is deliberate rather than passing ``Settings`` straight through:
        a stage has no business reading the database URL or the webhook secret, and a
        narrow value object is how that stays true as settings grow.
        """
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

    # -- Safety ---------------------------------------------------------------------

    def validate_runtime_safety(self) -> None:
        """Refuse configurations that are unsafe in production (Rule 7, spec section 55).

        The orchestrator owns job state, so a mistake here is worse than a mistake in a
        stateless process: it can mark a change verified.

        Raises:
            ConfigurationError: when production is requested without the protections
                production requires.
        """
        # A stage name that is not a JobStatus protects nothing; fail before work starts rather
        # than at the first skip decision.
        _ = self.required_stage_set

        if not self.is_production:
            return

        problems: list[str] = []
        if self.sandbox_backend == SandboxBackendKind.LOCAL.value:
            problems.append(
                "SCOUT_SANDBOX_BACKEND=local executes repository code on the host and is "
                "not permitted in production; use the docker backend"
            )
        if "localhost" in self.database_url or "scout:scout@" in self.database_url:
            problems.append("SCOUT_DATABASE_URL must not use development credentials in production")
        if "localhost" in self.redis_url:
            problems.append("SCOUT_REDIS_URL must not point at localhost in production")
        if self.execution_mode == EXECUTION_MODE_WORKER and not self.worker_consumer_group:
            problems.append("SCOUT_WORKER_CONSUMER_GROUP must be set when dispatching to workers")
        if not self.notifications_enabled:
            problems.append(
                "SCOUT_NOTIFICATIONS_ENABLED must be true in production: an escalation "
                "nobody is told about is an escalation nobody answers"
            )

        if problems:
            raise ConfigurationError(
                "refusing to start with an unsafe production configuration",
                problems="; ".join(problems),
            )

    def pipeline_stage_names(self) -> tuple[str, ...]:
        """The pipeline stage names, for the ``--help`` text and start-up logging."""
        return tuple(stage.value for stage in VERIFICATION_PIPELINE)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process settings singleton."""
    return Settings()
