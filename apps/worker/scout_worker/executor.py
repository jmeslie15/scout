"""Executes exactly one pipeline stage inside a disposable sandbox.

The worker's job is narrow on purpose: create the isolated environment, run the stage the
orchestrator chose, persist what it produced, and tear everything down. It never decides a job
status, never chooses a stage, and never classifies a failure for retry -- all three belong to the
orchestrator, and a worker that started making those decisions would be a second source of truth
for the lifecycle.

Cleanup is unconditional. ``scout_orchestrator.execution.execute_stage`` owns the workspace and
sandbox lifecycle as an async context manager, so both are disposed on the exception path as well
as the success path; and because ``Workspace.create`` wipes any existing directory for the same job
id, a process killed outright still leaves a workspace the next attempt can reclaim rather than an
ambiguous pile of files.

Why this module imports from :mod:`scout_orchestrator`: the stage implementations live in the
orchestrator package (that is where the pipeline is defined), and the two execution planes must run
*the same* code or "the worker behaves exactly like the in-process runner" stops being true. The
import direction is one-way -- worker depends on the pipeline, the pipeline depends on neither app
-- and the deployment declares the dependency. Keeping a second copy of the stages here would be
the drift this avoids.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from scout_domain.models.base import utcnow
from scout_observability import get_logger
from scout_orchestrator.execution import StageExecution, execute_stage
from scout_orchestrator.sandbox_records import complete_sandbox_run, create_sandbox_run
from scout_orchestrator.stages import StageInputs, StageSettings
from scout_persistence.unit_of_work import UnitOfWork
from scout_tools.artifacts import LocalArtifactStore

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from scout_domain.enums import JobStatus
    from scout_domain.models.job import VerificationJob
    from scout_github import GitHubClient
    from scout_tools.artifacts import ArtifactStore
    from scout_tools.registry import ToolRegistry

__all__ = ["StageExecutor"]

_log = get_logger(__name__)


class StageExecutor:
    """Runs one stage inside a sandbox and records the sandbox-run row around it."""

    def __init__(
        self,
        *,
        settings: StageSettings,
        session_factory: async_sessionmaker[AsyncSession],
        tools: ToolRegistry,
        github: GitHubClient,
        artifacts: ArtifactStore | None = None,
    ) -> None:
        """Bind the collaborators a stage needs.

        ``artifacts`` defaults to the content-addressed filesystem store, which is where tool
        stdout, stderr, and machine-readable reports are written with
        :meth:`scout_tools.artifacts.LocalArtifactStore.put_file`.
        """
        self._settings = settings
        self._session_factory = session_factory
        self._tools = tools
        self._github = github
        self._artifacts: ArtifactStore = artifacts or LocalArtifactStore(settings.artifact_root)

    @property
    def artifacts(self) -> ArtifactStore:
        """The artifact store the worker also uses for its own records."""
        return self._artifacts

    async def execute(
        self,
        *,
        job: VerificationJob,
        stage: JobStatus,
        inputs: StageInputs,
        sandbox_run_id: str,
    ) -> StageExecution:
        """Create the sandbox, run exactly one stage, persist its artifacts, and tear down.

        Returns:
            The stage's result, its sandbox record, and the original exception when the stage
            raised. Failures are returned rather than raised so the caller can record the failed
            stage row and the sandbox row before deciding what to tell the orchestrator.
        """
        async with UnitOfWork(self._session_factory) as uow:
            await create_sandbox_run(
                uow,
                job_id=job.id,
                sandbox_run_id=sandbox_run_id,
                settings=self._settings,
                repository_config=inputs.repository_config,
            )

        _log.info(
            "executing stage",
            job_id=job.id,
            job_stage=stage.value,
            sandbox_run_id=sandbox_run_id,
            attempt=job.attempt,
        )
        execution = await execute_stage(
            job=job,
            stage=stage,
            inputs=inputs,
            settings=self._settings,
            tools=self._tools,
            github=self._github,
            sandbox_run_id=sandbox_run_id,
            artifact_store=self._artifacts,
        )

        # The artifact rows are written here rather than by the stage: the bytes are in the store
        # and the worker owns the database, which is the split that keeps a stage from needing a
        # session at all.
        async with UnitOfWork(self._session_factory) as uow:
            for artifact in execution.result.artifacts:
                await uow.artifacts.record(artifact)
            await complete_sandbox_run(uow, execution.sandbox_run)

        _log.info(
            "stage execution finished",
            job_id=job.id,
            job_stage=stage.value,
            status=execution.sandbox_run.status.value,
            artifacts=len(execution.result.artifacts),
            tool_runs=len(execution.result.tool_results),
            finished_at=str(utcnow()),
        )
        return execution
