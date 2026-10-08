"""Questions and answers: the human escalation surface (spec sections 27, 28, 71).

Questions are first-class entities, not log lines. This router exposes the operator's side of
them: list what is open, read why Scout is asking, and answer.

Two behaviours worth noting:

* Listing questions across jobs needs a query the per-job repository methods do not provide,
  so it is expressed directly against the table here. The right home for it is
  ``QuestionRepository.list_recent``; until that exists this is called out rather than hidden
  behind a misleading method name.
* Answering can promote the answer into durable engineering memory (spec section 29), which
  is how Scout stops asking the same question about the same system twice.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select

from scout_api.deps import UowDep
from scout_api.schemas import ErrorBody, Page, Question, QuestionAnswerRequest
from scout_domain.enums import ActorType, QuestionStatus
from scout_domain.ids import IdKind, new_id
from scout_domain.models.audit import AuditEvent
from scout_domain.models.base import utcnow
from scout_domain.models.question import QuestionAnswer
from scout_observability import get_logger
from scout_persistence import mapping
from scout_persistence.tables import QuestionRow

__all__ = ["router"]

_log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/questions", tags=["questions"])


@router.get("", response_model=Page[Question], summary="List engineering questions")
async def list_questions(
    uow: UowDep,
    question_status: Annotated[QuestionStatus | None, Query(alias="status")] = None,
    repository: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[Question]:
    """List questions, newest first, optionally filtered by status."""
    statement = select(QuestionRow)
    count_statement = select(func.count()).select_from(QuestionRow)

    if question_status is not None:
        statement = statement.where(QuestionRow.status == question_status.value)
        count_statement = count_statement.where(QuestionRow.status == question_status.value)
    if repository:
        # Questions denormalise the repository through their job, so filter on the job's
        # repository via the stored job link.
        from scout_persistence.tables import JobRow

        job_ids = select(JobRow.id).where(JobRow.repository_full_name == repository)
        statement = statement.where(QuestionRow.job_id.in_(job_ids))
        count_statement = count_statement.where(QuestionRow.job_id.in_(job_ids))

    total = int(await uow.session.scalar(count_statement) or 0)
    rows = list(
        await uow.session.scalars(
            statement.order_by(QuestionRow.created_at.desc(), QuestionRow.id.desc())
            .limit(limit)
            .offset(offset)
        )
    )

    items: list[Question] = []
    for row in rows:
        answers = await uow.questions.list_answers(row.id)
        items.append(mapping.to_question(row, answers))
    return Page[Question](items=items, total=total, limit=limit, offset=offset)


@router.get(
    "/{question_id}",
    response_model=Question,
    responses={404: {"model": ErrorBody}},
    summary="Question detail",
)
async def get_question(question_id: str, uow: UowDep) -> Question:
    """Return one question with its answer thread."""
    row = await uow.questions.get_row(question_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "question_not_found", "message": f"no question with id {question_id}"},
        )
    answers = await uow.questions.list_answers(question_id)
    return mapping.to_question(row, answers)


@router.post(
    "/{question_id}/answer",
    response_model=Question,
    responses={404: {"model": ErrorBody}, 409: {"model": ErrorBody}},
    summary="Answer a question",
)
async def answer_question(question_id: str, body: QuestionAnswerRequest, uow: UowDep) -> Question:
    """Record a human answer, optionally promoting it to engineering memory.

    Answering is a privileged action because it changes what Scout will believe about the
    system in every future job, so it is audited.
    """
    row = await uow.questions.get_row(question_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "question_not_found", "message": f"no question with id {question_id}"},
        )

    current = mapping.coerce(row.status, QuestionStatus)
    if current is not QuestionStatus.OPEN:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "question_not_open",
                "message": f"this question is {current.value} and cannot be answered again",
            },
        )

    answer = QuestionAnswer(
        id=new_id(IdKind.ANSWER),
        question_id=question_id,
        body=body.body,
        answered_by=body.answered_by,
        answered_by_type=ActorType.HUMAN,
        promote_to_knowledge=body.promote_to_knowledge,
        knowledge_scope=body.knowledge_scope,
    )
    await uow.questions.record_answer(answer)
    row = await uow.questions.close(question_id, QuestionStatus.ANSWERED)

    await uow.audit_events.append(
        AuditEvent(
            id=new_id(IdKind.AUDIT_EVENT),
            actor=body.answered_by,
            actor_type=ActorType.HUMAN,
            action="question.answer",
            resource="question",
            resource_id=question_id,
            job_id=row.job_id,
            metadata={
                "promote_to_knowledge": body.promote_to_knowledge,
                "knowledge_scope": body.knowledge_scope,
                "answer_id": answer.id,
            },
        )
    )

    # Promotion to knowledge is best-effort by design: a failure to index the answer must not
    # lose the answer itself, which is already recorded above. The failure is logged and
    # audited so it stays visible (Rule 12).
    if body.promote_to_knowledge:
        try:
            from scout_knowledge.decisions import DecisionRegistry

            registry = DecisionRegistry()
            document = await registry.record_from_answer(
                answer=answer,
                title=row.title,
                scope=body.knowledge_scope or "*",
            )
            if document is not None:
                await uow.knowledge.upsert(document)
                _log.info(
                    "promoted a human answer to engineering memory",
                    question_id=question_id,
                    document_id=document.id,
                )
        except Exception as exc:
            _log.error(
                "failed to promote a human answer to engineering memory",
                question_id=question_id,
                error=str(exc),
                exc_info=True,
            )
            await uow.audit_events.append(
                AuditEvent(
                    id=new_id(IdKind.AUDIT_EVENT),
                    actor="system",
                    actor_type=ActorType.SYSTEM,
                    action="knowledge.promote_failed",
                    resource="question",
                    resource_id=question_id,
                    job_id=row.job_id,
                    metadata={"error": f"{type(exc).__name__}: {exc}"},
                )
            )

    answers = await uow.questions.list_answers(question_id)
    return mapping.to_question(row, answers)


@router.post(
    "/{question_id}/dismiss",
    response_model=Question,
    responses={404: {"model": ErrorBody}},
    summary="Dismiss a question",
)
async def dismiss_question(question_id: str, uow: UowDep) -> Question:
    """Close a question without an answer.

    Dismissal is explicit and distinct from answering so that the record shows Scout asked
    and a human decided the question did not need an answer. It is never inferred from
    silence.
    """
    row = await uow.questions.get_row(question_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "question_not_found", "message": f"no question with id {question_id}"},
        )

    row = await uow.questions.close(question_id, QuestionStatus.DISMISSED)
    await uow.audit_events.append(
        AuditEvent(
            id=new_id(IdKind.AUDIT_EVENT),
            actor="operator",
            actor_type=ActorType.HUMAN,
            action="question.dismiss",
            resource="question",
            resource_id=question_id,
            job_id=row.job_id,
            metadata={"dismissed_at": utcnow().isoformat()},
        )
    )
    answers = await uow.questions.list_answers(question_id)
    return mapping.to_question(row, answers)
