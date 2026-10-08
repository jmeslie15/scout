"""Finding persistence and severity-ordered retrieval.

Findings are the raw material of the report, so the read paths are shaped by how the
report consumes them: worst-first within a job, and grouped by category for the
code-review, security, and static-analysis sections.

Ordering by severity is done with an explicit ``CASE`` expression rather than by
sorting on the string column. Alphabetical order would put ``CRITICAL`` above
``HIGH``—which happens to be right—but it would also put ``INFO`` above ``LOW`` and
``MEDIUM``, and it would silently change meaning the day a new severity is added. The
case expression is generated from :data:`scout_domain.enums.SEVERITY_ORDER`, so the
database and the domain agree by construction.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scout_domain.enums import SEVERITY_ORDER, FindingCategory, Severity
from scout_domain.models.finding import Finding
from scout_persistence.mapping import from_finding, to_finding
from scout_persistence.repositories.base import BaseRepository
from scout_persistence.tables import FindingRow

__all__ = ["FindingRepository", "severity_rank_expression"]


def severity_rank_expression(column: Any = None) -> Case[int]:
    """Return a ``CASE`` expression mapping a severity column to its numeric rank.

    ``column`` defaults to ``findings.severity``, but it is a parameter because two tables
    carry a severity column and both need the same ordering. An earlier version hard-coded
    the findings column, which made every question query fail with ``missing FROM-clause
    entry for table "findings"`` — a bug that only a real database surfaces, and the reason
    this takes the column explicitly.

    The ranking comes from :data:`scout_domain.enums.SEVERITY_ORDER` so the database and
    the domain agree on which item is "worst" without a second definition of that order.
    """
    target = FindingRow.severity if column is None else column
    return Case(
        *[(target == severity.value, rank) for severity, rank in SEVERITY_ORDER.items()],
        else_=0,
    )


class FindingRepository(BaseRepository):
    """Reads and writes ``findings`` rows."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to ``session``."""
        super().__init__(session)

    async def create(self, finding: Finding) -> FindingRow:
        """Insert ``finding`` and return its row."""
        row = from_finding(finding)
        self.session.add(row)
        await self.session.flush()
        return row

    async def create_many(self, findings: list[Finding]) -> list[FindingRow]:
        """Insert several findings in one flush.

        An agent that returns twenty findings should cost one round trip, not twenty,
        and a single flush means they become visible to the rest of the unit of work
        atomically.
        """
        rows = [from_finding(finding) for finding in findings]
        self.session.add_all(rows)
        await self.session.flush()
        return rows

    async def get(self, finding_id: str) -> FindingRow | None:
        """Return the finding row with ``finding_id``, or ``None``."""
        return await self.session.get(FindingRow, finding_id)

    async def list_for_job(
        self,
        job_id: str,
        *,
        severity: Severity | None = None,
        category: FindingCategory | None = None,
        limit: int | None = None,
    ) -> list[FindingRow]:
        """Return findings for ``job_id``, worst first, optionally filtered.

        The tiebreak on ``created_at`` keeps the order stable for findings of equal
        severity, which matters because the report renders them in the order it reads
        them and a report that reshuffles between renders is hard to review.
        """
        statement = select(FindingRow).where(FindingRow.job_id == job_id)
        if severity is not None:
            statement = statement.where(FindingRow.severity == severity.value)
        if category is not None:
            statement = statement.where(FindingRow.category == category.value)
        statement = statement.order_by(
            severity_rank_expression().desc(), FindingRow.created_at.asc(), FindingRow.id.asc()
        )
        if limit is not None:
            statement = statement.limit(limit)
        result = await self.session.scalars(statement)
        return list(result)

    async def list_blocking(self, job_id: str) -> list[FindingRow]:
        """Return CRITICAL and HIGH findings, which block a ``PASS`` recommendation."""
        result = await self.session.scalars(
            select(FindingRow)
            .where(
                FindingRow.job_id == job_id,
                FindingRow.severity.in_([Severity.CRITICAL.value, Severity.HIGH.value]),
            )
            .order_by(severity_rank_expression().desc(), FindingRow.created_at.asc())
        )
        return list(result)

    async def count_by_severity(self, job_id: str) -> dict[Severity, int]:
        """Return per-severity counts for ``job_id``, with zeros included.

        Zero-filled so the report can state "no CRITICAL findings" as a fact rather than
        by omission — an absent key and a zero count mean different things to a reader.
        """
        rows = (
            await self.session.execute(
                select(FindingRow.severity, func.count())
                .where(FindingRow.job_id == job_id)
                .group_by(FindingRow.severity)
            )
        ).all()
        counts = dict.fromkeys(Severity, 0)
        for severity_value, count in rows:
            counts[Severity(severity_value)] = int(count)
        return counts

    @staticmethod
    def to_domain(row: FindingRow) -> Finding:
        """Convert a finding row into its domain model."""
        return to_finding(row)
