"""Leases are what make "a job survives a worker restart" true (spec section 50).

A crashed process cannot clear its own lease -- nothing runs when a process dies -- so the job would
sit ``RUNNING`` forever with nobody owning it. These tests exercise the two halves of the recovery
story against a real PostgreSQL:

* the **sweep** (``list_expired_leases`` + re-enqueue) for a job whose owner disappeared;
* the **resumption** that follows: the job returns to ``QUEUED`` and the derivation sends it back to
  its first *unresolved* stage rather than to the beginning, which is the whole point of the
  resolved-stage projection.

The conditional-``UPDATE`` semantics of ``claim_lease`` are covered by the persistence suite; what is
proven here is the orchestrator's use of it.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest
from sqlalchemy import select

from scout_domain.enums import JobStatus
from scout_domain.models.base import utcnow
from scout_domain.models.job import CommitRange, JobStageOutcome, VerificationJob
from scout_domain.models.repository import RepositoryRef
from scout_persistence.queue import InMemoryJobQueue
from scout_persistence.tables import JobRow, JobStageRow
from scout_persistence.unit_of_work import UnitOfWork
from scout_tools.artifacts import InMemoryArtifactStore

from scout_orchestrator.orchestrator import Orchestrator
from scout_orchestrator.settings import Settings
from scout_orchestrator.state import RECLAIM_ERROR_PREFIX

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

pytestmark = pytest.mark.integration

JOB_ID = "job_01J8ZQ4K2M7X3N9PQRSTVWXY0"
HEAD = "b" * 40
BASE = "a" * 40


def _settings(tmp_path: object) -> Settings:
    """Orchestrator settings for an integration test."""
    return Settings(
        env="test",
        database_url="postgresql+asyncpg://scout:scout@localhost:5433/scout",
        redis_url="redis://localhost:6380/0",
        artifact_root=tmp_path / "artifacts",  # type: ignore[operator]
        workspace_root=tmp_path / "workspaces",  # type: ignore[operator]
        sandbox_backend="local",
        notifications_enabled=False,
        lease_ttl_seconds=60,
    )


async def _seed_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    status: JobStatus,
    lease_owner: str | None,
    lease_expires_at: object,
    attempt: int = 1,
) -> None:
    """Insert a job row with a lease in the state under test."""
    async with UnitOfWork(session_factory) as uow:
        job = VerificationJob(
            id=JOB_ID,
            repository=RepositoryRef(full_name="acme/widgets"),
            repository_full_name="acme/widgets",
            pull_request_number=42,
            commits=CommitRange(base_sha=BASE, head_sha=HEAD),
            status=status,
            attempt=attempt,
            correlation_id="corr-lease",
        )
        row = await uow.jobs.create(job)
        row.lease_owner = lease_owner
        row.lease_expires_at = lease_expires_at  # type: ignore[assignment]


async def _seed_stage(
    session_factory: async_sessionmaker[AsyncSession],
    stage: JobStatus,
    outcome: JobStageOutcome,
    *,
    attempt: int = 1,
) -> None:
    """Insert a stage row for the seeded job."""
    async with UnitOfWork(session_factory) as uow:
        row = await uow.stages.start_stage(JOB_ID, stage, attempt)
        await uow.stages.complete_stage(row.id, outcome, skip_reason="seeded")


async def _job_status(session_factory: async_sessionmaker[AsyncSession]) -> str:
    """Read the persisted job status."""
    async with UnitOfWork(session_factory) as uow:
        row = await uow.jobs.get(JOB_ID)
        assert row is not None
        return str(row.status)


async def test_expired_lease_is_released_and_requeued(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: object
) -> None:
    """A job whose owner vanished is released and re-enqueued."""
    expired = utcnow() - timedelta(minutes=5)
    await _seed_job(
        session_factory,
        status=JobStatus.TESTING,
        lease_owner="worker:host:999999",
        lease_expires_at=expired,
    )
    await _seed_stage(session_factory, JobStatus.PREPARING, JobStageOutcome.SUCCEEDED)

    queue = InMemoryJobQueue()
    orchestrator = Orchestrator(
        settings=_settings(tmp_path),
        session_factory=session_factory,
        artifacts=InMemoryArtifactStore(),
        github=None,  # type: ignore[arg-type]
        queue=queue,
        consumer="orchestrator:test:1",
    )

    reclaimed = await orchestrator.sweep_expired_leases()

    assert reclaimed == 1
    assert await _job_status(session_factory) == JobStatus.TESTING.value

    async with UnitOfWork(session_factory) as uow:
        row = await uow.jobs.get(JOB_ID)
        assert row is not None
        assert row.lease_owner is None
        assert row.lease_expires_at is None
        events = await uow.audit_events.list_for_job(JOB_ID)
        assert any(event.action == "lease.reclaimed" for event in events)

    reserved = await queue.reserve("probe", block_ms=0, count=1)
    assert len(reserved) == 1
    message = reserved[0][1]
    assert message.job_id == JOB_ID
    assert message.resume_stage is None, "resumption is derived, never pinned by the sweep"


async def test_a_running_stage_left_by_a_dead_owner_is_marked_failed(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: object
) -> None:
    """A stage that will never finish must not sit ``RUNNING`` forever in the timeline."""
    await _seed_job(
        session_factory,
        status=JobStatus.TESTING,
        lease_owner="worker:host:999999",
        lease_expires_at=utcnow() - timedelta(minutes=5),
    )
    async with UnitOfWork(session_factory) as uow:
        started = await uow.stages.start_stage(JOB_ID, JobStatus.TESTING, 1)
        stage_id = started.id

    orchestrator = Orchestrator(
        settings=_settings(tmp_path),
        session_factory=session_factory,
        artifacts=InMemoryArtifactStore(),
        github=None,  # type: ignore[arg-type]
        queue=InMemoryJobQueue(),
        consumer="orchestrator:test:1",
    )
    await orchestrator.sweep_expired_leases()

    async with UnitOfWork(session_factory) as uow:
        row = await uow.stages.get(stage_id)
        assert row is not None
        assert row.outcome == JobStageOutcome.FAILED.value
        assert row.error is not None
        assert row.error.startswith(RECLAIM_ERROR_PREFIX)


async def test_a_live_lease_is_never_reclaimed(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: object
) -> None:
    """A job whose owner is still renewing its lease is left alone."""
    await _seed_job(
        session_factory,
        status=JobStatus.TESTING,
        lease_owner="worker:host:1234",
        lease_expires_at=utcnow() + timedelta(minutes=10),
    )
    queue = InMemoryJobQueue()
    orchestrator = Orchestrator(
        settings=_settings(tmp_path),
        session_factory=session_factory,
        artifacts=InMemoryArtifactStore(),
        github=None,  # type: ignore[arg-type]
        queue=queue,
        consumer="orchestrator:test:1",
    )

    assert await orchestrator.sweep_expired_leases() == 0
    assert queue.ready_count == 0

    async with UnitOfWork(session_factory) as uow:
        row = await uow.jobs.get(JOB_ID)
        assert row is not None
        assert row.lease_owner == "worker:host:1234"


async def test_the_lease_is_claimed_atomically(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: object
) -> None:
    """A second orchestrator cannot take a lease a live owner holds."""
    await _seed_job(
        session_factory,
        status=JobStatus.TESTING,
        lease_owner="orchestrator:first",
        lease_expires_at=utcnow() + timedelta(minutes=5),
    )
    async with UnitOfWork(session_factory) as uow:
        claimed_by_second = await uow.jobs.claim_lease(JOB_ID, "orchestrator:second")
        claimed_by_owner = await uow.jobs.claim_lease(JOB_ID, "orchestrator:first")

    assert claimed_by_second is False
    assert claimed_by_owner is True, "renewal by the current owner must be idempotent"


async def test_resume_derives_the_first_unresolved_stage(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: object
) -> None:
    """After a reclaim, the job resumes where it left off rather than from the start.

    ``PREPARING``, ``ANALYZING``, and ``REVIEWING`` are resolved, so the derived resume point is
    ``SECURITY_SCANNING`` -- the first unresolved stage. Re-checking out the repository would be the
    expensive, wrong answer.
    """
    await _seed_job(
        session_factory,
        status=JobStatus.REVIEWING,
        lease_owner="orchestrator:dead",
        lease_expires_at=utcnow() - timedelta(minutes=1),
    )
    for stage in (JobStatus.PREPARING, JobStatus.ANALYZING, JobStatus.REVIEWING):
        await _seed_stage(session_factory, stage, JobStageOutcome.SUCCEEDED)

    orchestrator = Orchestrator(
        settings=_settings(tmp_path),
        session_factory=session_factory,
        artifacts=InMemoryArtifactStore(),
        github=None,  # type: ignore[arg-type]
        queue=InMemoryJobQueue(),
        consumer="orchestrator:test:1",
    )

    state = await orchestrator._load_orchestration_state(JOB_ID)

    assert state.resolved_stages == frozenset(
        {JobStatus.PREPARING, JobStatus.ANALYZING, JobStatus.REVIEWING}
    )
    assert state.next_stage is JobStatus.SECURITY_SCANNING


async def test_a_skipped_stage_counts_as_resolved(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: object
) -> None:
    """A recorded skip advances the derivation, which is what makes risk-based skipping legal."""
    await _seed_job(
        session_factory,
        status=JobStatus.SECURITY_SCANNING,
        lease_owner=None,
        lease_expires_at=None,
    )
    for stage in (JobStatus.PREPARING, JobStatus.ANALYZING, JobStatus.REVIEWING):
        await _seed_stage(session_factory, stage, JobStageOutcome.SUCCEEDED)
    await _seed_stage(session_factory, JobStatus.SECURITY_SCANNING, JobStageOutcome.SKIPPED)

    orchestrator = Orchestrator(
        settings=_settings(tmp_path),
        session_factory=session_factory,
        artifacts=InMemoryArtifactStore(),
        github=None,  # type: ignore[arg-type]
        queue=InMemoryJobQueue(),
        consumer="orchestrator:test:1",
    )

    state = await orchestrator._load_orchestration_state(JOB_ID)

    assert state.next_stage is JobStatus.TESTING


async def test_expired_lease_on_a_finished_job_is_released_without_requeue(
    session_factory: async_sessionmaker[AsyncSession], tmp_path: object
) -> None:
    """A terminal job is bookkeeping, not work to resume: reclaiming it would resurrect it."""
    await _seed_job(
        session_factory,
        status=JobStatus.WAITING_FOR_HUMAN,
        lease_owner="orchestrator:dead",
        lease_expires_at=utcnow() - timedelta(minutes=1),
    )
    queue = InMemoryJobQueue()
    orchestrator = Orchestrator(
        settings=_settings(tmp_path),
        session_factory=session_factory,
        artifacts=InMemoryArtifactStore(),
        github=None,  # type: ignore[arg-type]
        queue=queue,
        consumer="orchestrator:test:1",
    )

    reclaimed = await orchestrator.sweep_expired_leases()

    assert reclaimed == 0
    assert queue.ready_count == 0
    async with UnitOfWork(session_factory) as uow:
        rows = list((await uow.session.scalars(select(JobRow.id))).all())
    assert rows == [JOB_ID]


async def test_persisted_stage_rows_are_the_derivation_source(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """The resolved-stage projection comes from rows, not from anything a caller asserts."""
    await _seed_job(
        session_factory, status=JobStatus.PREPARING, lease_owner=None, lease_expires_at=None
    )
    async with UnitOfWork(session_factory) as uow:
        started = await uow.stages.start_stage(JOB_ID, JobStatus.PREPARING, 1)

    async with UnitOfWork(session_factory) as uow:
        assert await uow.stages.resolved_stages(JOB_ID) == frozenset()

        await uow.stages.complete_stage(started.id, JobStageOutcome.SUCCEEDED)
        assert await uow.stages.resolved_stages(JOB_ID) == frozenset({JobStatus.PREPARING})

    async with UnitOfWork(session_factory) as uow:
        rows = list(
            (
                await uow.session.scalars(select(JobStageRow).where(JobStageRow.job_id == JOB_ID))
            ).all()
        )
    assert len(rows) == 1
    assert rows[0].outcome == JobStageOutcome.SUCCEEDED.value
