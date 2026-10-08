"""Retryable failures are retried with backoff; non-retryable ones are not (spec section 51).

The policy is domain code and is unit-tested there; what these tests prove is the *orchestrator's*
use of it:

* a retryable stage failure moves the job through ``FAILED`` to ``QUEUED``, increments the attempt,
  records an audit row, waits the computed backoff, and re-enqueues -- with the attempt counter
  carried on the message so a stale redelivery is ignored;
* a non-retryable failure moves the job to ``FAILED`` with a reason and does **not** re-enqueue;
* the retry budget is finite: the last allowed attempt fails the job instead of looping;
* classification is by exception *type*, so a message that merely reads like a transient failure
  does not become a retry storm.

The sleep is injected, so the whole file runs instantly while still asserting the exact delays.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fakes import FakeUnitOfWork, RecordingSleep, make_job_row

from scout_domain.enums import JobStatus
from scout_orchestrator.errors import ResumeInvariantViolation
from scout_orchestrator.orchestrator import Orchestrator
from scout_persistence.queue import InMemoryJobQueue
from scout_sandbox.errors import SandboxBackendUnavailable, SandboxCommandError

if TYPE_CHECKING:
    from scout_domain.models.job import VerificationJob


def _seed(uow: FakeUnitOfWork, *, status: JobStatus, attempt: int = 1) -> VerificationJob:
    """Seed a job and return its domain value."""
    row = uow.add_job(make_job_row(status=status, attempt=attempt))
    return uow.jobs.to_domain(row)


async def test_retryable_failure_is_retried_with_backoff(
    orchestrator: Orchestrator,
    uow: FakeUnitOfWork,
    queue: InMemoryJobQueue,
    sleep: RecordingSleep,
) -> None:
    """A transient sandbox failure re-queues the job with an incremented attempt."""
    job = _seed(uow, status=JobStatus.TESTING)

    await orchestrator._handle_stage_failure(
        job, JobStatus.TESTING, SandboxBackendUnavailable("the container runtime is unavailable")
    )

    # FAILED records the failure, QUEUED resumes the job. Both go through the state machine.
    assert uow.statuses_written() == [JobStatus.FAILED, JobStatus.QUEUED]
    assert uow.job_row(job.id).attempt == 2
    assert "job.retry_scheduled" in uow.audit_actions()

    # The delay is the policy's first backoff, and it was actually asked for.
    assert sleep.delays == [1.0]

    reserved = await queue.reserve("test", block_ms=0, count=1)
    assert len(reserved) == 1
    _message_id, message = reserved[0]
    assert message.job_id == job.id
    assert message.attempt == 2
    assert message.resume_stage is JobStatus.TESTING


async def test_non_retryable_failure_is_not_retried(
    orchestrator: Orchestrator,
    uow: FakeUnitOfWork,
    queue: InMemoryJobQueue,
    sleep: RecordingSleep,
) -> None:
    """A permanent failure fails the job and re-enqueues nothing."""
    job = _seed(uow, status=JobStatus.TESTING)

    await orchestrator._handle_stage_failure(
        job, JobStatus.TESTING, SandboxCommandError("the pytest binary is missing")
    )

    assert uow.statuses_written() == [JobStatus.FAILED]
    assert uow.job_row(job.id).failure_reason is not None
    assert "job.failed" in uow.audit_actions()
    assert sleep.delays == []
    assert queue.ready_count == 0


async def test_unknown_exception_types_fail_closed(
    orchestrator: Orchestrator,
    uow: FakeUnitOfWork,
    queue: InMemoryJobQueue,
) -> None:
    """An unclassified exception is not a licence to retry: it is treated as permanent."""
    job = _seed(uow, status=JobStatus.TESTING)

    await orchestrator._handle_stage_failure(job, JobStatus.TESTING, ValueError("something odd"))

    assert uow.statuses_written() == [JobStatus.FAILED]
    assert queue.ready_count == 0


async def test_retry_budget_is_finite(
    orchestrator: Orchestrator,
    uow: FakeUnitOfWork,
    queue: InMemoryJobQueue,
) -> None:
    """The last allowed attempt fails the job rather than looping forever."""
    job = _seed(uow, status=JobStatus.TESTING, attempt=3)

    await orchestrator._handle_stage_failure(
        job, JobStatus.TESTING, SandboxBackendUnavailable("still unavailable")
    )

    assert job.max_attempts == 3
    assert uow.statuses_written() == [JobStatus.FAILED]
    assert queue.ready_count == 0


async def test_backoff_grows_with_the_attempt(
    orchestrator: Orchestrator,
    uow: FakeUnitOfWork,
    sleep: RecordingSleep,
) -> None:
    """Exponential backoff, bounded by the configured ceiling."""
    first = _seed(uow, status=JobStatus.TESTING, attempt=1)
    await orchestrator._handle_stage_failure(
        first, JobStatus.TESTING, SandboxBackendUnavailable("down")
    )

    second_row = uow.add_job(make_job_row(job_id="job_second", status=JobStatus.TESTING, attempt=2))
    second = uow.jobs.to_domain(second_row)
    await orchestrator._handle_stage_failure(
        second, JobStatus.TESTING, SandboxBackendUnavailable("down")
    )

    assert sleep.delays == [1.0, 2.0]


async def test_forced_retryable_overrides_text_classification(
    orchestrator: Orchestrator,
    uow: FakeUnitOfWork,
    queue: InMemoryJobQueue,
) -> None:
    """The worker plane's reclaim marker is retryable even though the error is only text.

    This is the one case where the exception object does not survive: the reclaim sweep wrote a
    marker into the stage row, and the orchestrator recognises its own marker rather than guessing
    from an arbitrary message.
    """
    job = _seed(uow, status=JobStatus.TESTING)

    await orchestrator._handle_stage_failure(
        job,
        JobStatus.TESTING,
        ResumeInvariantViolation("reclaimed: the worker stopped renewing its lease"),
        force_retryable=True,
    )

    assert uow.statuses_written() == [JobStatus.FAILED, JobStatus.QUEUED]
    assert queue.ready_count == 1


async def test_a_stale_message_is_ignored(
    orchestrator: Orchestrator,
    uow: FakeUnitOfWork,
) -> None:
    """A redelivered message for an older attempt must not double-execute a stage."""
    _seed(uow, status=JobStatus.TESTING, attempt=2)

    await orchestrator.process_job("job_01J8ZQ4K2M7X3N9PQRSTVWXY0", attempt=1)

    assert uow.statuses_written() == []
