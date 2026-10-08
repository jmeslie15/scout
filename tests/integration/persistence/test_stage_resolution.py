"""``resolved_stages`` and stage-attempt bookkeeping.

``resolved_stages`` is the single most load-bearing read in the job state machine. The
machine decides whether a job may advance by asking which earlier stages are *dealt
with* — completed, or deliberately skipped with a recorded reason (spec section 12) — so
this projection is what makes both resumption after a crash (spec section 50) and
risk-based skipping (spec section 41) representable.

The exact property under test is narrow and worth stating precisely: the returned set
contains a stage if and only if that stage has a row whose outcome is ``SUCCEEDED`` or
``SKIPPED``. Not ``RUNNING``, not ``PENDING``, and not ``FAILED``. If ``FAILED`` leaked
into that set, a failed security scan would let a job advance past security.
"""

from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from scout_domain.enums import JobStatus
from scout_domain.models.base import utcnow
from scout_domain.models.job import JobStageOutcome
from scout_persistence.repositories.stage import RESOLVED_OUTCOMES, StageRepository
from scout_persistence.tables import JobStageRow

pytestmark = pytest.mark.integration


async def test_resolved_stages_returns_the_domain_set() -> None:
    """The resolved outcome set is the domain's, not a hand-written pair.

    Derived from :attr:`JobStageOutcome.is_resolved` so a future reclassification cannot
    leave the database query behind.
    """
    assert {JobStageOutcome.SUCCEEDED, JobStageOutcome.SKIPPED} == RESOLVED_OUTCOMES
    assert {outcome for outcome in JobStageOutcome if outcome.is_resolved} == RESOLVED_OUTCOMES


async def test_start_stage_records_a_running_attempt(
    session: AsyncSession, job_factory: object
) -> None:
    """A started stage is ``RUNNING`` with a start time and no completion."""
    job = await _create_job(session, job_factory)
    row = await StageRepository(session).start_stage(job.id, JobStatus.PREPARING, 1)

    assert row.id.startswith("stage_")
    assert row.stage == JobStatus.PREPARING.value
    assert row.outcome == JobStageOutcome.RUNNING.value
    assert row.attempt == 1
    assert row.started_at is not None
    assert row.completed_at is None
    assert row.artifact_ids == []


async def test_complete_stage_fills_completed_at_and_duration(
    session: AsyncSession, job_factory: object
) -> None:
    """Completing a stage records the outcome, the completion time, and the duration."""
    job = await _create_job(session, job_factory)
    repository = StageRepository(session)
    started = await repository.start_stage(job.id, JobStatus.ANALYZING, 1)

    completed = await repository.complete_stage(started.id, JobStageOutcome.SUCCEEDED)

    assert completed.outcome == JobStageOutcome.SUCCEEDED.value
    assert completed.completed_at is not None
    assert completed.duration_seconds is not None
    assert completed.duration_seconds >= 0.0


async def test_resolved_stages_includes_succeeded_and_skipped(
    session: AsyncSession, job_factory: object
) -> None:
    """The exact set for a job whose stages succeeded, were skipped, and are still running."""
    job = await _create_job(session, job_factory)
    repository = StageRepository(session)

    prepared = await repository.start_stage(job.id, JobStatus.PREPARING, 1)
    await repository.complete_stage(prepared.id, JobStageOutcome.SUCCEEDED)

    security = await repository.start_stage(job.id, JobStatus.SECURITY_SCANNING, 1)
    await repository.complete_stage(
        security.id,
        JobStageOutcome.SKIPPED,
        skip_reason="Risk is LOW and only documentation changed.",
    )

    await repository.start_stage(job.id, JobStatus.TESTING, 1)

    resolved = await repository.resolved_stages(job.id)

    assert resolved == {JobStatus.PREPARING, JobStatus.SECURITY_SCANNING}
    assert JobStatus.TESTING not in resolved


async def test_resolved_stages_excludes_failed_running_and_pending(
    session: AsyncSession, job_factory: object
) -> None:
    """A failed stage is not resolved.

    This is the assertion that keeps a failed security scan from being treated as "dealt
    with". If it leaked into the resolved set, the state machine would let the job advance
    past the stage that just told it there was a problem.
    """
    job = await _create_job(session, job_factory)
    repository = StageRepository(session)

    failed = await repository.start_stage(job.id, JobStatus.SECURITY_SCANNING, 1)
    await repository.complete_stage(
        failed.id, JobStageOutcome.FAILED, error="bandit reported a high-severity issue"
    )
    await repository.start_stage(job.id, JobStatus.ANALYZING, 1)
    await repository.start_stage(job.id, JobStatus.REVIEWING, 1)

    resolved = await repository.resolved_stages(job.id)

    assert resolved == frozenset()
    assert JobStatus.SECURITY_SCANNING not in resolved
    assert JobStatus.ANALYZING not in resolved
    assert JobStatus.REVIEWING not in resolved


async def test_resolved_stages_is_per_job(session: AsyncSession, job_factory: object) -> None:
    """Resolution is scoped to one job, so a sibling job's progress cannot leak in."""
    repository = StageRepository(session)
    first = await _create_job(session, job_factory, head_sha="b" * 40)
    second = await _create_job(session, job_factory, pull_request_number=99, head_sha="c" * 40)

    stage = await repository.start_stage(first.id, JobStatus.PREPARING, 1)
    await repository.complete_stage(stage.id, JobStageOutcome.SUCCEEDED)

    assert await repository.resolved_stages(first.id) == {JobStatus.PREPARING}
    assert await repository.resolved_stages(second.id) == frozenset()


async def test_resolved_stages_deduplicates_retries(
    session: AsyncSession, job_factory: object
) -> None:
    """A stage retried after a failure still reports as resolved exactly once.

    Stages are append-only across attempts, so the projection has to collapse them; a
    ``list`` here would let a caller count a stage twice and conclude a job had advanced
    further than it had.
    """
    job = await _create_job(session, job_factory)
    repository = StageRepository(session)

    first_attempt = await repository.start_stage(job.id, JobStatus.TESTING, 1)
    await repository.complete_stage(first_attempt.id, JobStageOutcome.FAILED, error="flaky network")
    second_attempt = await repository.start_stage(job.id, JobStatus.TESTING, 2)
    await repository.complete_stage(second_attempt.id, JobStageOutcome.SUCCEEDED)

    resolved = await repository.resolved_stages(job.id)
    assert resolved == frozenset({JobStatus.TESTING})
    assert isinstance(resolved, frozenset)


async def test_failed_attempt_then_success_still_resolves(
    session: AsyncSession, job_factory: object
) -> None:
    """A later success resolves the stage even though an earlier attempt failed.

    The projection asks "is this stage dealt with?", not "was every attempt clean?".
    History is preserved by :meth:`StageRepository.list_for_job`, which is where a report
    should look for retry counts.
    """
    job = await _create_job(session, job_factory)
    repository = StageRepository(session)

    stage = await repository.start_stage(job.id, JobStatus.INVESTIGATING, 1)
    await repository.complete_stage(stage.id, JobStageOutcome.FAILED, error="no such file")
    retry = await repository.start_stage(job.id, JobStatus.INVESTIGATING, 2)
    await repository.complete_stage(retry.id, JobStageOutcome.SUCCEEDED)

    assert await repository.resolved_stages(job.id) == frozenset({JobStatus.INVESTIGATING})
    assert len(await repository.list_for_job(job.id)) == 2


async def test_list_for_job_is_in_creation_order(
    session: AsyncSession, job_factory: object
) -> None:
    """The timeline is ordered oldest first, with a stable tiebreak."""
    job = await _create_job(session, job_factory)
    repository = StageRepository(session)
    for stage in (
        JobStatus.PREPARING,
        JobStatus.ANALYZING,
        JobStatus.REVIEWING,
        JobStatus.TESTING,
    ):
        await repository.start_stage(job.id, stage, 1)

    rows = await repository.list_for_job(job.id)

    assert [row.stage for row in rows] == [
        JobStatus.PREPARING.value,
        JobStatus.ANALYZING.value,
        JobStatus.REVIEWING.value,
        JobStatus.TESTING.value,
    ]


async def test_same_stage_attempt_cannot_be_recorded_twice(
    session: AsyncSession, job_factory: object
) -> None:
    """``(job_id, stage, attempt)`` is unique.

    Without it, a double-delivered queue entry would add a second ``attempt=1`` row and the
    timeline would show a stage that ran twice when it ran once.
    """
    from sqlalchemy.exc import IntegrityError

    job = await _create_job(session, job_factory)
    await StageRepository(session).start_stage(job.id, JobStatus.PREPARING, 1)

    session.add(
        JobStageRow(
            id="stage_01J8ZQ4K2M7X3N9PQRSTVWXY0",
            job_id=job.id,
            stage=JobStatus.PREPARING.value,
            outcome=JobStageOutcome.RUNNING.value,
            attempt=1,
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_skipped_stage_requires_a_reason_in_the_database(
    session: AsyncSession, job_factory: object
) -> None:
    """The database refuses a silent skip (spec section 12).

    "No stage is silently skipped" is enforced by a check constraint as well as by the
    domain validator, so a direct write cannot bypass it.
    """
    from sqlalchemy.exc import IntegrityError

    job = await _create_job(session, job_factory)
    session.add(
        JobStageRow(
            id="stage_01J8ZQ4K2M7X3N9PQRSTVWXY1",
            job_id=job.id,
            stage=JobStatus.SECURITY_SCANNING.value,
            outcome=JobStageOutcome.SKIPPED.value,
            attempt=1,
            skip_reason=None,
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_mark_running_as_failed_closes_orphaned_stages(
    session: AsyncSession,
    verification_session: AsyncSession,
    job_factory: object,
) -> None:
    """The reclaim sweep can fail stages a dead worker left ``RUNNING``.

    Otherwise a crashed worker leaves a row that says ``RUNNING`` forever, and the job
    timeline shows a stage that will never finish.
    """
    job = await _create_job(session, job_factory)
    repository = StageRepository(session)
    running = await repository.start_stage(job.id, JobStatus.TESTING, 1)
    finished = await repository.start_stage(job.id, JobStatus.PREPARING, 1)
    await repository.complete_stage(finished.id, JobStageOutcome.SUCCEEDED)

    changed = await repository.mark_running_as_failed(job.id, error="worker crashed")

    assert changed == 1
    # Commit first: the reader is a separate connection, and an uncommitted update is
    # invisible to it for the same reason it is invisible to any other transaction.
    await session.commit()

    # Read through a fresh session: the bulk UPDATE bypasses the ORM identity map, so a
    # read through ``session`` could return the pre-update instance and the assertion
    # would pass or fail on cache state rather than on database state.
    reader = StageRepository(verification_session)
    refreshed = await reader.get(running.id)
    assert refreshed is not None
    assert refreshed.outcome == JobStageOutcome.FAILED.value
    assert refreshed.error == "worker crashed"
    assert refreshed.completed_at is not None

    # The already-finished stage is untouched.
    untouched = await reader.get(finished.id)
    assert untouched is not None
    assert untouched.outcome == JobStageOutcome.SUCCEEDED.value
    assert untouched.error is None


async def test_complete_stage_on_a_missing_row_raises(session: AsyncSession) -> None:
    """Completing a stage that does not exist is an error."""
    with pytest.raises(LookupError):
        await StageRepository(session).complete_stage("stage_missing", JobStageOutcome.SUCCEEDED)


async def test_latest_attempt_returns_the_highest_attempt_number(
    session: AsyncSession, job_factory: object
) -> None:
    """``latest_attempt`` picks the retry, not the first try."""
    job = await _create_job(session, job_factory)
    repository = StageRepository(session)
    await repository.start_stage(job.id, JobStatus.TESTING, 1)
    third = await repository.start_stage(job.id, JobStatus.TESTING, 3)

    latest = await repository.latest_attempt(job.id, JobStatus.TESTING)
    assert latest is not None
    assert latest.id == third.id
    assert latest.attempt == 3


async def _create_job(
    session: AsyncSession,
    job_factory: object,
    **overrides: object,
) -> object:
    """Create a job and return it, so each stage test has a parent row.

    ``job_stages.job_id`` is a foreign key, so a stage cannot exist without its job; this
    helper keeps that prerequisite from obscuring what each test is actually asserting.
    """
    from scout_persistence.repositories.job import JobRepository

    job = job_factory(**overrides)  # type: ignore[operator]
    await JobRepository(session).create(job)
    return job


def test_utcnow_is_timezone_aware() -> None:
    """Stage timestamps are compared against ``utcnow``; a naive value would break that."""
    assert utcnow().tzinfo is not None
