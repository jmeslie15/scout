"""Tool-run persistence (spec section 21).

A tool run records *what was executed* and *where its output went*, never the output
itself. ``invocation`` keeps the resolved argv and timeout so a result can be
reproduced; ``stdout_artifact_id`` and friends point at the bytes.

:meth:`ToolRunRepository.record_result` takes an optional
:class:`~scout_domain.models.tool.ToolInvocation` and derives the capability the run was
authorized under from it. That matters for audit: a run recorded without a capability
would be indistinguishable from one that bypassed the capability check.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scout_domain.models.tool import ToolInvocation, ToolResult
from scout_persistence.mapping import (
    from_tool_result,
    invocation_to_json,
    to_tool_result,
    tool_run_capability,
)
from scout_persistence.repositories.base import BaseRepository
from scout_persistence.tables import ToolRunRow

__all__ = ["ToolRunRepository"]


class ToolRunRepository(BaseRepository):
    """Reads and writes ``tool_runs`` rows."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to ``session``."""
        super().__init__(session)

    async def record(self, result: ToolResult) -> ToolRunRow:
        """Insert a tool result with no invocation context.

        Kept for replaying persisted results; live execution should use
        :meth:`record_result` so the capability and argv are recorded alongside.
        """
        row = from_tool_result(result)
        self.session.add(row)
        await self.session.flush()
        return row

    async def record_result(
        self,
        result: ToolResult,
        *,
        invocation: ToolInvocation | None = None,
    ) -> ToolRunRow:
        """Insert a tool result together with the invocation that produced it."""
        row = from_tool_result(result)
        if invocation is not None:
            row.invocation = invocation_to_json(invocation)
            row.capability = tool_run_capability(invocation)
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, tool_run_id: str) -> ToolRunRow | None:
        """Return the tool-run row with ``tool_run_id``, or ``None``."""
        return await self.session.get(ToolRunRow, tool_run_id)

    async def list_for_job(self, job_id: str, *, tool: str | None = None) -> list[ToolRunRow]:
        """Return tool runs for ``job_id`` in execution order, optionally by tool name."""
        statement = select(ToolRunRow).where(ToolRunRow.job_id == job_id)
        if tool is not None:
            statement = statement.where(ToolRunRow.tool == tool)
        statement = statement.order_by(ToolRunRow.started_at.asc(), ToolRunRow.id.asc())
        result = await self.session.scalars(statement)
        return list(result)

    async def latest_for_tool(self, job_id: str, tool: str) -> ToolRunRow | None:
        """Return the most recent run of ``tool`` for ``job_id``, or ``None``.

        Retries make this the interesting read: the report quotes the newest attempt, not
        the first failure, and the earlier attempts stay available through
        :meth:`list_for_job`.
        """
        return await self.session.scalar(
            select(ToolRunRow)
            .where(ToolRunRow.job_id == job_id, ToolRunRow.tool == tool)
            .order_by(ToolRunRow.started_at.desc(), ToolRunRow.id.desc())
            .limit(1)
        )

    async def list_for_sandbox(self, sandbox_run_id: str) -> list[ToolRunRow]:
        """Return the tool runs executed inside one sandbox, in order."""
        result = await self.session.scalars(
            select(ToolRunRow)
            .where(ToolRunRow.sandbox_run_id == sandbox_run_id)
            .order_by(ToolRunRow.started_at.asc())
        )
        return list(result)

    @staticmethod
    def to_domain(row: ToolRunRow) -> ToolResult:
        """Convert a tool-run row into its domain model."""
        return to_tool_result(row)
