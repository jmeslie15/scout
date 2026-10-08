"""Agent-run persistence: reproducibility and cost accounting (spec sections 32, 38).

An agent run is the unit of both *provenance* and *cost*. ``prompt_version`` pins which
template produced a result (spec section 39), and the token and cost columns answer "how
much does Scout cost per verified pull request?" (spec section 59).

The context that an agent saw is deliberately not stored — only ``context_digest``.
Agent context contains repository text, and a table of it would be both enormous and a
new place secrets could land. A digest is sufficient to prove two runs saw the same
input.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scout_domain.enums import AgentRole, AgentStatus
from scout_domain.models.agent import AgentRun
from scout_domain.models.base import utcnow
from scout_persistence.mapping import from_agent_run, to_agent_run
from scout_persistence.repositories.base import BaseRepository
from scout_persistence.tables import AgentRunRow

__all__ = ["AgentRunRepository"]


class AgentRunRepository(BaseRepository):
    """Reads and writes ``agent_runs`` rows."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to ``session``."""
        super().__init__(session)

    async def create(self, run: AgentRun) -> AgentRunRow:
        """Insert an agent run and return its row."""
        row = from_agent_run(run)
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, agent_run_id: str) -> AgentRunRow | None:
        """Return the agent-run row with ``agent_run_id``, or ``None``."""
        return await self.session.get(AgentRunRow, agent_run_id)

    async def list_for_job(
        self,
        job_id: str,
        *,
        role: AgentRole | None = None,
    ) -> list[AgentRunRow]:
        """Return agent runs for ``job_id`` in execution order, optionally by role."""
        statement = select(AgentRunRow).where(AgentRunRow.job_id == job_id)
        if role is not None:
            statement = statement.where(AgentRunRow.role == role.value)
        statement = statement.order_by(AgentRunRow.started_at.asc(), AgentRunRow.id.asc())
        result = await self.session.scalars(statement)
        return list(result)

    async def latest_for_role(self, job_id: str, role: AgentRole) -> AgentRunRow | None:
        """Return the newest run of ``role`` for ``job_id``, or ``None``."""
        return await self.session.scalar(
            select(AgentRunRow)
            .where(AgentRunRow.job_id == job_id, AgentRunRow.role == role.value)
            .order_by(AgentRunRow.started_at.desc(), AgentRunRow.id.desc())
            .limit(1)
        )

    async def complete(
        self,
        agent_run_id: str,
        *,
        status: AgentStatus,
        result_artifact_id: str | None = None,
        error: str | None = None,
    ) -> AgentRunRow:
        """Close out an agent run with its outcome.

        ``error`` is required when the status is ``FAILED`` in spirit even though the
        column permits null: a failed run with no explanation is indistinguishable from
        one that was never closed, and the distinction is what an operator needs when a
        report comes back thin.
        """
        row = await self.session.get(AgentRunRow, agent_run_id)
        if row is None:
            raise LookupError(f"no agent run {agent_run_id!r}")
        row.status = status.value
        row.completed_at = utcnow()
        if result_artifact_id is not None:
            row.result_artifact_id = result_artifact_id
        if error is not None:
            row.error = error
        if row.started_at is not None:
            row.latency_seconds = max(0.0, (row.completed_at - row.started_at).total_seconds())
        self.stamp(row)
        await self.session.flush()
        return row

    async def record_usage(
        self,
        agent_run_id: str,
        *,
        input_tokens: int,
        output_tokens: int,
        estimated_cost_usd: float,
    ) -> AgentRunRow:
        """Record token and cost accounting for a run.

        Separate from :meth:`complete` because the provider reports usage after the
        response has already been consumed, and a run should be closed as soon as its
        result exists rather than waiting on a counting step that may fail.
        """
        row = await self.session.get(AgentRunRow, agent_run_id)
        if row is None:
            raise LookupError(f"no agent run {agent_run_id!r}")
        row.input_tokens = input_tokens
        row.output_tokens = output_tokens
        row.estimated_cost_usd = estimated_cost_usd
        self.stamp(row)
        await self.session.flush()
        return row

    @staticmethod
    def to_domain(row: AgentRunRow) -> AgentRun:
        """Convert an agent-run row into its domain model."""
        return to_agent_run(row)
