"""Verification job persistence: creation, lease ownership, and status transitions.

Two responsibilities here are easy to get subtly wrong, and both are enforced by the
database rather than by convention:

* **One job per PR revision.** ``find_by_commit`` and ``create`` rely on the
  ``(repository_full_name, pull_request_number, head_sha)`` unique constraint. A webhook
  redelivery or a force-push race surfaces as an ``IntegrityError`` from PostgreSQL,
  which is the only check that holds when two orchestrator replicas receive the same
  event at the same instant.
* **Leases.** ``claim_lease`` is a single conditional ``UPDATE``, not read-then-write:
  a worker that reads an expired lease, thinks for a moment, and then writes would
  happily steal a lease another worker had already renewed. The ``WHERE`` clause makes
  PostgreSQL—not the worker—decide who wins, and the returned row count is the answer.

``list_expired_leases`` is the sweep that makes crash recovery (spec section 50)
possible: it returns jobs that *look* owned but whose owner has stopped renewing.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from scout_domain.enums import JobStatus
from scout_domain.ids import IdKind, new_id
from scout_domain.models.base import utcnow
from scout_domain.models.job import VerificationJob
from scout_persistence.mapping import from_verification_job, to_verification_job
from scout_persistence.repositories.base import BaseRepository, ensure_valid_columns
from scout_persistence.tables import JobRow

__all__ = ["JobRepository"]

#: Job statuses in which an expired lease is worth reclaiming. Derived from
#: :attr:`JobStatus.is_active` rather than written out, so a new active status is
#: reclaimable the day it is added and a terminal one is never resurrected by the
#: sweep.
ACTIVE_STATUSES: frozenset[JobStatus] = frozenset(
    status for status in JobStatus if status.is_active
)

#: Default lease length. Long enough to cover a slow stage (a full test suite in a
#: sandbox), short enough that a crashed worker's job is picked up while a human is
#: still watching the dashboard.
DEFAULT_LEASE_TTL_SECONDS = 900


class JobRepository(BaseRepository):
    """Reads and writes ``verification_jobs`` rows."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to ``session``."""
        super().__init__(session)

    # -- creation ---------------------------------------------------------------

    async def create(self, job: VerificationJob) -> JobRow:
        """Insert ``job`` and return its row.

        The identifier comes from the domain model; nothing here mints one. That is
        deliberate — a job id has to be known before the insert so it can appear in the
        queue message, the log line, and the GitHub check run that are written around it.
        """
        row = from_verification_job(job)
        self.session.add(row)
        await self.session.flush()
        return row

    async def create_unless_exists(self, job: VerificationJob) -> tuple[JobRow, bool]:
        """Insert ``job``, or return the existing row for the same PR revision.

        Returns ``(row, created)``. Used by the webhook path, where two deliveries of
        the same ``pull_request`` event can arrive concurrently and only one may create
        a job. The unique constraint decides; this method translates its violation into
        an ordinary "already exists" answer.
        """
        try:
            async with self.session.begin_nested():
                row = from_verification_job(job)
                self.session.add(row)
                await self.session.flush()
        except IntegrityError:
            existing = await self.find_by_commit(
                job.repository_full_name,
                job.pull_request_number,
                job.commits.head_sha,
            )
            if existing is None:  # pragma: no cover - the constraint guarantees a row
                raise
            return existing, False
        return row, True

    # -- reads ------------------------------------------------------------------

    async def get(self, job_id: str) -> JobRow | None:
        """Return the job row with ``job_id``, or ``None``."""
        return await self.session.get(JobRow, job_id)

    async def get_by_delivery(self, delivery_id: str) -> JobRow | None:
        """Return the most recent job created by ``delivery_id``, or ``None``.

        A delivery can legitimately create more than one job (a ``synchronize`` event
        for a PR with several new commits), so the newest is returned rather than
        assuming at most one.
        """
        return await self.session.scalar(
            select(JobRow)
            .where(JobRow.github_delivery_id == delivery_id)
            .order_by(JobRow.created_at.desc(), JobRow.id.desc())
            .limit(1)
        )

    async def find_by_commit(
        self,
        repository_full_name: str,
        pull_request_number: int,
        head_sha: str,
    ) -> JobRow | None:
        """Return the job for one PR revision, or ``None``."""
        return await self.session.scalar(
            select(JobRow).where(
                JobRow.repository_full_name == repository_full_name,
                JobRow.pull_request_number == pull_request_number,
                JobRow.head_sha == head_sha,
            )
        )

    async def list_for_pull_request(
        self,
        repository_full_name: str,
        pull_request_number: int,
        *,
        limit: int = 50,
    ) -> list[JobRow]:
        """Return every job recorded for one pull request, newest first."""
        result = await self.session.scalars(
            select(JobRow)
            .where(
                JobRow.repository_full_name == repository_full_name,
                JobRow.pull_request_number == pull_request_number,
            )
            .order_by(JobRow.created_at.desc())
            .limit(limit)
        )
        return list(result)

    async def list_recent(self, *, limit: int = 50, offset: int = 0) -> list[JobRow]:
        """Return recent jobs, newest first, for the dashboard list view."""
        result = await self.session.scalars(
            select(JobRow)
            .order_by(JobRow.created_at.desc(), JobRow.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result)

    async def list_active(self, *, limit: int = 100) -> list[JobRow]:
        """Return jobs the orchestrator is still expected to be working on."""
        result = await self.session.scalars(
            select(JobRow)
            .where(JobRow.status.in_(sorted(status.value for status in ACTIVE_STATUSES)))
            .order_by(JobRow.created_at.asc())
            .limit(limit)
        )
        return list(result)

    async def count_by_status(self) -> dict[JobStatus, int]:
        """Return the job count per status, including statuses with no jobs.

        Every enum member is present with a zero default so a dashboard can render a
        stable set of tiles without the counts appearing and disappearing.
        """
        result = await self.session.execute(
            select(JobRow.status, func.count()).group_by(JobRow.status)
        )
        counts = dict.fromkeys(JobStatus, 0)
        for row in result.all():
            counts[JobStatus(str(row[0]))] = int(row[1])
        return counts

    # -- mutations --------------------------------------------------------------

    async def update_status(
        self,
        job_id: str,
        new_status: JobStatus,
        **fields: Any,
    ) -> JobRow:
        """Move ``job_id`` to ``new_status`` and persist ``fields`` alongside it.

        The transition is *recorded*, not validated: the state machine in
        :mod:`scout_domain.state.machine` owns legality, and duplicating those rules
        here would give Scout two sources of truth that drift. What this method does
        guarantee is that ``updated_at`` moves and that an unknown field name is
        rejected before it reaches SQL.
        """
        row = await self._require(job_id)
        ensure_valid_columns(JobRow, fields)
        row.status = new_status.value
        for name, value in fields.items():
            setattr(row, name, value)
        self.stamp(row)
        await self.session.flush()
        return row

    async def claim_lease(
        self,
        job_id: str,
        owner: str,
        ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Try to take the worker lease on ``job_id``; return whether it succeeded.

        Succeeds only when the job is currently unleased, when the existing lease has
        expired, or when ``owner`` already holds it (which makes a retry idempotent).
        A live lease held by someone else is left untouched and ``False`` is returned —
        the caller must not start work it does not own.

        This is one atomic statement on purpose. A read-then-write version has a window
        in which two workers both observe an expired lease and both proceed, which is
        precisely the double-execution the lease exists to prevent.
        """
        started_at = now or utcnow()
        result = await self.session.execute(
            update(JobRow)
            .where(
                JobRow.id == job_id,
                JobRow.lease_expires_at.is_(None)
                | (JobRow.lease_expires_at <= started_at)
                | (JobRow.lease_owner == owner),
            )
            .values(
                lease_owner=owner,
                lease_expires_at=started_at + timedelta(seconds=ttl_seconds),
                updated_at=started_at,
            )
            .returning(JobRow.id)
        )
        claimed = result.first() is not None
        await self.session.flush()
        return claimed

    async def release_lease(self, job_id: str, *, now: datetime | None = None) -> None:
        """Clear the lease on ``job_id``.

        Called when a worker finishes or deliberately gives up. It is not called on a
        crash — nothing runs then — which is exactly why the expiry sweep exists as the
        second half of the recovery story.
        """
        await self.session.execute(
            update(JobRow)
            .where(JobRow.id == job_id)
            .values(
                lease_owner=None,
                lease_expires_at=None,
                updated_at=now or utcnow(),
            )
        )
        await self.session.flush()

    async def list_expired_leases(
        self,
        now: datetime,
        *,
        limit: int = 100,
    ) -> list[JobRow]:
        """Return active jobs whose lease expired at or before ``now``.

        Restricted to active statuses: a completed job that still carries a lease is a
        bookkeeping artefact, not work to resume, and reclaiming it would resurrect a
        finished verification.
        """
        result = await self.session.scalars(
            select(JobRow)
            .where(
                JobRow.lease_expires_at.is_not(None),
                JobRow.lease_expires_at <= now,
                JobRow.status.in_(sorted(status.value for status in ACTIVE_STATUSES)),
            )
            .order_by(JobRow.lease_expires_at.asc())
            .limit(limit)
        )
        return list(result)

    async def record_attempt(self, job_id: str, *, attempt: int) -> JobRow:
        """Set the job's retry counter (spec section 51)."""
        row = await self._require(job_id)
        row.attempt = attempt
        self.stamp(row)
        await self.session.flush()
        return row

    async def _require(self, job_id: str) -> JobRow:
        """Return the row for ``job_id`` or raise ``LookupError``.

        Mutating a job that does not exist is a programming error, and returning
        ``None`` would push the failure to a caller that has no better context to
        report it from.
        """
        row = await self.session.get(JobRow, job_id)
        if row is None:
            raise LookupError(f"no verification job {job_id!r}")
        return row

    # -- convenience ------------------------------------------------------------

    @staticmethod
    def mint_id() -> str:
        """Return a new ``job_`` identifier.

        Exposed so the orchestrator and the webhook handler mint ids the same way and
        with the same prefix; a job id assembled by hand elsewhere would be a silent
        contract break with the rest of Scout.
        """
        return new_id(IdKind.JOB)

    @staticmethod
    def to_domain(row: JobRow) -> VerificationJob:
        """Convert a row into its domain model, for callers that already have the row."""
        return to_verification_job(row)
