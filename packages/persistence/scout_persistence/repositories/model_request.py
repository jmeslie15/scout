"""Model-request accounting (spec section 38).

One row per provider call: model, prompt version, tokens, latency, status, estimated
cost. Prompts themselves are never stored — only ``prompt_digest`` — because Scout's
prompts contain repository text and this table is not a secret store (spec section 55).

``created_at`` is part of the primary access index with ``job_id`` because the two
questions this table answers are "what did this job cost?" and "what is the spend trend
over time?", and both are range scans over ``(job_id, created_at)``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scout_domain.models.model_request import ModelRequestRecord
from scout_persistence.mapping import from_model_request, to_model_request
from scout_persistence.repositories.base import BaseRepository
from scout_persistence.tables import ModelRequestRow

__all__ = ["ModelRequestRepository"]


class ModelRequestRepository(BaseRepository):
    """Reads and writes ``model_requests`` rows."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to ``session``."""
        super().__init__(session)

    async def record(self, request: ModelRequestRecord) -> ModelRequestRow:
        """Insert a model-request record and return its row."""
        row = from_model_request(request)
        self.session.add(row)
        await self.session.flush()
        return row

    async def get(self, request_id: str) -> ModelRequestRow | None:
        """Return the row with ``request_id``, or ``None``."""
        return await self.session.get(ModelRequestRow, request_id)

    async def list_for_job(self, job_id: str) -> list[ModelRequestRow]:
        """Return every model request for ``job_id``, oldest first."""
        result = await self.session.scalars(
            select(ModelRequestRow)
            .where(ModelRequestRow.job_id == job_id)
            .order_by(ModelRequestRow.created_at.asc(), ModelRequestRow.id.asc())
        )
        return list(result)

    async def list_for_agent_run(self, agent_run_id: str) -> list[ModelRequestRow]:
        """Return every model request made by one agent run, in attempt order."""
        result = await self.session.scalars(
            select(ModelRequestRow)
            .where(ModelRequestRow.agent_run_id == agent_run_id)
            .order_by(ModelRequestRow.attempt.asc(), ModelRequestRow.created_at.asc())
        )
        return list(result)

    async def job_cost_usd(self, job_id: str) -> float:
        """Return the summed estimated cost of a job's model calls.

        Estimated, not billed: providers report tokens, and the price table that turns
        them into dollars is configuration. Summing in SQL keeps the figure consistent
        with whatever rows exist rather than with whatever the caller happened to load.
        """
        total = await self.session.scalar(
            select(func.coalesce(func.sum(ModelRequestRow.estimated_cost_usd), 0.0)).where(
                ModelRequestRow.job_id == job_id
            )
        )
        return float(total or 0.0)

    async def totals_for_job(self, job_id: str) -> dict[str, float]:
        """Return input, output, and total tokens plus cost for one job."""
        row = (
            await self.session.execute(
                select(
                    func.coalesce(func.sum(ModelRequestRow.input_tokens), 0),
                    func.coalesce(func.sum(ModelRequestRow.output_tokens), 0),
                    func.coalesce(func.sum(ModelRequestRow.estimated_cost_usd), 0.0),
                ).where(ModelRequestRow.job_id == job_id)
            )
        ).one()
        input_tokens, output_tokens, cost = row
        return {
            "input_tokens": float(input_tokens),
            "output_tokens": float(output_tokens),
            "total_tokens": float(input_tokens) + float(output_tokens),
            "estimated_cost_usd": float(cost),
        }

    async def count_since(self, since: datetime) -> int:
        """Return how many model calls happened at or after ``since``."""
        total = await self.session.scalar(
            select(func.count())
            .select_from(ModelRequestRow)
            .where(ModelRequestRow.created_at >= since)
        )
        return int(total or 0)

    @staticmethod
    def to_domain(row: ModelRequestRow) -> ModelRequestRecord:
        """Convert a model-request row into its domain model."""
        return to_model_request(row)
