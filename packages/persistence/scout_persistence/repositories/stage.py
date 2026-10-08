"""Stage-attempt persistence and the resolved-stage projection.

``job_stages`` is append-only in practice: a retry adds a row with a higher ``attempt``
rather than updating the failed one, which is what makes "retries are recorded"
(spec section 51) inspectable after the fact.

The method the rest of Scout leans on hardest is :meth:`StageRepository.resolved_stages`.
The job state machine decides whether a job may advance by asking which earlier stages
are *resolved* — completed or deliberately skipped with a recorded reason (spec section
12). That question is answered here, from rows, so a stage can never be "resolved" by a
caller simply asserting it is.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from scout_domain.enums import JobStatus
from scout_domain.ids import IdKind, new_id
from scout_domain.models.base import utcnow
from scout_domain.models.job import JobStage, JobStageOutcome
from scout_persistence.mapping import to_job_stage
from scout_persistence.repositories.base import BaseRepository, ensure_valid_columns
from scout_persistence.tables import JobStageRow

__all__ = ["RESOLVED_OUTCOMES", "StageRepository"]

#: Outcomes that count as a stage being dealt with. Derived from the domain enum so
#: that adding a resolved outcome (or reclassifying one) cannot leave the database
#: query behind.
RESOLVED_OUTCOMES: frozenset[JobStageOutcome] = frozenset(
    outcome for outcome in JobStageOutcome if outcome.is_resolved
)


class StageRepository(BaseRepository):
    """Reads and writes ``job_stages`` rows."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to ``session``."""
        super().__init__(session)

    async def start_stage(
        self,
        job_id: str,
        stage: JobStatus,
        attempt: int = 1,
        *,
        started_at: datetime | None = None,
    ) -> JobStageRow:
        """Record the start of one attempt at ``stage`` and return its row.

        The row is created with outcome ``RUNNING`` rather than left null: a stage that
        crashed mid-flight must be distinguishable from one that never started, and the
        reclaim sweep needs the ``started_at`` to attribute the time correctly.
        """
        now = started_at or utcnow()
        row = JobStageRow(
            id=new_id(IdKind.STAGE),
            job_id=job_id,
            stage=stage.value,
            outcome=JobStageOutcome.RUNNING.value,
            attempt=attempt,
            started_at=now,
            artifact_ids=[],
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def complete_stage(
        self,
        stage_id: str,
        outcome: JobStageOutcome,
        **fields: Any,
    ) -> JobStageRow:
        """Finish a stage attempt, recording the outcome and any extra columns.

        ``completed_at`` is filled when the outcome is resolved and the caller did not
        supply one: a ``SUCCEEDED`` row without a completion time is an invitation to
        compute durations from the wrong pair of timestamps later.
        """
        row = await self.session.get(JobStageRow, stage_id)
        if row is None:
            raise LookupError(f"no job stage {stage_id!r}")
        ensure_valid_columns(JobStageRow, fields)

        row.outcome = outcome.value
        for name, value in fields.items():
            setattr(row, name, value)
        if outcome.is_resolved and row.completed_at is None:
            row.completed_at = utcnow()
        if row.started_at is not None and row.completed_at is not None:
            elapsed = (row.completed_at - row.started_at).total_seconds()
            row.duration_seconds = max(0.0, elapsed)
        self.stamp(row)
        await self.session.flush()
        return row

    async def get(self, stage_id: str) -> JobStageRow | None:
        """Return the stage row with ``stage_id``, or ``None``."""
        return await self.session.get(JobStageRow, stage_id)

    async def list_for_job(self, job_id: str) -> list[JobStageRow]:
        """Return every stage attempt for ``job_id``, oldest first.

        Ordered by ``created_at`` and then ``id``: ULIDs sort by creation time, so the
        tiebreak is stable even when two attempts share a millisecond, and the job
        timeline never renders in a different order on two page loads.
        """
        result = await self.session.scalars(
            select(JobStageRow)
            .where(JobStageRow.job_id == job_id)
            .order_by(JobStageRow.created_at.asc(), JobStageRow.id.asc())
        )
        return list(result)

    async def latest_attempt(self, job_id: str, stage: JobStatus) -> JobStageRow | None:
        """Return the most recent attempt at ``stage`` for ``job_id``, or ``None``."""
        return await self.session.scalar(
            select(JobStageRow)
            .where(JobStageRow.job_id == job_id, JobStageRow.stage == stage.value)
            .order_by(JobStageRow.attempt.desc())
            .limit(1)
        )

    async def resolved_stages(self, job_id: str) -> frozenset[JobStatus]:
        """Return the stages of ``job_id`` that are complete or explicitly skipped.

        This is the input to :class:`~scout_domain.state.machine.TransitionContext`, and
        it is read from the database rather than passed in by the caller precisely so
        that a job cannot claim a stage is resolved when no row says so.
        """
        result = await self.session.scalars(
            select(JobStageRow.stage)
            .where(
                JobStageRow.job_id == job_id,
                JobStageRow.outcome.in_(sorted(outcome.value for outcome in RESOLVED_OUTCOMES)),
            )
            .distinct()
        )
        return frozenset(JobStatus(value) for value in result)

    async def mark_running_as_failed(
        self,
        job_id: str,
        *,
        error: str,
        now: datetime | None = None,
    ) -> int:
        """Fail any stage rows still marked ``RUNNING`` and return how many changed.

        Called by the reclaim sweep: if a worker died mid-stage, the row it left behind
        says ``RUNNING`` forever, and leaving it that way makes the job timeline show a
        stage that will never finish. Recording the reason and moving on is honest about
        what happened.
        """
        finished_at = now or utcnow()
        result = await self.session.execute(
            update(JobStageRow)
            .where(
                JobStageRow.job_id == job_id,
                JobStageRow.outcome == JobStageOutcome.RUNNING.value,
            )
            .values(
                outcome=JobStageOutcome.FAILED.value,
                error=error,
                completed_at=finished_at,
                updated_at=finished_at,
            )
            .returning(JobStageRow.id)
        )
        changed = len(result.all())
        await self.session.flush()
        return changed

    @staticmethod
    def to_domain(row: JobStageRow) -> JobStage:
        """Convert a stage row into its domain model."""
        return to_job_stage(row)
