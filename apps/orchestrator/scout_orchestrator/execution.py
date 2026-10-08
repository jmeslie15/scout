"""The shared stage-execution lifecycle.

Both execution planes need the same seven steps: create a fresh workspace, build the sandbox
from configuration clamped by the repository's own request, start it, run exactly one stage,
tear both down unconditionally, and report what happened. Writing that twice would be the
classic place for the two planes to drift -- one of them forgetting the teardown, or recording
the sandbox run only on the happy path.

So it lives here, and both the orchestrator's in-process runner and the worker's executor are
thin wrappers that add their own persistence. That also means this module is the *only* place a
workspace or sandbox is created for a pipeline stage, which is the property that makes the
isolation story reviewable.

Cleanup never depends on the stage being well behaved. ``stage_environment`` is an async
context manager, so the sandbox is stopped and the workspace removed on the exception path as
well as the success path, and a process that dies outright leaves a workspace that the next
attempt reclaims (``Workspace.create`` wipes any existing directory for the same job id).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from scout_domain.enums import JobStatus, SandboxBackendKind
from scout_domain.models.base import utcnow
from scout_domain.models.job import JobStageOutcome
from scout_domain.models.sandbox import (
    ResourceLimits,
    SandboxPolicy,
    SandboxRun,
    SandboxStatus,
)
from scout_domain.models.tool import ToolResult
from scout_observability import get_logger, instrument_span
from scout_orchestrator.stages import (
    StageContext,
    StageInputs,
    StageResult,
    StageSettings,
    build_stage,
)
from scout_orchestrator.stages.artifacts import RecordingArtifactStore
from scout_sandbox import (
    Sandbox,
    Workspace,
    build_policy,
    build_resource_limits,
    create_sandbox,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator
    from datetime import datetime

    from scout_domain.config import ScoutRepositoryConfig
    from scout_domain.models.job import VerificationJob
    from scout_github import GitHubClient
    from scout_tools.artifacts import ArtifactStore
    from scout_tools.registry import ToolRegistry

__all__ = [
    "StageEnvironment",
    "StageExecution",
    "execute_stage",
    "run_stage_in_environment",
    "sandbox_limits",
    "sandbox_policy",
    "stage_environment",
]

_log = get_logger(__name__)

#: The stage's own name in traces, kept separate from the tool-level spans so a trace reader can
#: see stage boundaries at a glance.
_SPAN_NAME: Final = "orchestrator.stage"


@dataclass(frozen=True, slots=True)
class StageEnvironment:
    """The isolated resources one stage runs inside."""

    workspace: Workspace
    sandbox: Sandbox
    artifacts: RecordingArtifactStore
    #: When the sandbox was started. Carried so the ``SandboxRun`` record can report a real
    #: duration rather than one measured from the moment the record happened to be built.
    started_at: datetime


@dataclass(frozen=True, slots=True)
class StageExecution:
    """The outcome of one stage run, including the sandbox record it must be filed with.

    ``error`` carries the original exception when the stage raised. It is returned rather than
    re-raised so the caller can persist the failed ``JobStage`` and the ``SandboxRun`` *before*
    deciding whether the failure is retryable -- the record must exist either way (Rule 12), and
    retry classification must see the original type rather than a wrapper.
    """

    result: StageResult
    sandbox_run: SandboxRun
    error: BaseException | None = None

    @property
    def succeeded(self) -> bool:
        """Whether the stage completed without raising."""
        return self.error is None


@asynccontextmanager
async def stage_environment(
    *,
    job_id: str,
    settings: StageSettings,
    sandbox_run_id: str,
    repository_config: ScoutRepositoryConfig | None = None,
    artifact_store: ArtifactStore | None = None,
) -> AsyncIterator[StageEnvironment]:
    """Create and always dispose of a workspace and sandbox for one stage.

    ``repository_config`` is the configuration read during PREPARING, when it is known. It can
    only *lower* the sandbox limits: :func:`scout_sandbox.build_resource_limits` clamps a
    repository request against the operator's ceiling, so a pull request can never widen its own
    budget (spec section 52).
    """
    workspace = Workspace.create(settings.workspace_root, job_id=job_id)
    started_at = utcnow()
    limits = sandbox_limits(settings, repository_config=repository_config)
    policy = sandbox_policy(settings)
    sandbox = create_sandbox(
        job_id=job_id,
        workspace=workspace,
        run_id=sandbox_run_id,
        backend=settings.sandbox_backend,
        limits=limits,
        policy=policy,
        image=settings.sandbox_image,
    )
    await sandbox.start()
    try:
        yield StageEnvironment(
            workspace=workspace,
            sandbox=sandbox,
            started_at=started_at,
            artifacts=RecordingArtifactStore(
                artifact_store if artifact_store is not None else sandbox_backed_store(settings)
            ),
        )
    finally:
        try:
            await sandbox.stop()
        finally:
            workspace.close()


def sandbox_limits(
    settings: StageSettings,
    *,
    repository_config: ScoutRepositoryConfig | None = None,
) -> ResourceLimits:
    """Return the resource limits the sandbox for a stage will be built with.

    Exposed so the caller that records the ``SandboxRun`` *before* the sandbox exists computes the
    same snapshot the sandbox itself uses. Two call sites of one function cannot drift; two
    hand-written copies of the same expression eventually would.

    ``settings`` is accepted for symmetry with :func:`sandbox_policy` and because the environment
    variables the factory reads are part of the same process configuration; a deployment that moves
    limits into ``Settings`` will need it here rather than at every call site.
    """
    del settings
    return build_resource_limits(repository_config=repository_config)


def sandbox_policy(settings: StageSettings) -> SandboxPolicy:
    """Return the network and isolation policy the sandbox for a stage will be built with."""
    return build_policy(network_mode=settings.sandbox_network)


def sandbox_backed_store(settings: StageSettings) -> ArtifactStore:
    """Return the artifact store the stage writes bytes into.

    Imported lazily so that a caller which only wants the sandbox lifecycle does not pull the
    artifact store in, and so that a future object-store backend is a change here alone.
    """
    from scout_tools.artifacts import LocalArtifactStore

    return LocalArtifactStore(settings.artifact_root)


async def run_stage_in_environment(
    environment: StageEnvironment,
    *,
    job: VerificationJob,
    stage: JobStatus,
    inputs: StageInputs,
    settings: StageSettings,
    tools: ToolRegistry,
    github: GitHubClient,
    sandbox_run_id: str,
) -> StageResult:
    """Build the stage's context and run it. Exceptions propagate to the caller."""
    context = StageContext(
        job=job,
        stage=stage,
        workspace=environment.workspace,
        sandbox=environment.sandbox,
        artifacts=environment.artifacts,
        tools=tools,
        github=github,
        settings=settings,
        inputs=inputs,
        sandbox_run_id=sandbox_run_id,
    )
    return await build_stage(stage).run(context)


async def execute_stage(
    *,
    job: VerificationJob,
    stage: JobStatus,
    inputs: StageInputs,
    settings: StageSettings,
    tools: ToolRegistry,
    github: GitHubClient,
    sandbox_run_id: str,
    artifact_store: ArtifactStore | None = None,
) -> StageExecution:
    """Run one stage inside a disposable environment and report both outcomes.

    Never raises for a stage failure: the failure is recorded on the returned
    :class:`StageExecution` alongside the ``SandboxRun`` and the original exception, so the
    caller can persist the record and *then* classify the failure. ``BaseException`` (a
    cancellation or an interpreter shutdown) is deliberately not caught here -- those must
    unwind to the process's shutdown path, which has its own bounded grace period.
    """
    repository_config = inputs.repository_config
    with instrument_span(
        _SPAN_NAME,
        attributes={
            "job.id": job.id,
            "job.stage": stage.value,
            "sandbox.run_id": sandbox_run_id,
        },
    ):
        async with stage_environment(
            job_id=job.id,
            settings=settings,
            sandbox_run_id=sandbox_run_id,
            repository_config=repository_config,
            artifact_store=artifact_store,
        ) as environment:
            try:
                result = await run_stage_in_environment(
                    environment,
                    job=job,
                    stage=stage,
                    inputs=inputs,
                    settings=settings,
                    tools=tools,
                    github=github,
                    sandbox_run_id=sandbox_run_id,
                )
            except Exception as exc:
                _log.error(
                    "stage failed",
                    job_id=job.id,
                    job_stage=stage.value,
                    error=f"{type(exc).__name__}: {exc}",
                )
                return StageExecution(
                    result=StageResult.failed_for(
                        stage, error=f"{type(exc).__name__}: {exc}"[:8000]
                    ),
                    sandbox_run=_sandbox_run(
                        job_id=job.id,
                        environment=environment,
                        settings=settings,
                        status=SandboxStatus.ERROR,
                        error=f"{type(exc).__name__}: {exc}",
                    ),
                    error=exc,
                )

            return StageExecution(
                result=result,
                sandbox_run=_sandbox_run(
                    job_id=job.id,
                    environment=environment,
                    settings=settings,
                    status=_terminal_status(result),
                    error=result.error,
                    tool_results=result.tool_results,
                ),
            )


def _terminal_status(result: StageResult) -> SandboxStatus:
    """Map a stage result onto the sandbox run's terminal status.

    A deliberately skipped stage is a success for the sandbox: nothing went wrong inside it.
    """
    if result.outcome in {JobStageOutcome.SUCCEEDED, JobStageOutcome.SKIPPED}:
        return SandboxStatus.SUCCEEDED
    return SandboxStatus.FAILED


def _sandbox_run(
    *,
    job_id: str,
    environment: StageEnvironment,
    settings: StageSettings,
    status: SandboxStatus,
    error: str | None,
    tool_results: tuple[ToolResult, ...] = (),
) -> SandboxRun:
    """Build the ``SandboxRun`` record for one stage execution.

    ``limits`` and ``policy`` are taken from the live sandbox rather than rebuilt from settings:
    they are the values that were *actually* enforced for this run, which is the whole point of
    storing them (spec section 35).
    """
    sandbox = environment.sandbox
    # A terminal status requires a completion time (the domain model enforces it), and the duration
    # is measured from when the sandbox started rather than from when the record was assembled.
    completed_at = utcnow()
    return SandboxRun(
        id=sandbox.run_id,
        job_id=job_id,
        backend=sandbox.backend,
        image=None if sandbox.backend is SandboxBackendKind.LOCAL else settings.sandbox_image,
        limits=sandbox.limits,
        policy=sandbox.policy,
        status=status,
        workspace_path=str(environment.workspace.path),
        started_at=environment.started_at,
        completed_at=completed_at,
        duration_seconds=max(0.0, (completed_at - environment.started_at).total_seconds()),
        tool_run_ids=[result.id for result in tool_results],
        error=None if error is None else error[:8000],
        native_id=None,
    )
