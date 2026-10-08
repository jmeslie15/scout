"""Lease claiming: the double-execution guard (spec sections 50, 51).

A job lease exists so that two workers cannot own one job at the same time, even when a
queue delivers the same entry twice. The property that makes it work is that
:meth:`JobRepository.claim_lease` is a *single conditional UPDATE*: PostgreSQL evaluates
the ``WHERE`` clause and applies the write atomically, so exactly one of two concurrent
claimants can match an expired-or-absent lease.

These tests therefore cover three cases that a read-then-write implementation would get
wrong: a live lease is not stealable, an expired lease is reclaimable, and the same owner
re-claiming its own lease is idempotent (so a retry does not lose the lease it holds).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from scout_domain.enums import JobStatus
from scout_domain.models.base import utcnow
from scout_persistence.repositories.job import JobRepository

pytestmark = pytest.mark.integration


async def claim(
    session: AsyncSession,
    job_id: str,
    owner: str,
    seconds: int,
    **overrides: object,
) -> bool:
    """Claim a lease with an explicit TTL, for readability at the call sites below."""
    return await JobRepository(session).claim_lease(job_id, owner, ttl_seconds=seconds, **overrides)


async def test_first_claim_succeeds(session: AsyncSession, job_factory: object) -> None:
    """An unleased job can be claimed."""
    job = await JobRepository(session).create(job_factory())  # type: ignore[operator]
    assert await claim(session, job.id, "worker-1", 900) is True

    row = await JobRepository(session).get(job.id)
    assert row is not None
    assert row.lease_owner == "worker-1"
    assert row.lease_expires_at is not None


async def test_second_owner_cannot_take_a_live_lease(
    session: AsyncSession,
    second_session: AsyncSession,
    job_factory: object,
) -> None:
    """While a lease is live, a different owner is refused.

    The refusal is `False`, not an exception: losing a claim is an ordinary outcome of
    running more than one worker, and the caller's job is to move on to the next job.
    """
    job = await JobRepository(session).create(job_factory())  # type: ignore[operator]
    assert await claim(session, job.id, "worker-1", 900) is True
    await session.commit()

    assert await claim(second_session, job.id, "worker-2", 900) is False
    await second_session.rollback()

    row = await JobRepository(session).get(job.id)
    assert row is not None
    assert row.lease_owner == "worker-1", "a refused claim must not overwrite the owner"


async def test_second_owner_can_take_an_expired_lease(
    session: AsyncSession,
    second_session: AsyncSession,
    verification_session: AsyncSession,
    job_factory: object,
) -> None:
    """After the lease expires, a different owner may take it.

    This is the crash-recovery path: the first worker died without releasing, so the only
    thing that frees the job is the expiry the sweep looks for.
    """
    repository = JobRepository(session)
    job = await repository.create(job_factory())  # type: ignore[operator]

    past = utcnow() - timedelta(hours=2)
    assert await repository.claim_lease(job.id, "worker-1", ttl_seconds=60, now=past) is True
    # Commit before the second attempt. The claim is an ``UPDATE``, so the first session
    # holds a row lock until its transaction ends; leaving it open would make the second
    # claimant wait on the lock rather than on the lease.
    await session.commit()

    assert await claim(second_session, job.id, "worker-2", 900) is True
    await second_session.commit()

    refreshed = await JobRepository(verification_session).get(job.id)
    assert refreshed is not None
    assert refreshed.lease_owner == "worker-2"


async def test_same_owner_can_renew_its_own_lease(
    session: AsyncSession, job_factory: object
) -> None:
    """Re-claiming your own live lease succeeds, so a retry is idempotent.

    Without this, a worker that retried a claim after a network blip would be refused its
    own lease and would abandon work it was already running.
    """
    repository = JobRepository(session)
    job = await repository.create(job_factory())  # type: ignore[operator]

    assert await repository.claim_lease(job.id, "worker-1", ttl_seconds=60) is True
    first_expiry = (await repository.get(job.id)).lease_expires_at  # type: ignore[union-attr]

    assert await repository.claim_lease(job.id, "worker-1", ttl_seconds=900) is True
    renewed = await repository.get(job.id)
    assert renewed is not None
    assert renewed.lease_owner == "worker-1"
    assert renewed.lease_expires_at is not None
    assert first_expiry is not None
    assert renewed.lease_expires_at > first_expiry


async def test_claim_on_a_missing_job_is_false(session: AsyncSession) -> None:
    """Claiming a job that does not exist reports failure rather than raising."""
    assert await claim(session, "job_missing", "worker-1", 60) is False


async def test_release_clears_the_lease(session: AsyncSession, job_factory: object) -> None:
    """Releasing returns the job to the unclaimed pool."""
    repository = JobRepository(session)
    job = await repository.create(job_factory())  # type: ignore[operator]
    await repository.claim_lease(job.id, "worker-1", ttl_seconds=900)

    await repository.release_lease(job.id)

    row = await repository.get(job.id)
    assert row is not None
    assert row.lease_owner is None
    assert row.lease_expires_at is None


async def test_released_lease_can_be_taken_by_another_owner(
    session: AsyncSession, job_factory: object
) -> None:
    """After a clean release, the next worker may claim immediately."""
    repository = JobRepository(session)
    job = await repository.create(job_factory())  # type: ignore[operator]
    await repository.claim_lease(job.id, "worker-1", ttl_seconds=900)
    await repository.release_lease(job.id)

    assert await repository.claim_lease(job.id, "worker-2", ttl_seconds=900) is True


async def test_list_expired_leases_finds_the_abandoned_job(
    session: AsyncSession, job_factory: object
) -> None:
    """The sweep returns jobs whose lease has passed."""
    repository = JobRepository(session)
    job = await repository.create(job_factory())  # type: ignore[operator]
    past = utcnow() - timedelta(hours=2)
    await repository.claim_lease(job.id, "worker-1", ttl_seconds=60, now=past)

    expired = await repository.list_expired_leases(utcnow())

    assert [row.id for row in expired] == [job.id]


async def test_list_expired_leases_excludes_a_live_lease(
    session: AsyncSession, job_factory: object
) -> None:
    """A job whose lease is still live is not swept.

    Getting this wrong is the worst failure mode in the design: the sweep would reclaim a
    job that a healthy worker is actively verifying, and two workers would run it.
    """
    repository = JobRepository(session)
    job = await repository.create(job_factory())  # type: ignore[operator]
    await repository.claim_lease(job.id, "worker-1", ttl_seconds=900)

    assert await repository.list_expired_leases(utcnow()) == []


async def test_list_expired_leases_excludes_terminal_jobs(
    session: AsyncSession, job_factory: object
) -> None:
    """A finished job with a stale lease is not reclaimed.

    Reclaiming a completed job would resurrect finished work, so the sweep filters on
    active statuses as well as on expiry.
    """
    repository = JobRepository(session)
    past = utcnow() - timedelta(hours=2)
    job = await repository.create(  # type: ignore[operator]
        job_factory(status=JobStatus.COMPLETED, completed_at=utcnow())
    )
    await repository.claim_lease(job.id, "worker-1", ttl_seconds=60, now=past)

    assert await repository.list_expired_leases(utcnow()) == []


async def test_list_expired_leases_excludes_unleased_jobs(
    session: AsyncSession, job_factory: object
) -> None:
    """A job that was never leased is not swept: nothing abandoned it."""
    repository = JobRepository(session)
    await repository.create(job_factory())  # type: ignore[operator]
    assert await repository.list_expired_leases(utcnow()) == []


async def test_only_one_of_two_competing_claims_wins(
    session: AsyncSession,
    second_session: AsyncSession,
    job_factory: object,
) -> None:
    """Two owners claiming an unleased job: exactly one succeeds.

    Both sessions attempt the claim without either holding a transaction open, so this is
    a genuine race against the conditional ``UPDATE`` rather than a sequential pair of
    calls. The loser must not leave the lease in a half-written state.
    """
    repository = JobRepository(session)
    job = await repository.create(job_factory())  # type: ignore[operator]
    await session.commit()

    first = await JobRepository(session).claim_lease(job.id, "worker-1", ttl_seconds=900)
    await session.commit()

    second = await JobRepository(second_session).claim_lease(job.id, "worker-2", ttl_seconds=900)
    await second_session.rollback()

    assert [first, second] == [True, False]

    row = await JobRepository(session).get(job.id)
    assert row is not None
    assert row.lease_owner == "worker-1"
