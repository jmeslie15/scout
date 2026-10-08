"""Recording ``sandbox_runs`` rows around a stage execution.

Shared by the orchestrator's in-process runner and the worker's executor because the row has to
be *created before* the sandbox exists. That ordering is the point of the table: a sandbox whose
process died has no other trace, and ``list_orphaned`` plus ``native_id`` is what lets an
operator find and kill a leak.

The limits and policy snapshot written at creation comes from the same factories
:func:`scout_orchestrator.execution.stage_environment` uses to build the sandbox, so the record
cannot drift from what was enforced.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from scout_domain.enums import SandboxBackendKind
from scout_domain.models.base import utcnow
from scout_domain.models.sandbox import SandboxRun, SandboxStatus
from scout_orchestrator.execution import sandbox_limits, sandbox_policy

if TYPE_CHECKING:
    from scout_domain.config import ScoutRepositoryConfig
    from scout_orchestrator.stages import StageSettings
    from scout_persistence.unit_of_work import UnitOfWork

__all__ = ["complete_sandbox_run", "create_sandbox_run"]


async def create_sandbox_run(
    uow: UnitOfWork,
    *,
    job_id: str,
    sandbox_run_id: str,
    settings: StageSettings,
    repository_config: ScoutRepositoryConfig | None = None,
) -> None:
    """Insert the ``PENDING`` sandbox-run row before the sandbox is started."""
    backend = SandboxBackendKind(settings.sandbox_backend)
    await uow.sandbox_runs.create(
        SandboxRun(
            id=sandbox_run_id,
            job_id=job_id,
            backend=backend,
            # The local backend runs on the host, so there is no image to record; recording one
            # anyway would misdescribe what executed the code.
            image=None if backend is SandboxBackendKind.LOCAL else settings.sandbox_image,
            limits=sandbox_limits(settings, repository_config=repository_config),
            policy=sandbox_policy(settings),
            status=SandboxStatus.PENDING,
            workspace_path="",
            started_at=utcnow(),
        )
    )


async def complete_sandbox_run(uow: UnitOfWork, run: SandboxRun) -> None:
    """Close the sandbox-run row with the status the sandbox actually reached."""
    await uow.sandbox_runs.mark_running(
        run.id,
        native_id=run.native_id,
        workspace_path=run.workspace_path or None,
    )
    await uow.sandbox_runs.complete(run.id, status=run.status, error=run.error)
