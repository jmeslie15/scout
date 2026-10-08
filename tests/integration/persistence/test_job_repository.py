"""Job identity, PR-revision uniqueness, and status updates.

The uniqueness assertion here is the same guarantee the webhook path depends on, but
proved at the schema level: one verification job per ``(repository, pull request, head
sha)``. A redelivery, a queue replay, or two orchestrator replicas receiving the same
event must converge on one job (spec sections 10, 12, and 62).

The complementary case matters just as much: a *new* commit is a new revision, so it must
be allowed to create a second job. A constraint that was too broad — say on
``(repository, pull request)`` alone — would silently refuse to verify a force-push.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from scout_domain.enums import JobStatus
from scout_domain.models.base import utcnow
from scout_persistence.repositories.job import JobRepository

pytestmark = pytest.mark.integration


async def test_create_and_read_back(session: AsyncSession, job_factory: object) -> None:
    """A created job is retrievable by id, with its fields intact."""
    repository = JobRepository(session)
    job = job_factory()  # type: ignore[operator]
    row = await repository.create(job)

    assert row.id == job.id
    assert row.status == JobStatus.QUEUED.value
    assert row.head_sha == job.commits.head_sha

    fetched = await repository.get(job.id)
    assert fetched is not None
    assert JobRepository.to_domain(fetched) == job


async def test_find_by_commit_is_exact(session: AsyncSession, job_factory: object) -> None:
    """``find_by_commit`` matches on all three key columns."""
    repository = JobRepository(session)
    job = job_factory()  # type: ignore[operator]
    await repository.create(job)

    found = await repository.find_by_commit("acme/widgets", 42, job.commits.head_sha)
    assert found is not None
    assert (await repository.find_by_commit("acme/other", 42, job.commits.head_sha)) is None
    assert (await repository.find_by_commit("acme/widgets", 43, job.commits.head_sha)) is None
    assert (await repository.find_by_commit("acme/widgets", 42, "f" * 40)) is None


async def test_same_pr_revision_cannot_be_created_twice(
    session: AsyncSession, job_factory: object
) -> None:
    """A second job for the same PR revision violates the unique constraint."""
    repository = JobRepository(session)
    await repository.create(job_factory())  # type: ignore[operator]

    with pytest.raises(IntegrityError):
        await repository.create(job_factory())  # type: ignore[operator]
    await session.rollback()


async def test_a_new_commit_creates_a_new_job(session: AsyncSession, job_factory: object) -> None:
    """A different ``head_sha`` is a different revision and is allowed.

    This is the assertion that stops the uniqueness constraint from being too broad: a
    force-push must produce a fresh verification, not a refusal.
    """
    repository = JobRepository(session)
    first = await repository.create(job_factory(head_sha="b" * 40))  # type: ignore[operator]
    second = await repository.create(job_factory(head_sha="c" * 40))  # type: ignore[operator]

    assert first.id != second.id
    assert await repository.find_by_commit("acme/widgets", 42, "c" * 40) is not None


async def test_a_different_pull_request_creates_a_new_job(
    session: AsyncSession, job_factory: object
) -> None:
    """The key includes the pull request number."""
    repository = JobRepository(session)
    first = await repository.create(job_factory(pull_request_number=42))  # type: ignore[operator]
    second = await repository.create(job_factory(pull_request_number=43))  # type: ignore[operator]
    assert first.id != second.id


async def test_create_unless_exists_reports_the_winner(
    session: AsyncSession,
    second_session: AsyncSession,
    job_factory: object,
) -> None:
    """A duplicate revision is reported rather than raised, and the original is returned."""
    repository = JobRepository(session)
    job = job_factory()  # type: ignore[operator]
    created_row, created = await repository.create_unless_exists(job)
    assert created is True
    await session.commit()

    duplicate = job_factory(  # type: ignore[operator]
        full_name=job.repository_full_name, head_sha=job.commits.head_sha
    )
    existing_row, existing = await JobRepository(second_session).create_unless_exists(duplicate)
    assert existing is False
    assert existing_row.id == created_row.id
    await second_session.rollback()


async def test_update_status_persists_updated_at(
    session: AsyncSession, job_factory: object
) -> None:
    """``update_status`` moves the status and the timestamp together.

    An ``updated_at`` that did not move on a row whose status did is a lie in the audit
    trail, and it is the field an operator uses to tell a stalled job from a running one.
    """
    repository = JobRepository(session)
    row = await repository.create(job_factory())  # type: ignore[operator]
    original_updated_at = row.updated_at

    updated = await repository.update_status(row.id, JobStatus.PREPARING, started_at=utcnow())

    assert updated.status == JobStatus.PREPARING.value
    assert updated.started_at is not None
    assert updated.updated_at >= original_updated_at


async def test_update_status_rejects_an_unknown_field(
    session: AsyncSession, job_factory: object
) -> None:
    """A typo in ``**fields`` fails at the call site, naming the column.

    Silently ignoring it is how a caller believes it recorded a failure reason that was
    never written.
    """
    repository = JobRepository(session)
    row = await repository.create(job_factory())  # type: ignore[operator]

    with pytest.raises(ValueError, match="no column"):
        await repository.update_status(row.id, JobStatus.FAILED, failur_reason="typo")


async def test_update_status_on_a_missing_job_raises(session: AsyncSession) -> None:
    """Mutating a job that does not exist is an error, not a silent no-op."""
    with pytest.raises(LookupError):
        await JobRepository(session).update_status("job_missing", JobStatus.QUEUED)


async def test_count_by_status_includes_zeroes(session: AsyncSession, job_factory: object) -> None:
    """Every status is present in the count, with a zero default.

    A dashboard rendering a stable tile set should not have tiles appear and disappear as
    jobs move through the pipeline.
    """
    repository = JobRepository(session)
    await repository.create(job_factory(pull_request_number=1, status=JobStatus.QUEUED))  # type: ignore[operator]
    await repository.create(  # type: ignore[operator]
        job_factory(pull_request_number=2, status=JobStatus.TESTING, head_sha="c" * 40)
    )

    counts = await repository.count_by_status()

    assert set(counts) == set(JobStatus)
    assert counts[JobStatus.QUEUED] == 1
    assert counts[JobStatus.TESTING] == 1
    assert counts[JobStatus.COMPLETED] == 0


async def test_list_active_excludes_terminal_statuses(
    session: AsyncSession, job_factory: object
) -> None:
    """``list_active`` returns work the orchestrator may still pick up.

    Derived from :attr:`JobStatus.is_active`, so a status added later is included without
    anyone remembering to update a literal list.
    """
    repository = JobRepository(session)
    # ``CANCELLED`` requires a recorded reason in the domain model, which is the point:
    # a cancelled job with no explanation is indistinguishable from one that was dropped.
    statuses = [JobStatus.QUEUED, JobStatus.TESTING, JobStatus.COMPLETED, JobStatus.CANCELLED]
    for index, status in enumerate(statuses):
        extra: dict[str, object] = {}
        if status is JobStatus.CANCELLED:
            extra["cancel_reason"] = "superseded by a newer revision"
        await repository.create(  # type: ignore[operator]
            job_factory(
                pull_request_number=index + 1,
                head_sha=f"{index}" * 40,
                status=status,
                **extra,
            )
        )

    active = {row.status for row in await repository.list_active()}
    assert active == {JobStatus.QUEUED.value, JobStatus.TESTING.value}


async def test_list_recent_orders_newest_first(session: AsyncSession, job_factory: object) -> None:
    """``list_recent`` is newest first, with a stable tiebreak on id."""
    repository = JobRepository(session)
    for index in range(5):
        await repository.create(  # type: ignore[operator]
            job_factory(pull_request_number=index + 1, head_sha=f"{index}" * 40)
        )

    rows = await repository.list_recent(limit=3)
    assert len(rows) == 3
    assert rows == sorted(rows, key=lambda row: (row.created_at, row.id), reverse=True)


async def test_list_recent_paginates(session: AsyncSession, job_factory: object) -> None:
    """``offset`` moves through the list without repeating or skipping ids."""
    repository = JobRepository(session)
    for index in range(4):
        await repository.create(  # type: ignore[operator]
            job_factory(pull_request_number=index + 1, head_sha=f"{index}" * 40)
        )

    first_page = await repository.list_recent(limit=2, offset=0)
    second_page = await repository.list_recent(limit=2, offset=2)
    assert {row.id for row in first_page}.isdisjoint({row.id for row in second_page})
    assert len(first_page) + len(second_page) == 4


async def test_get_by_delivery_returns_the_newest_job(
    session: AsyncSession, job_factory: object
) -> None:
    """A single delivery can legitimately create several jobs; the newest is returned."""
    repository = JobRepository(session)
    delivery_id = "delivery-abc"
    await repository.create(  # type: ignore[operator]
        job_factory(pull_request_number=1, head_sha="b" * 40, github_delivery_id=delivery_id)
    )
    newest = await repository.create(  # type: ignore[operator]
        job_factory(pull_request_number=1, head_sha="c" * 40, github_delivery_id=delivery_id)
    )

    found = await repository.get_by_delivery(delivery_id)
    assert found is not None
    assert found.id == newest.id


def test_repository_full_name_must_match_the_repository_ref(job_factory: object) -> None:
    """The domain model refuses a job whose two spellings of the repository disagree.

    The row stores both (a JSON ``RepositoryRef`` and a denormalised column), so a
    disagreement would make ``find_by_commit`` and the report disagree about which
    repository a job belongs to.
    """
    with pytest.raises(ValueError, match="repository_full_name must match"):
        job_factory(  # type: ignore[operator]
            full_name="acme/widgets", repository={"full_name": "acme/other"}
        )
