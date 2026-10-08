"""How the orchestrator runs a stage.

``StageRunner`` is the seam between scheduling and execution. There are two implementations:

* :class:`InProcessStageRunner` runs the stage inside the orchestrator process. It is the
  default (``SCOUT_EXECUTION_MODE=in_process``) and the mode the vertical slice uses, because
  it makes the whole control plane exercisable in one process with no queue hops.
* :class:`scout_worker.worker.Worker` is the other: the orchestrator publishes one stage message
  and a disposable execution-plane process runs it. The orchestrator still owns every status
  transition; the worker only records what the stage observed.

Both call :func:`scout_orchestrator.execution.execute_stage`, which is why "the same stage code
runs in either process" is a fact rather than an intention.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from scout_orchestrator.execution import StageExecution, execute_stage

if TYPE_CHECKING:
    from scout_domain.enums import JobStatus
    from scout_domain.models.job import VerificationJob
    from scout_github import GitHubClient
    from scout_orchestrator.stages import StageInputs, StageSettings
    from scout_tools.artifacts import ArtifactStore
    from scout_tools.registry import ToolRegistry

__all__ = ["InProcessStageRunner", "StageRequest", "StageRunner"]


@dataclass(frozen=True, slots=True)
class StageRequest:
    """One unit of stage work, with everything the runner needs to execute it."""

    job: VerificationJob
    stage: JobStatus
    inputs: StageInputs
    #: The identifier of the ``sandbox_runs`` row the stage's execution belongs to. Minted by
    #: the caller so the row can be recorded before the sandbox exists, which is what makes a
    #: leaked sandbox attributable to a job.
    sandbox_run_id: str


@runtime_checkable
class StageRunner(Protocol):
    """Executes one stage and returns everything that must be persisted for it."""

    async def run_stage(self, request: StageRequest) -> StageExecution:
        """Run ``request`` to completion and report the outcome, including failures."""
        ...


class InProcessStageRunner:
    """Runs a stage inside this process, in a disposable workspace and sandbox."""

    def __init__(
        self,
        *,
        settings: StageSettings,
        tools: ToolRegistry,
        github: GitHubClient,
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        """Bind the collaborators every stage needs.

        ``artifact_store`` is optional so tests can inject the in-memory store; the default is
        the content-addressed filesystem store under ``SCOUT_ARTIFACT_ROOT``.
        """
        self._settings = settings
        self._tools = tools
        self._github = github
        self._artifacts = artifact_store

    async def run_stage(self, request: StageRequest) -> StageExecution:
        """Run the stage and return its result plus the sandbox record."""
        return await execute_stage(
            job=request.job,
            stage=request.stage,
            inputs=request.inputs,
            settings=self._settings,
            tools=self._tools,
            github=self._github,
            sandbox_run_id=request.sandbox_run_id,
            artifact_store=self._artifacts,
        )
