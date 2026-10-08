"""The stage contract: what a stage receives, and what it must return.

Why this module exists rather than passing loose arguments around: the pipeline has seven
stages that share a handful of collaborators, and a stage that reaches for the database is
a stage that cannot be tested without one. :class:`StageContext` is the complete input and
:class:`StageResult` the complete output, so a stage is a pure function of persisted facts
and an isolated sandbox.

Two deliberate exclusions from :class:`StageSettings`:

* **No datastore URL and no webhook secret.** A stage never writes to PostgreSQL. The
  orchestrator and the worker own persistence, which is what keeps "one code path produces
  identical rows whether a stage ran in-process or in a worker".
* **No sandbox policy override.** Sandbox limits and network posture come from the
  platform factory, clamped by the repository configuration, and never from a stage.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from scout_domain.enums import JobStatus
from scout_domain.errors import ScoutDomainError
from scout_domain.models.job import JobStageOutcome
from scout_orchestrator.stages.artifacts import RecordingArtifactStore

if TYPE_CHECKING:
    from collections.abc import Mapping

    from scout_domain.config import ScoutRepositoryConfig
    from scout_domain.models.agent import AgentRun
    from scout_domain.models.artifact import Artifact
    from scout_domain.models.finding import Finding
    from scout_domain.models.job import VerificationJob
    from scout_domain.models.question import Question
    from scout_domain.models.report import (
        CriterionVerification,
        ReportSection,
        TestOutcomeSummary,
    )
    from scout_domain.models.repository import ChangeSet, RepositorySummary
    from scout_domain.models.tool import ToolResult
    from scout_domain.risk import RiskAssessment
    from scout_github import GitHubClient
    from scout_sandbox import Sandbox, Workspace
    from scout_tools.registry import ToolRegistry

__all__ = [
    "BaseStage",
    "StageContext",
    "StageInputs",
    "StageResult",
    "StageSettings",
]


@dataclass(frozen=True, slots=True, kw_only=True)
class StageSettings:
    """The process settings a pipeline stage is allowed to see.

    A narrow projection of the orchestrator's ``Settings`` (and the worker's) so that a
    stage cannot read configuration it has no business reading, and so that both apps can
    construct the same object for the shared stage implementations.
    """

    dashboard_url: str = "http://localhost:5173"
    workspace_root: Path = Path("./var/workspaces")
    artifact_root: Path = Path("./var/artifacts")
    sandbox_backend: str = "docker"
    sandbox_image: str = "scout-sandbox-python:3.12"
    sandbox_network: str = "none"
    #: Where the pinned commit is fetched from; a ``file://`` URL in an offline harness.
    git_base_url: str = "https://github.com"
    #: Per-command git credential. Never persisted, never placed in a sandbox environment.
    git_token: str | None = None
    #: Whether the repository analyzer may call the model gateway.
    llm_analysis_enabled: bool = True
    llm_analyzer_prompt_version: str = "v1"
    llm_provider: str = "fake"
    llm_model: str = "fake-deterministic"


@dataclass(frozen=True, slots=True, kw_only=True)
class StageInputs:
    """Everything earlier stages established, reconstructed from persisted state.

    Rule 5 ("resumable; no in-memory-only state") is why this is a value object rebuilt
    from the database and the artifact store rather than a mutable accumulator carried in a
    closure: after a crash the orchestrator reloads exactly this and resumes.
    """

    #: Produced by PREPARING and re-read from the ``REPOSITORY_SUMMARY`` artifact.
    change_set: ChangeSet | None = None
    repository_summary: RepositorySummary | None = None
    risk: RiskAssessment | None = None
    repository_config: ScoutRepositoryConfig | None = None
    #: The PR description and the requirement material extracted from it.
    pr_body: str = ""
    requirement_text: str = ""
    commit_messages: tuple[str, ...] = ()

    #: Accumulated evidence, reloaded from ``tool_runs`` / ``findings`` rows.
    tool_results: tuple[ToolResult, ...] = ()
    #: Artifact locators recorded for this job. Carried because a later stage may need to read
    #: an earlier stage's bytes -- FEATURE_VERIFYING reads the JUnit report to know which tests
    #: passed -- and the locator is what makes the blob addressable.
    artifacts: tuple[Artifact, ...] = ()
    findings: tuple[Finding, ...] = ()
    sections: tuple[ReportSection, ...] = ()
    criteria: tuple[CriterionVerification, ...] = ()
    tests: TestOutcomeSummary | None = None
    architecture_impact: str = ""
    agent_provenance: Mapping[str, str] = field(default_factory=dict)
    known_limitations: tuple[str, ...] = ()
    #: Repository-configuration problems found while preparing, reported in the report.
    config_findings: tuple[Finding, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class StageContext:
    """The complete input to one stage run."""

    job: VerificationJob
    stage: JobStatus
    workspace: Workspace
    sandbox: Sandbox
    #: Always the recording wrapper, so a stage can hand back the artifact records it
    #: produced alongside the results that reference them.
    artifacts: RecordingArtifactStore
    tools: ToolRegistry
    github: GitHubClient
    settings: StageSettings
    inputs: StageInputs
    #: The sandbox-run row this execution belongs to, recorded before the stage started.
    sandbox_run_id: str

    @property
    def stage_name(self) -> str:
        """The stage name as it is recorded on tool runs and artifacts."""
        return self.stage.value

    def require_change_set(self) -> ChangeSet:
        """Return the prepared change set, or fail loudly.

        Raises:
            StageInputError: when the stage ran without PREPARING having produced one. A
                stage that silently analysed nothing would produce a plausible, wrong
                report, which is the failure mode Rule 12 exists to prevent.
        """
        if self.inputs.change_set is None:
            raise StageInputError(
                f"{self.stage.value} requires the prepared change set",
                stage=self.stage.value,
            )
        return self.inputs.change_set

    def require_repository_summary(self) -> RepositorySummary:
        """Return the repository summary, or fail loudly."""
        if self.inputs.repository_summary is None:
            raise StageInputError(
                f"{self.stage.value} requires the repository summary",
                stage=self.stage.value,
            )
        return self.inputs.repository_summary

    def require_repository_config(self) -> ScoutRepositoryConfig:
        """Return the repository configuration, or fail loudly."""
        if self.inputs.repository_config is None:
            raise StageInputError(
                f"{self.stage.value} requires the repository configuration",
                stage=self.stage.value,
            )
        return self.inputs.repository_config

    @property
    def changed_paths(self) -> tuple[str, ...]:
        """The paths this pull request changed, in the order the change set lists them."""
        change_set = self.inputs.change_set
        return tuple(change_set.changed_paths) if change_set is not None else ()


class StageInputError(ScoutDomainError):
    """A stage was invoked without the inputs its contract requires."""

    def __init__(self, message: str, **context: Any) -> None:
        """Build the error with structured context naming the stage."""
        super().__init__(message, **context)


@dataclass(frozen=True, slots=True, kw_only=True)
class StageResult:
    """The complete output of one stage run.

    ``tool_results`` and ``artifacts`` carry the *records*, not only their ids, because the
    caller owns persistence: the stage stored bytes in the artifact store and the
    orchestrator/worker writes the rows. Splitting it that way is what lets the same stage
    code run inside the orchestrator process or inside a worker.

    ``sections`` exists beyond the listed contract because spec section 21 requires a tool
    that could not run to surface as an ``UNVERIFIED`` report section rather than as a
    finding about the pull request; sections are how that becomes visible in the report.
    """

    stage: JobStatus
    outcome: JobStageOutcome
    tool_results: tuple[ToolResult, ...] = ()
    findings: tuple[Finding, ...] = ()
    questions: tuple[Question, ...] = ()
    artifacts: tuple[Artifact, ...] = ()
    criteria: tuple[CriterionVerification, ...] = ()
    sections: tuple[ReportSection, ...] = ()
    agent_runs: tuple[AgentRun, ...] = ()
    tests: TestOutcomeSummary | None = None
    narrative: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)
    error: str | None = None

    @property
    def artifact_ids(self) -> tuple[str, ...]:
        """The ids of every artifact this stage produced, in creation order."""
        return tuple(artifact.id for artifact in self.artifacts)

    @property
    def succeeded(self) -> bool:
        """Whether the stage ran to completion."""
        return self.outcome is JobStageOutcome.SUCCEEDED

    @classmethod
    def succeeded_for(
        cls,
        stage: JobStatus,
        *,
        narrative: str = "",
        tool_results: tuple[ToolResult, ...] = (),
        findings: tuple[Finding, ...] = (),
        questions: tuple[Question, ...] = (),
        artifacts: tuple[Artifact, ...] = (),
        criteria: tuple[CriterionVerification, ...] = (),
        sections: tuple[ReportSection, ...] = (),
        tests: TestOutcomeSummary | None = None,
        agent_runs: tuple[AgentRun, ...] = (),
        payload: Mapping[str, Any] | None = None,
    ) -> StageResult:
        """Build a successful result. The common case, so it gets a named constructor."""
        return cls(
            stage=stage,
            outcome=JobStageOutcome.SUCCEEDED,
            tool_results=tool_results,
            findings=findings,
            questions=questions,
            artifacts=artifacts,
            criteria=criteria,
            sections=sections,
            tests=tests,
            agent_runs=agent_runs,
            narrative=narrative,
            payload=payload or {},
        )

    @classmethod
    def failed_for(cls, stage: JobStatus, *, error: str, narrative: str = "") -> StageResult:
        """Build a failed result, which must record the error (Rule 12)."""
        return cls(
            stage=stage,
            outcome=JobStageOutcome.FAILED,
            error=error,
            narrative=narrative or error,
        )


class BaseStage(abc.ABC):
    """One pipeline stage.

    ``stage`` is a class variable rather than a constructor argument so a stage's identity
    is a property of its type: the registry cannot pair a stage object with the wrong
    status by mistake.
    """

    stage: ClassVar[JobStatus]

    @abc.abstractmethod
    async def run(self, ctx: StageContext) -> StageResult:
        """Execute the stage and return a complete, contract-valid result."""
