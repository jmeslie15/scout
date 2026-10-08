"""Question and answer persistence (spec sections 27 and 28).

Answers live in their own table, so "answer a question" is two writes that must land
together: the ``question_answers`` row and the question's move out of ``OPEN``. Both
happen inside the caller's unit of work, which is why :meth:`QuestionRepository.answer`
does not commit — a half-applied answer (recorded, but the question still open) would
either re-prompt a human who already answered or hide the answer from the report.

:meth:`QuestionRepository.get` is the read path that reconstructs the full aggregate,
answers included, because that is the shape the report needs. The row-plus-answers
join is explicit here rather than a lazy relationship on the row: a lazy load in async
SQLAlchemy raises rather than silently issuing I/O, and making the second query visible
means a caller that does not need answers can use :meth:`get_row` and skip it.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from scout_domain.enums import ActorType, QuestionStatus, Severity
from scout_domain.ids import IdKind, new_id
from scout_domain.models.base import utcnow
from scout_domain.models.question import Question, QuestionAnswer
from scout_persistence.mapping import (
    from_question,
    from_question_answer,
    to_question,
    to_question_answer,
)
from scout_persistence.repositories.base import BaseRepository
from scout_persistence.repositories.finding import severity_rank_expression
from scout_persistence.tables import QuestionAnswerRow, QuestionRow

__all__ = ["QuestionRepository"]


class QuestionRepository(BaseRepository):
    """Reads and writes ``questions`` and ``question_answers`` rows."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to ``session``."""
        super().__init__(session)

    # -- questions --------------------------------------------------------------

    async def create(self, question: Question) -> QuestionRow:
        """Insert ``question`` and return its row.

        Only the question is written: any answers on the model are expected to be
        inserted through :meth:`record_answer`, because a question created with answers
        is a state the domain forbids (``OPEN`` with answers, or ``ANSWERED`` before the
        answers exist as rows).
        """
        row = from_question(question)
        self.session.add(row)
        await self.session.flush()
        return row

    async def get_row(self, question_id: str) -> QuestionRow | None:
        """Return the question row without loading its answers."""
        return await self.session.get(QuestionRow, question_id)

    async def get(self, question_id: str) -> Question | None:
        """Return the full question aggregate, answers included, or ``None``."""
        row = await self.session.get(QuestionRow, question_id)
        if row is None:
            return None
        answers = await self.list_answers(question_id)
        return to_question(row, answers)

    async def list_open_for_job(self, job_id: str) -> list[QuestionRow]:
        """Return the open questions for ``job_id``, most severe first.

        Severity is ordered through the domain's ranking rather than a stored mirror, so
        this can never disagree with :data:`scout_domain.enums.SEVERITY_ORDER`.
        """
        severity_rank = severity_rank_expression(QuestionRow.severity)
        result = await self.session.scalars(
            select(QuestionRow)
            .where(
                QuestionRow.job_id == job_id,
                QuestionRow.status == QuestionStatus.OPEN.value,
            )
            .order_by(
                severity_rank.desc(),
                QuestionRow.created_at.asc(),
                QuestionRow.id.asc(),
            )
        )
        return list(result)

    async def list_for_job(
        self,
        job_id: str,
        *,
        status: QuestionStatus | None = None,
    ) -> list[QuestionRow]:
        """Return questions for ``job_id``, newest first, optionally filtered by status."""
        statement = select(QuestionRow).where(QuestionRow.job_id == job_id)
        if status is not None:
            statement = statement.where(QuestionRow.status == status.value)
        statement = statement.order_by(QuestionRow.created_at.desc(), QuestionRow.id.desc())
        result = await self.session.scalars(statement)
        return list(result)

    async def count_open_for_job(self, job_id: str) -> int:
        """Return how many questions for ``job_id`` are still open.

        The report's recommendation gates depend on this number, so it is counted in SQL
        rather than by loading the questions and filtering in Python.
        """
        total = await self.session.scalar(
            select(func.count())
            .select_from(QuestionRow)
            .where(
                QuestionRow.job_id == job_id,
                QuestionRow.status == QuestionStatus.OPEN.value,
            )
        )
        return int(total or 0)

    # -- answers ----------------------------------------------------------------

    async def answer(
        self,
        question_id: str,
        body: str,
        *,
        answered_by: str,
        answered_by_type: ActorType = ActorType.HUMAN,
        promote_to_knowledge: bool = False,
        knowledge_scope: str | None = None,
        answered_at: datetime | None = None,
    ) -> tuple[QuestionAnswer, Question]:
        """Record an answer and close the question.

        Returns ``(answer, question)``. The question's ``accepted_answer_id`` is filled
        from this answer so that :meth:`Question.accepted_answer` has something to
        return even when a caller never set one explicitly.
        """
        row = await self.session.get(QuestionRow, question_id)
        if row is None:
            raise LookupError(f"no question {question_id!r}")

        answer = QuestionAnswer(
            id=new_id(IdKind.ANSWER),
            question_id=question_id,
            body=body,
            answered_by=answered_by,
            answered_by_type=answered_by_type,
            answered_at=answered_at or utcnow(),
            promote_to_knowledge=promote_to_knowledge,
            knowledge_scope=knowledge_scope,
        )
        await self.record_answer(answer)
        row.status = QuestionStatus.ANSWERED.value
        row.accepted_answer_id = row.accepted_answer_id or answer.id
        self.stamp(row)
        await self.session.flush()

        answers = await self.list_answers(question_id)
        return answer, to_question(row, answers)

    async def record_answer(self, answer: QuestionAnswer) -> QuestionAnswerRow:
        """Insert a pre-built answer row.

        Separate from :meth:`answer` so a caller replaying persisted answers (a
        promotion worker, a backfill) does not have to re-close the question, which
        would overwrite a dismissal with ``ANSWERED``.
        """
        row = from_question_answer(answer)
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_answers(self, question_id: str) -> list[QuestionAnswerRow]:
        """Return answers for ``question_id``, oldest first."""
        result = await self.session.scalars(
            select(QuestionAnswerRow)
            .where(QuestionAnswerRow.question_id == question_id)
            .order_by(QuestionAnswerRow.answered_at.asc(), QuestionAnswerRow.id.asc())
        )
        return list(result)

    async def close(self, question_id: str, status: QuestionStatus) -> QuestionRow:
        """Move a question into a closing status (``DISMISSED`` or ``SUPERSEDED``).

        ``ANSWERED`` is rejected here: it is only reachable by recording an answer, so
        that the status can never assert a decision that has no answer row behind it.
        """
        if status is QuestionStatus.ANSWERED:
            raise ValueError(
                "use QuestionRepository.answer to mark a question ANSWERED; "
                "closing it directly would claim a decision with no answer recorded"
            )
        if status is QuestionStatus.OPEN:
            raise ValueError("cannot close a question into OPEN")

        row = await self.session.get(QuestionRow, question_id)
        if row is None:
            raise LookupError(f"no question {question_id!r}")
        row.status = status.value
        if status is QuestionStatus.SUPERSEDED:
            row.accepted_answer_id = None
        self.stamp(row)
        await self.session.flush()
        return row

    async def mark_severity(self, question_id: str, severity: Severity) -> QuestionRow:
        """Change a question's severity, e.g. after new evidence raises its stakes."""
        row = await self.session.get(QuestionRow, question_id)
        if row is None:
            raise LookupError(f"no question {question_id!r}")
        row.severity = severity.value
        self.stamp(row)
        await self.session.flush()
        return row

    @staticmethod
    def to_domain(row: QuestionRow, answers: list[QuestionAnswerRow] | None = None) -> Question:
        """Convert a question row (plus its answer rows) into its domain model."""
        return to_question(row, answers or [])

    @staticmethod
    def answer_to_domain(row: QuestionAnswerRow) -> QuestionAnswer:
        """Convert an answer row into its domain model."""
        return to_question_answer(row)
