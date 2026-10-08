"""Engineering-decision persistence (spec section 29).

A decision is the strongest memory Scout holds: when a human has said "this is
intentional, stop flagging it", that statement has to outrank every inference Scout can
make. It therefore lives in its own table rather than as a knowledge document, because a
decision carries provenance a document does not — which question was asked, which answer
resolved it, which job it came from — and because reversal is modelled as a chain
(``supersedes_id`` / ``superseded_by_id``) rather than as an edit.

The read path that matters is :meth:`EngineeringDecisionRepository.list_active_for_scope`,
which is what retrieval calls before an agent is allowed to re-raise a settled issue.
"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from scout_domain.models.knowledge import EngineeringDecision
from scout_persistence.mapping import from_engineering_decision, to_engineering_decision
from scout_persistence.repositories.base import BaseRepository
from scout_persistence.tables import EngineeringDecisionRow

__all__ = ["EngineeringDecisionRepository"]


class EngineeringDecisionRepository(BaseRepository):
    """Reads and writes ``engineering_decisions`` rows."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to ``session``."""
        super().__init__(session)

    async def create(self, decision: EngineeringDecision) -> EngineeringDecisionRow:
        """Insert a decision and return its row."""
        row = from_engineering_decision(decision)
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, decision_id: str) -> EngineeringDecisionRow | None:
        """Return the decision row with ``decision_id``, or ``None``."""
        return await self.session.get(EngineeringDecisionRow, decision_id)

    async def list_active_for_scope(
        self,
        scope: str,
        *,
        limit: int = 100,
    ) -> list[EngineeringDecisionRow]:
        """Return active decisions applying to ``scope`` plus organisation-wide ones.

        ``scope`` is ``owner/name`` or ``*``. Filtering here rather than in the caller is
        what stops a decision about one service being applied to an unrelated one, while
        still applying org-wide policy everywhere.
        """
        result = await self.session.scalars(
            select(EngineeringDecisionRow)
            .where(
                or_(
                    EngineeringDecisionRow.scope == scope,
                    EngineeringDecisionRow.scope == "*",
                ),
                EngineeringDecisionRow.superseded_by_id.is_(None),
            )
            .order_by(EngineeringDecisionRow.decided_at.desc())
            .limit(limit)
        )
        return list(result)

    async def list_for_job(self, job_id: str) -> list[EngineeringDecisionRow]:
        """Return decisions recorded against ``job_id``."""
        result = await self.session.scalars(
            select(EngineeringDecisionRow)
            .where(EngineeringDecisionRow.source_job_id == job_id)
            .order_by(EngineeringDecisionRow.decided_at.asc())
        )
        return list(result)

    async def list_for_question(self, question_id: str) -> list[EngineeringDecisionRow]:
        """Return decisions derived from ``question_id``."""
        result = await self.session.scalars(
            select(EngineeringDecisionRow)
            .where(EngineeringDecisionRow.source_question_id == question_id)
            .order_by(EngineeringDecisionRow.decided_at.asc())
        )
        return list(result)

    async def supersede(
        self,
        decision_id: str,
        *,
        superseded_by_id: str,
    ) -> EngineeringDecisionRow:
        """Mark a decision superseded and back-link the replacement.

        Both directions are written in one place so a chain can be walked either way:
        forward from the original to its replacement, or backward to find what a change
        replaced. Writing only one direction is the classic way an audit trail becomes a
        dead end.
        """
        row = await self.session.get(EngineeringDecisionRow, decision_id)
        if row is None:
            raise LookupError(f"no engineering decision {decision_id!r}")
        replacement = await self.session.get(EngineeringDecisionRow, superseded_by_id)
        if replacement is None:
            raise LookupError(
                f"cannot supersede {decision_id!r} with unknown decision {superseded_by_id!r}"
            )
        row.superseded_by_id = superseded_by_id
        replacement.supersedes_id = decision_id
        self.stamp(row)
        self.stamp(replacement)
        await self.session.flush()
        return row

    async def link_knowledge_document(
        self,
        decision_id: str,
        document_id: str,
    ) -> EngineeringDecisionRow:
        """Record that a knowledge document was derived from ``decision_id``."""
        row = await self.session.get(EngineeringDecisionRow, decision_id)
        if row is None:
            raise LookupError(f"no engineering decision {decision_id!r}")
        existing = list(row.knowledge_document_ids or [])
        if document_id not in existing:
            existing.append(document_id)
            row.knowledge_document_ids = existing
        self.stamp(row)
        await self.session.flush()
        return row

    @staticmethod
    def to_domain(row: EngineeringDecisionRow) -> EngineeringDecision:
        """Convert a decision row into its domain model."""
        return to_engineering_decision(row)
