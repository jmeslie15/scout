"""The worker's stage executor: sandbox bookkeeping and unconditional cleanup.

The executor is where the isolation story becomes bookkeeping: a ``sandbox_runs`` row exists
*before* the sandbox does (so a leaked container is attributable to a job), the artifact rows are
recorded by the worker rather than by the stage -- which is what lets a stage run without a database
session -- and the sandbox result is closed out on the failure path as well as the success path.

``execute_stage`` is replaced with a fabricated execution so these tests stay offline: the real
workspace-and-sandbox lifecycle is covered by the end-to-end slice, which runs real repository code.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fakes import FakeUnitOfWork

from scout_domain.enums import JobStatus
from scout_domain.models.artifact import ArtifactKind
from scout_domain.models.base import utcnow
from scout_domain.models.sandbox import SandboxRun, SandboxStatus
from scout_orchestrator.execution import StageExecution
from scout_orchestrator.stages import StageInputs, StageResult
from scout_persistence.mapping import to_verification_job
from scout_worker.executor import StageExecutor

if TYPE_CHECKING:
    from scout_worker.settings import Settings


def _execution(*, succeeded: bool) -> StageExecution:
    """Fabricate a stage execution with one artifact and one failed run when asked."""
    result = (
        StageResult.succeeded_for(JobStatus.PREPARING, narrative="prepared")
        if succeeded
        else StageResult.failed_for(JobStatus.PREPARING, error="git fetch failed")
    )
    run = SandboxRun(
        id="srun_test",
        job_id="job_01J8ZQ4K2M7X3N9PQRSTVWXY0",
        backend="local",
        status=SandboxStatus.SUCCEEDED if succeeded else SandboxStatus.ERROR,
        error=None if succeeded else "git fetch failed",
        completed_at=utcnow(),
        workspace_path="/tmp/workspaces/job_test",
    )
    return StageExecution(result=result, sandbox_run=run, error=None if succeeded else OSError("x"))


@pytest.fixture
def executor(
    settings: Settings,
    uow: FakeUnitOfWork,
    monkeypatch: pytest.MonkeyPatch,
) -> StageExecutor:
    """An executor whose unit of work and stage execution are both faked."""
    import scout_worker.executor as executor_module

    monkeypatch.setattr(executor_module, "UnitOfWork", lambda _factory: uow)
    return StageExecutor(
        settings=settings.to_stage_settings(),
        session_factory=lambda: None,
        tools=_EmptyRegistry(),
        github=None,
    )


class _EmptyRegistry:
    """A registry with no tools; the executor tests never plan one."""

    def plan(self, *args: object, **kwargs: object) -> object:
        """Never called."""
        raise AssertionError("the registry is not used by these tests")

    def __iter__(self) -> object:
        """Empty iteration."""
        return iter(())


async def test_sandbox_run_row_is_created_and_closed(
    executor: StageExecutor, uow: FakeUnitOfWork, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A row exists before the sandbox starts, and is terminal afterwards."""
    import scout_worker.executor as executor_module

    row = uow.add_job(_job_row())
    job = to_verification_job(row)

    async def fake_execute(**_kwargs: object) -> StageExecution:
        return _execution(succeeded=True)

    monkeypatch.setattr(executor_module, "execute_stage", fake_execute)

    execution = await executor.execute(
        job=job,
        stage=JobStatus.PREPARING,
        inputs=StageInputs(),
        sandbox_run_id="srun_test",
    )

    assert execution.succeeded
    run = uow.sandbox_run_for(job.id)
    assert run is not None
    assert run.status is SandboxStatus.SUCCEEDED
    assert run.workspace_path == "/tmp/workspaces/job_test"


async def test_a_failed_stage_still_closes_its_sandbox_row(
    executor: StageExecutor, uow: FakeUnitOfWork, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The failure path records the same bookkeeping as the success path."""
    import scout_worker.executor as executor_module

    row = uow.add_job(_job_row())
    job = to_verification_job(row)

    async def fake_execute(**_kwargs: object) -> StageExecution:
        return _execution(succeeded=False)

    monkeypatch.setattr(executor_module, "execute_stage", fake_execute)

    execution = await executor.execute(
        job=job, stage=JobStatus.PREPARING, inputs=StageInputs(), sandbox_run_id="srun_test"
    )

    assert not execution.succeeded
    run = uow.sandbox_run_for(job.id)
    assert run is not None
    assert run.status is SandboxStatus.ERROR
    assert run.error == "git fetch failed"


async def test_artifact_rows_are_recorded_by_the_worker(
    executor: StageExecutor, uow: FakeUnitOfWork, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bytes live in the store; the worker owns the locator rows."""
    import scout_worker.executor as executor_module

    row = uow.add_job(_job_row())
    job = to_verification_job(row)
    artifact = executor.artifacts.put_text(
        "diff --git a/x b/x\n",
        kind=ArtifactKind.DIFF,
        content_type="text/x-diff",
        filename="changes.diff",
        job_id=job.id,
    )
    result = StageResult.succeeded_for(JobStatus.PREPARING, artifacts=(artifact,))

    async def fake_execute(**_kwargs: object) -> StageExecution:
        return StageExecution(result=result, sandbox_run=_execution(succeeded=True).sandbox_run)

    monkeypatch.setattr(executor_module, "execute_stage", fake_execute)

    execution = await executor.execute(
        job=job, stage=JobStatus.PREPARING, inputs=StageInputs(), sandbox_run_id="srun_test"
    )

    assert execution.result.artifact_ids == (artifact.id,)
    assert uow.state.artifacts == [artifact]


def _job_row() -> object:
    """Build the seeded job row for these tests."""
    from fakes import make_job_row

    return make_job_row(status=JobStatus.PREPARING)
