"""Approval persistence: commit binding, invalidation, and the audit trail.

Two rules are enforced here that make an approval worth anything (spec sections 46, 74):

1. **An approval authorizes one exact commit.** A valid approval for an older commit does
   not authorize a newer one, so the commit-scoped read is the one the orchestrator must
   use, and it is the one tested hardest here.
2. **Invalidation preserves the record.** When the pull request moves, the decision stops
   being valid but is never deleted. An incident review has to be able to answer "who
   approved what, and when did it stop applying?", and a ``DELETE`` cannot answer that.

The test that ``invalidate_for_job`` clears *prior* approvals is the one that keeps a stale
"approve" from silently authorizing a revision nobody reviewed.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from scout_domain.enums import ActorType, ApprovalDecision
from scout_domain.models.base import utcnow
from scout_persistence.repositories.approval import ApprovalRepository
from scout_persistence.tables import ApprovalRow

pytestmark = pytest.mark.integration

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


async def _job(session: AsyncSession, job_factory: object, **overrides: object) -> object:
    """Create a parent job, which ``approvals.job_id`` references."""
    from scout_persistence.repositories.job import JobRepository

    job = job_factory(**overrides)  # type: ignore[operator]
    await JobRepository(session).create(job)
    return job


async def test_record_and_read_back(
    session: AsyncSession, job_factory: object, approval_factory: object
) -> None:
    """A recorded approval is retrievable as a domain model."""
    job = await _job(session, job_factory)
    repository = ApprovalRepository(session)
    approval = approval_factory(job_id=job.id, commit_sha=COMMIT_B)  # type: ignore[operator]

    row = await repository.record(approval)

    assert row.id == approval.id
    assert row.commit_sha == COMMIT_B
    assert row.decision == ApprovalDecision.APPROVE.value
    assert row.decided_by_type == ActorType.HUMAN.value
    assert ApprovalRepository.to_domain(row) == approval


async def test_record_decision_builds_a_human_approval(
    session: AsyncSession, job_factory: object
) -> None:
    """The convenience path mints an ``apv_`` id and pins the actor to ``HUMAN``."""
    job = await _job(session, job_factory)
    row = await ApprovalRepository(session).record_decision(
        job.id,
        commit_sha=COMMIT_B,
        decision=ApprovalDecision.APPROVE,
        decided_by="dana",
        comment="Walked the export path by hand.",
    )
    assert row.id.startswith("apv_")
    assert row.decided_by_type == ActorType.HUMAN.value


async def test_invalid_decision_is_rejected(session: AsyncSession, job_factory: object) -> None:
    """An unrecognised decision fails at the domain boundary and names the vocabulary."""
    job = await _job(session, job_factory)
    with pytest.raises(ValueError, match="not a valid ApprovalDecision"):
        await ApprovalRepository(session).record_decision(
            job.id, commit_sha=COMMIT_B, decision="LGTM", decided_by="dana"
        )


async def test_domain_refuses_a_non_human_approval(
    session: AsyncSession, job_factory: object
) -> None:
    """Scout cannot approve its own work (spec section 46).

    The domain model is the first line of defence: an ``Approval`` whose actor is not
    ``HUMAN`` cannot be constructed at all, so there is no code path that records one.
    """
    from scout_domain.models.approval import Approval

    job = await _job(session, job_factory)
    with pytest.raises(ValueError, match="HUMAN"):
        Approval(
            id="apv_01J8ZQ4K2M7X3N9PQRSTVWXY0",
            job_id=job.id,
            commit_sha=COMMIT_B,
            decision=ApprovalDecision.APPROVE,
            decided_by="scout",
            decided_by_type=ActorType.AGENT,
        )


async def test_database_refuses_an_unknown_actor_type(
    session: AsyncSession, job_factory: object
) -> None:
    """The second line of defence: the column's CHECK constraint.

    The domain validator protects the domain path; the constraint protects the database
    from any path that bypasses it, including a future migration script or a direct write.
    """
    job = await _job(session, job_factory)
    session.add(
        ApprovalRow(
            id="apv_01J8ZQ4K2M7X3N9PQRSTVWXY0",
            job_id=job.id,
            commit_sha=COMMIT_B,
            decision=ApprovalDecision.APPROVE.value,
            decided_by="scout",
            decided_by_type="NOT_AN_ACTOR_TYPE",
        )
    )
    with pytest.raises(IntegrityError):
        await session.flush()
    await session.rollback()


async def test_latest_for_job_returns_the_newest_including_invalid(
    session: AsyncSession, job_factory: object, approval_factory: object
) -> None:
    """``latest_for_job`` answers "what did a human last say?", valid or not."""
    job = await _job(session, job_factory)
    repository = ApprovalRepository(session)
    first = approval_factory(job_id=job.id, commit_sha=COMMIT_A)  # type: ignore[operator]
    await repository.record(first)
    newest = approval_factory(  # type: ignore[operator]
        job_id=job.id, commit_sha=COMMIT_B, decided_at=utcnow() + timedelta(minutes=5)
    )
    await repository.record(newest)

    latest = await repository.latest_for_job(job.id)
    assert latest is not None
    assert latest.id == newest.id


async def test_list_valid_for_commit_is_commit_scoped(
    session: AsyncSession, job_factory: object, approval_factory: object
) -> None:
    """A valid approval for another commit does not authorize this one (spec section 74)."""
    job = await _job(session, job_factory)
    repository = ApprovalRepository(session)
    await repository.record(approval_factory(job_id=job.id, commit_sha=COMMIT_A))  # type: ignore[operator]

    assert len(await repository.list_valid_for_commit(job.id, COMMIT_A)) == 1
    assert await repository.list_valid_for_commit(job.id, COMMIT_B) == []
    assert await repository.count_valid_for_commit(job.id, COMMIT_A) == 1
    assert await repository.count_valid_for_commit(job.id, COMMIT_B) == 0


async def test_list_valid_for_commit_excludes_invalidated(
    session: AsyncSession, job_factory: object, approval_factory: object
) -> None:
    """An invalidated approval is not returned by the commit-scoped read."""
    job = await _job(session, job_factory)
    repository = ApprovalRepository(session)
    approval = approval_factory(job_id=job.id, commit_sha=COMMIT_B)  # type: ignore[operator]
    await repository.record(approval)
    await repository.invalidate_for_job(job.id, "PR moved to a new commit")

    assert await repository.list_valid_for_commit(job.id, COMMIT_B) == []
    assert await repository.list_valid_for_job(job.id) == []


async def test_invalidate_for_job_clears_validity_of_prior_approvals(
    session: AsyncSession,
    job_factory: object,
    approval_factory: object,
    verification_session: AsyncSession,
) -> None:
    """Every still-valid approval for the job is invalidated, and the rows are retained.

    This is the test that keeps a stale approval from authorizing an unreviewed revision:
    after the PR moves, no approval for this job is valid any more, on any commit. The
    records themselves survive, because the audit trail has to say what was authorized.
    """
    job = await _job(session, job_factory)
    repository = ApprovalRepository(session)
    await repository.record(approval_factory(job_id=job.id, commit_sha=COMMIT_A))  # type: ignore[operator]
    await repository.record(approval_factory(job_id=job.id, commit_sha=COMMIT_B))  # type: ignore[operator]

    invalidated = await repository.invalidate_for_job(job.id, "PR moved from b to c", at=utcnow())

    assert invalidated == 2
    await session.commit()

    reader = ApprovalRepository(verification_session)
    assert await reader.list_valid_for_commit(job.id, COMMIT_A) == []
    assert await reader.list_valid_for_commit(job.id, COMMIT_B) == []

    all_rows = await reader.list_for_job(job.id)
    assert len(all_rows) == 2, "invalidation must not delete the record"
    for row in all_rows:
        assert row.invalidated_at is not None
        assert row.invalidated_reason == "PR moved from b to c"
        assert row.decision == ApprovalDecision.APPROVE.value


async def test_invalidation_leaves_other_jobs_alone(
    session: AsyncSession,
    job_factory: object,
    approval_factory: object,
    verification_session: AsyncSession,
) -> None:
    """Invalidation is scoped to one job, so a sibling PR keeps its authorization."""
    repository = ApprovalRepository(session)
    first = await _job(session, job_factory, head_sha="c" * 40)
    second = await _job(session, job_factory, pull_request_number=99, head_sha=COMMIT_B)
    await repository.record(  # type: ignore[operator]
        approval_factory(job_id=first.id, commit_sha="c" * 40)
    )
    await repository.record(approval_factory(job_id=second.id, commit_sha=COMMIT_B))  # type: ignore[operator]

    await repository.invalidate_for_job(first.id, "PR moved")
    await session.commit()

    reader = ApprovalRepository(verification_session)
    assert await reader.list_valid_for_commit(first.id, "c" * 40) == []
    assert len(await reader.list_valid_for_commit(second.id, COMMIT_B)) == 1


async def test_invalidation_requires_a_reason(
    session: AsyncSession, job_factory: object, approval_factory: object
) -> None:
    """Invalidating without a reason is refused: an unexplained invalidation is not auditable."""
    job = await _job(session, job_factory)
    repository = ApprovalRepository(session)
    await repository.record(approval_factory(job_id=job.id, commit_sha=COMMIT_B))  # type: ignore[operator]

    with pytest.raises(ValueError, match="reason"):
        await repository.invalidate_for_job(job.id, "")


async def test_invalidation_is_idempotent(
    session: AsyncSession, job_factory: object, approval_factory: object
) -> None:
    """A second invalidation changes nothing and reports zero rows.

    Recording the same fact twice must not overwrite the original reason, which is what
    the first invalidation date and explanation are for.
    """
    job = await _job(session, job_factory)
    repository = ApprovalRepository(session)
    await repository.record(approval_factory(job_id=job.id, commit_sha=COMMIT_B))  # type: ignore[operator]

    assert await repository.invalidate_for_job(job.id, "first reason") == 1
    assert await repository.invalidate_for_job(job.id, "second reason") == 0

    row = (await repository.list_for_job(job.id))[0]
    assert row.invalidated_reason == "first reason"


async def test_invalidation_before_the_decision_is_rejected(
    session: AsyncSession, job_factory: object, approval_factory: object
) -> None:
    """The database refuses an invalidation that predates the decision it invalidates."""
    job = await _job(session, job_factory)
    repository = ApprovalRepository(session)
    decided_at = utcnow()
    await repository.record(  # type: ignore[operator]
        approval_factory(job_id=job.id, commit_sha=COMMIT_B, decided_at=decided_at)
    )

    with pytest.raises(IntegrityError):
        await repository.invalidate_for_job(job.id, "backdated", at=decided_at - timedelta(hours=1))
    await session.rollback()


async def test_request_changes_is_recorded_as_such(
    session: AsyncSession, job_factory: object, approval_factory: object
) -> None:
    """A non-approval decision is preserved, not coerced into an approval."""
    job = await _job(session, job_factory)
    repository = ApprovalRepository(session)
    row = await repository.record(  # type: ignore[operator]
        approval_factory(
            job_id=job.id, commit_sha=COMMIT_B, decision=ApprovalDecision.REQUEST_CHANGES
        )
    )
    assert row.decision == ApprovalDecision.REQUEST_CHANGES.value
    assert ApprovalRepository.to_domain(row).is_valid is True
