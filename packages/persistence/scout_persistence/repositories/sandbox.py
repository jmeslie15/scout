"""Sandbox-run persistence (spec sections 35 and 53).

A sandbox run records the isolation that was *actually enforced* for one job. ``limits``
and ``policy`` are snapshots taken at creation, so tightening a limit or forbidding a
new environment variable later does not retroactively rewrite the record of what ran
before. That property is the whole point of storing them here rather than joining to
current configuration.

Sandbox runs must always be closed: a run left ``RUNNING`` after a crash is exactly the
leak this table exists to make findable, which is why :meth:`SandboxRunRepository.complete`
requires an outcome and :meth:`list_orphaned` exists for the reaper.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scout_domain.models.base import utcnow
from scout_domain.models.sandbox import SandboxRun, SandboxStatus
from scout_persistence.mapping import from_sandbox_run, to_sandbox_run
from scout_persistence.repositories.base import BaseRepository
from scout_persistence.tables import SandboxRunRow

__all__ = ["SandboxRunRepository"]

#: Statuses that mean the sandbox is finished.
#:
#: ``SandboxStatus`` has no ``is_terminal`` property of its own — it lives on
#: :class:`~scout_domain.models.sandbox.SandboxRun`, which cannot be evaluated against a
#: bare enum member. Listing them here is therefore unavoidable; the unit test
#: ``test_sandbox_terminal_statuses_match_domain`` asserts this set agrees with
#: ``SandboxRun.is_terminal`` so the two cannot drift silently.
TERMINAL_STATUSES: Final[frozenset[SandboxStatus]] = frozenset(
    {
        SandboxStatus.SUCCEEDED,
        SandboxStatus.FAILED,
        SandboxStatus.TIMEOUT,
        SandboxStatus.ERROR,
        SandboxStatus.CANCELLED,
    }
)


class SandboxRunRepository(BaseRepository):
    """Reads and writes ``sandbox_runs`` rows."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to ``session``."""
        super().__init__(session)

    async def create(self, run: SandboxRun) -> SandboxRunRow:
        """Insert a sandbox run together with its limits and policy snapshot."""
        row = from_sandbox_run(run)
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, sandbox_run_id: str) -> SandboxRunRow | None:
        """Return the sandbox-run row with ``sandbox_run_id``, or ``None``."""
        return await self.session.get(SandboxRunRow, sandbox_run_id)

    async def list_for_job(self, job_id: str) -> list[SandboxRunRow]:
        """Return sandbox runs for ``job_id``, oldest first."""
        result = await self.session.scalars(
            select(SandboxRunRow)
            .where(SandboxRunRow.job_id == job_id)
            .order_by(SandboxRunRow.started_at.asc(), SandboxRunRow.id.asc())
        )
        return list(result)

    async def latest_for_job(self, job_id: str) -> SandboxRunRow | None:
        """Return the most recent sandbox run for ``job_id``, or ``None``."""
        return await self.session.scalar(
            select(SandboxRunRow)
            .where(SandboxRunRow.job_id == job_id)
            .order_by(SandboxRunRow.started_at.desc(), SandboxRunRow.id.desc())
            .limit(1)
        )

    async def mark_running(
        self,
        sandbox_run_id: str,
        *,
        native_id: str | None = None,
        workspace_path: str | None = None,
    ) -> SandboxRunRow:
        """Record that a sandbox has been created and is now executing.

        ``native_id`` (container id or process group) is captured here rather than at
        creation because it does not exist until the backend has actually started
        something, and it is the only handle a reaper has for killing a leak.
        """
        row = await self.session.get(SandboxRunRow, sandbox_run_id)
        if row is None:
            raise LookupError(f"no sandbox run {sandbox_run_id!r}")
        row.status = SandboxStatus.RUNNING.value
        if native_id is not None:
            row.native_id = native_id
        if workspace_path is not None:
            row.workspace_path = workspace_path
        self.stamp(row)
        await self.session.flush()
        return row

    async def complete(
        self,
        sandbox_run_id: str,
        *,
        status: SandboxStatus,
        error: str | None = None,
        completed_at: datetime | None = None,
    ) -> SandboxRunRow:
        """Close a sandbox run with its terminal status.

        Raises for a non-terminal status: letting a caller "complete" a run into
        ``PENDING`` would produce a row that the reaper never picks up and no operator
        can explain.
        """
        if status not in TERMINAL_STATUSES:
            raise ValueError(
                f"{status.value} is not a terminal sandbox status; "
                f"expected one of {', '.join(sorted(s.value for s in TERMINAL_STATUSES))}"
            )
        row = await self.session.get(SandboxRunRow, sandbox_run_id)
        if row is None:
            raise LookupError(f"no sandbox run {sandbox_run_id!r}")
        finished_at = completed_at or utcnow()
        row.status = status.value
        row.completed_at = finished_at
        if row.started_at is not None:
            row.duration_seconds = max(0.0, (finished_at - row.started_at).total_seconds())
        if error is not None:
            row.error = error
        self.stamp(row)
        await self.session.flush()
        return row

    async def append_tool_run(self, sandbox_run_id: str, tool_run_id: str) -> SandboxRunRow:
        """Append a tool run id to the sandbox's ordered list.

        The list is kept on the sandbox row as well as on each tool run so that rendering
        a sandbox's activity is one read rather than a join per command, and so the order
        of execution is preserved even though tool runs also carry their own timestamps.
        """
        row = await self.session.get(SandboxRunRow, sandbox_run_id)
        if row is None:
            raise LookupError(f"no sandbox run {sandbox_run_id!r}")
        existing = list(row.tool_run_ids or [])
        if tool_run_id not in existing:
            existing.append(tool_run_id)
            row.tool_run_ids = existing
        self.stamp(row)
        await self.session.flush()
        return row

    async def list_orphaned(
        self,
        *,
        started_before: datetime,
        limit: int = 100,
    ) -> list[SandboxRunRow]:
        """Return non-terminal sandbox runs started before ``started_before``.

        These are the leaks: a sandbox that never reported an outcome, because the worker
        died or the backend lost track of it. ``native_id`` is what lets the reaper kill
        whatever is still running.
        """
        result = await self.session.scalars(
            select(SandboxRunRow)
            .where(
                SandboxRunRow.status.in_(
                    [status.value for status in SandboxStatus if status not in TERMINAL_STATUSES]
                ),
                SandboxRunRow.started_at < started_before,
            )
            .order_by(SandboxRunRow.started_at.asc())
            .limit(limit)
        )
        return list(result)

    @staticmethod
    def to_domain(row: SandboxRunRow) -> SandboxRun:
        """Convert a sandbox-run row into its domain model."""
        return to_sandbox_run(row)
