"""Human authorization persistence (spec sections 46 and 74).

Two properties drive every method here.

**Approvals are never deleted.** When a pull request moves past an approval, the
decision stops being *valid* but must remain readable: an incident review asks "who
approved what, and when did it stop applying?" — a question a ``DELETE`` cannot answer.
Hence :meth:`ApprovalRepository.invalidate_for_job` and the two validity-scoped reads.

**Validity is bound to a commit.** :meth:`list_valid_for_commit` is the check the
orchestrator actually performs before treating a job as authorized: a valid approval for
an older commit does not authorize a newer one, which is the mechanism behind "old
approval = invalid; new verification = required" (spec section 74).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from scout_domain.enums import ActorType, ApprovalDecision
from scout_domain.ids import IdKind, new_id
from scout_domain.models.approval import Approval
from scout_domain.models.base import utcnow
from scout_persistence.mapping import from_approval, to_approval
from scout_persistence.repositories.base import BaseRepository
from scout_persistence.tables import ApprovalRow

__all__ = ["ApprovalRepository"]


class ApprovalRepository(BaseRepository):
    """Reads and writes ``approvals`` rows."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to ``session``."""
        super().__init__(session)

    async def record(self, approval: Approval) -> ApprovalRow:
        """Insert ``approval`` and return its row.

        The domain validator has already refused anything not decided by a human; the
        table's check constraint refuses it a second time. Neither check is redundant in
        practice: the validator protects the domain path, the constraint protects the
        database from any path that bypasses it.
        """
        row = from_approval(approval)
        self.session.add(row)
        await self.session.flush()
        return row

    async def record_decision(
        self,
        job_id: str,
        *,
        commit_sha: str,
        decision: ApprovalDecision | str,
        decided_by: str,
        comment: str = "",
        decided_at: datetime | None = None,
    ) -> ApprovalRow:
        """Build and insert a human approval in one call.

        ``decision`` is validated by :class:`~scout_domain.models.approval.Approval`
        rather than trusted, so an unrecognised value fails at the domain boundary with
        the permitted values named. ``decided_by_type`` is not a parameter: it is pinned
        to ``HUMAN`` here, so there is no code path through this repository that lets
        Scout approve its own work.
        """
        approval = Approval(
            id=new_id(IdKind.APPROVAL),
            job_id=job_id,
            commit_sha=commit_sha,
            decision=ApprovalDecision(decision),
            decided_by=decided_by,
            decided_by_type=ActorType.HUMAN,
            decided_at=decided_at or utcnow(),
            comment=comment,
        )
        return await self.record(approval)

    async def get(self, approval_id: str) -> ApprovalRow | None:
        """Return the approval row with ``approval_id``, or ``None``."""
        return await self.session.get(ApprovalRow, approval_id)

    async def latest_for_job(self, job_id: str) -> ApprovalRow | None:
        """Return the most recent approval for ``job_id``, valid or not.

        Invalid approvals are included on purpose: a caller asking "what did a human
        last say about this job?" wants the last word, not the last still-valid word.
        Use :meth:`list_valid_for_commit` to decide whether that word still authorizes
        the revision under review.
        """
        return await self.session.scalar(
            select(ApprovalRow)
            .where(ApprovalRow.job_id == job_id)
            .order_by(ApprovalRow.decided_at.desc(), ApprovalRow.id.desc())
            .limit(1)
        )

    async def list_valid_for_commit(self, job_id: str, commit_sha: str) -> list[ApprovalRow]:
        """Return approvals that still authorize ``commit_sha``, newest first.

        This is the commit-scoped form the orchestrator needs: a valid approval for a
        different commit is not an authorization for this revision, so filtering happens
        in SQL rather than by the caller remembering to compare SHAs.
        """
        result = await self.session.scalars(
            select(ApprovalRow)
            .where(
                ApprovalRow.job_id == job_id,
                ApprovalRow.commit_sha == commit_sha,
                ApprovalRow.invalidated_at.is_(None),
            )
            .order_by(ApprovalRow.decided_at.desc(), ApprovalRow.id.desc())
        )
        return list(result)

    async def list_valid_for_job(self, job_id: str) -> list[ApprovalRow]:
        """Return every still-valid approval for ``job_id``, newest first."""
        result = await self.session.scalars(
            select(ApprovalRow)
            .where(ApprovalRow.job_id == job_id, ApprovalRow.invalidated_at.is_(None))
            .order_by(ApprovalRow.decided_at.desc(), ApprovalRow.id.desc())
        )
        return list(result)

    async def list_for_job(self, job_id: str) -> list[ApprovalRow]:
        """Return every approval for ``job_id``, newest first, including invalid ones."""
        result = await self.session.scalars(
            select(ApprovalRow)
            .where(ApprovalRow.job_id == job_id)
            .order_by(ApprovalRow.decided_at.desc(), ApprovalRow.id.desc())
        )
        return list(result)

    async def invalidate_for_job(
        self,
        job_id: str,
        reason: str,
        *,
        at: datetime | None = None,
    ) -> int:
        """Invalidate every still-valid approval for ``job_id``; return the count.

        Called when a pull request moves past an approval. Bulk invalidation runs in one
        statement so that a job never sits in a state where *some* of its approvals have
        been invalidated and others have not — a job whose authorization is ambiguous is
        worse than one whose authorization is clearly gone.
        """
        if not reason:
            raise ValueError("invalidation requires a reason; an unexplained one is not auditable")
        invalidated_at = at or utcnow()
        result = await self.session.execute(
            update(ApprovalRow)
            .where(ApprovalRow.job_id == job_id, ApprovalRow.invalidated_at.is_(None))
            .values(
                invalidated_at=invalidated_at,
                invalidated_reason=reason,
                updated_at=invalidated_at,
            )
            .returning(ApprovalRow.id)
        )
        count = len(result.all())
        await self.session.flush()
        return count

    async def count_valid_for_commit(self, job_id: str, commit_sha: str) -> int:
        """Return how many valid approvals exist for one commit."""
        return len(await self.list_valid_for_commit(job_id, commit_sha))

    @staticmethod
    def to_domain(row: ApprovalRow) -> Approval:
        """Convert an approval row into its domain model."""
        return to_approval(row)
