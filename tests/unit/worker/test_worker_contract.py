"""The worker executes one stage per message and never decides a job status.

Spec section 11 puts every status transition behind ``assert_transition``, and architecture
section 2 assigns those transitions to the orchestrator alone. The worker's most important property
is therefore a *negative* one, and it is worth testing two ways:

* structurally, by refusing to let the source contain a status write at all -- a future change that
  adds one fails this test rather than shipping a second source of truth;
* behaviourally, by proving the contract check fires when a dispatched stage disagrees with the
  job's recorded status, that redelivery of a resolved stage is a no-op, and that finishing a stage
  hands the job back to the orchestrator rather than advancing it.
"""

from __future__ import annotations

import ast
import inspect

import pytest
from fakes import FakeUnitOfWork, make_job_row

from scout_domain.enums import JobStatus
from scout_domain.models.job import JobStageOutcome
from scout_persistence.mapping import to_verification_job
from scout_persistence.queue import JobMessage
from scout_worker import worker as worker_module
from scout_worker.errors import WorkerProtocolError
from scout_worker.worker import Worker


def _tree() -> ast.Module:
    """The worker implementation parsed into an AST.

    Parsed rather than grepped: the module's docstring *mentions* ``update_status`` and
    ``recommendation`` precisely to explain their absence, so a substring check would fail on its
    own documentation while a real call could hide behind a comment.
    """
    return ast.parse(inspect.getsource(worker_module))


def _called_attributes(tree: ast.Module) -> set[str]:
    """Names of attributes invoked as calls anywhere in the module."""
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def _assigned_attributes(tree: ast.Module) -> set[str]:
    """Names of attributes assigned anywhere in the module."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
            targets = [node.target]
        else:
            continue
        names.update(target.attr for target in targets if isinstance(target, ast.Attribute))
    return names


def test_worker_never_writes_a_job_status() -> None:
    """No status write and no recommendation exists in the worker.

    The worker legitimately *reads* ``job.status`` (to refuse work for a finished job, and to check
    the dispatched stage against it); what it must never do is write one.
    """
    tree = _tree()
    called = _called_attributes(tree)
    assigned = _assigned_attributes(tree)
    identifiers = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    identifiers |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}

    assert "update_status" not in called
    assert "apply_transition" not in called
    assert "status" not in assigned
    assert "recommendation" not in identifiers


def test_worker_only_records_stage_outcomes() -> None:
    """The worker's writes are stage rows and results, which is exactly its remit."""
    called = _called_attributes(_tree())

    assert "start_stage" in called
    assert "complete_stage" in called


async def test_dispatch_mismatch_is_a_protocol_error(worker: Worker, uow: FakeUnitOfWork) -> None:
    """A dispatched stage that contradicts the job's status must fail loudly, not be guessed at."""
    row = uow.add_job(make_job_row(status=JobStatus.TESTING))
    job = to_verification_job(row)
    message = JobMessage(
        job_id=job.id,
        repository_full_name=job.repository_full_name,
        head_sha=job.head_sha,
        attempt=job.attempt,
        resume_stage=JobStatus.PREPARING,
    )

    with pytest.raises(WorkerProtocolError):
        await worker._resolve_stage(job, message)


async def test_dispatched_stage_is_honoured(worker: Worker, uow: FakeUnitOfWork) -> None:
    """The orchestrator's instruction wins, because the status was advanced before dispatch."""
    row = uow.add_job(make_job_row(status=JobStatus.TESTING))
    job = to_verification_job(row)
    message = JobMessage(
        job_id=job.id,
        repository_full_name=job.repository_full_name,
        head_sha=job.head_sha,
        attempt=job.attempt,
        resume_stage=JobStatus.TESTING,
    )

    work = await worker._resolve_stage(job, message)

    assert work is not None
    assert work.stage is JobStatus.TESTING


async def test_a_completed_pipeline_resolves_to_no_stage(
    worker: Worker, uow: FakeUnitOfWork
) -> None:
    """With every stage resolved there is nothing for the worker to do."""
    row = uow.add_job(make_job_row(status=JobStatus.INVESTIGATING))
    job = to_verification_job(row)
    for stage in (
        JobStatus.PREPARING,
        JobStatus.ANALYZING,
        JobStatus.REVIEWING,
        JobStatus.SECURITY_SCANNING,
        JobStatus.TESTING,
        JobStatus.FEATURE_VERIFYING,
        JobStatus.INVESTIGATING,
    ):
        uow.add_stage(job.id, stage, JobStageOutcome.SUCCEEDED)
    message = JobMessage(
        job_id=job.id,
        repository_full_name=job.repository_full_name,
        head_sha=job.head_sha,
        attempt=job.attempt,
    )

    assert await worker._resolve_stage(job, message) is None


async def test_resumption_without_a_dispatch_derives_the_stage(
    worker: Worker, uow: FakeUnitOfWork
) -> None:
    """A reclaimed message carries no stage, so the worker derives it from the resolved set."""
    row = uow.add_job(make_job_row(status=JobStatus.ANALYZING))
    job = to_verification_job(row)
    uow.add_stage(job.id, JobStatus.PREPARING, JobStageOutcome.SUCCEEDED)
    message = JobMessage(
        job_id=job.id,
        repository_full_name=job.repository_full_name,
        head_sha=job.head_sha,
        attempt=job.attempt,
    )

    work = await worker._resolve_stage(job, message)

    assert work is not None
    assert work.stage is JobStatus.ANALYZING


async def test_a_resolved_stage_is_not_re_executed(worker: Worker, uow: FakeUnitOfWork) -> None:
    """At-least-once delivery must not run a finished stage twice."""
    row = uow.add_job(make_job_row(status=JobStatus.PREPARING))
    job = to_verification_job(row)
    uow.add_stage(job.id, JobStatus.PREPARING, JobStageOutcome.SUCCEEDED)

    assert await worker._stage_is_resolved(job.id, JobStatus.PREPARING) is True
    assert await worker._stage_is_resolved(job.id, JobStatus.ANALYZING) is False


async def test_finishing_a_stage_hands_the_job_back(
    worker: Worker, uow: FakeUnitOfWork, queue: object
) -> None:
    """The worker publishes a scheduling message; it does not advance the job."""
    row = uow.add_job(make_job_row(status=JobStatus.PREPARING))
    job = to_verification_job(row)

    await worker._hand_back(job, attempt=job.attempt)

    assert queue.ready_count == 1  # type: ignore[attr-defined]
    reserved = await queue.reserve("probe", block_ms=0, count=1)  # type: ignore[attr-defined]
    message = reserved[0][1]
    assert message.resume_stage is None
    assert message.attempt == job.attempt
    assert uow.statuses_written() == []


async def test_a_finished_job_is_ignored(worker: Worker, uow: FakeUnitOfWork) -> None:
    """Work for a cancelled or completed job is acknowledged and dropped."""
    row = uow.add_job(make_job_row(status=JobStatus.CANCELLED, cancel_reason="operator stopped it"))
    job = to_verification_job(row)
    message = JobMessage(
        job_id=job.id,
        repository_full_name=job.repository_full_name,
        head_sha=job.head_sha,
        attempt=job.attempt,
        resume_stage=JobStatus.PREPARING,
    )

    await worker.process_message(message)

    assert uow.statuses_written() == []
