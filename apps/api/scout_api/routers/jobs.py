"""Job queries and human authorization (spec sections 12, 46, 74).

Two responsibilities that belong together because they share the same resource:

* Reading a job — the list view and the detail view the dashboard renders.
* Acting on a job — recording a human decision, cancelling, or retrying.

The approval path is the security-relevant one. Three rules are enforced here rather than
trusted to the UI:

1. A decision must name a human (``decided_by``); the domain model rejects any other actor.
2. A decision is bound to the exact commit under review, so an approval cannot be silently
   carried over to a newer revision.
3. Recording a decision is a privileged action, so it is audited.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status

from scout_api.deps import UowDep, app_state
from scout_api.schemas import (
    ApprovalRequest,
    CancelRequest,
    ErrorBody,
    JobDetail,
    JobSummary,
    Page,
)
from scout_domain.enums import (
    ActorType,
    ApprovalDecision,
    JobStatus,
    Recommendation,
    RiskLevel,
    Severity,
    VerificationStatus,
)
from scout_domain.errors import InvalidJobTransition
from scout_domain.ids import IdKind, new_id
from scout_domain.models.approval import Approval
from scout_domain.models.artifact import ArtifactKind
from scout_domain.models.audit import AuditEvent
from scout_domain.models.base import utcnow
from scout_domain.models.report import VerificationReport
from scout_domain.state import TransitionContext, transition
from scout_observability import get_logger
from scout_persistence import mapping
from scout_persistence.queue import JobMessage, JobQueue, RedisJobQueue

__all__ = ["router"]

_log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/jobs", tags=["jobs"])

#: Statuses the dashboard polls while a job is still moving.
_IN_FLIGHT = {
    JobStatus.RECEIVED,
    JobStatus.QUEUED,
    JobStatus.PREPARING,
    JobStatus.ANALYZING,
    JobStatus.REVIEWING,
    JobStatus.SECURITY_SCANNING,
    JobStatus.TESTING,
    JobStatus.FEATURE_VERIFYING,
    JobStatus.INVESTIGATING,
}


@router.get("", response_model=Page[JobSummary], summary="List verification jobs")
async def list_jobs(
    uow: UowDep,
    status_filter: Annotated[JobStatus | None, Query(alias="status")] = None,
    repository: Annotated[str | None, Query(max_length=200)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> Page[JobSummary]:
    """List jobs, newest first.

    Limitation worth stating: filtering is applied in Python after fetching a bounded
    window, because ``JobRepository`` exposes ``list_recent``/``list_active`` rather than a
    general filtered query. At Scout's volumes (tens of jobs a day) that is fine, and the
    ``total`` is the count of the filtered window rather than of the whole table. A proper
    filtered query is a follow-up, not a redesign.
    """
    window = max(limit * 4, limit) if (status_filter or repository) else limit
    rows = await uow.jobs.list_recent(
        limit=window, offset=0 if (status_filter or repository) else offset
    )

    if status_filter is not None:
        rows = [row for row in rows if row.status == status_filter.value]
    if repository:
        rows = [row for row in rows if row.repository_full_name == repository]

    total = len(rows)
    items = [await _summarise(uow, row) for row in rows[offset : offset + limit]]
    return Page[JobSummary](items=items, total=total, limit=limit, offset=offset)


@router.get(
    "/{job_id}",
    response_model=JobDetail,
    responses={404: {"model": ErrorBody}},
    summary="Job detail",
)
async def get_job(job_id: str, uow: UowDep) -> JobDetail:
    """Return everything the job page renders, in one response."""
    row = await uow.jobs.get(job_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "job_not_found", "message": f"no job with id {job_id}"},
        )
    return await _detail(uow, row)


@router.get(
    "/{job_id}/report",
    response_model=VerificationReport,
    responses={404: {"model": ErrorBody}},
    summary="Verification report",
)
async def get_report(job_id: str, request: Request, uow: UowDep) -> VerificationReport:
    """Return the final verification report, or 404 if the job has not produced one."""
    row = await uow.jobs.get(job_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "job_not_found", "message": f"no job with id {job_id}"},
        )
    report = await _load_report(uow, app_state(request), job_id)
    if report is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": "report_not_available",
                "message": "this job has not produced a verification report yet",
                "job_status": row.status,
            },
        )
    return report


@router.post(
    "/{job_id}/approve",
    response_model=JobDetail,
    responses={404: {"model": ErrorBody}, 409: {"model": ErrorBody}},
    summary="Record a human authorization decision",
)
async def approve_job(job_id: str, body: ApprovalRequest, uow: UowDep) -> JobDetail:
    """Record a human decision against the commit currently under review.

    Spec section 74 is the reason the commit SHA is read from the job rather than accepted
    from the client: an approval authorises *a revision*, and letting a caller name the
    revision would allow approving a commit nobody reviewed.
    """
    row = await uow.jobs.get(job_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "job_not_found", "message": f"no job with id {job_id}"},
        )

    if row.status == JobStatus.CANCELLED.value:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "job_cancelled",
                "message": "a cancelled job cannot be approved; open a new verification",
            },
        )

    approval = Approval(
        id=new_id(IdKind.APPROVAL),
        job_id=job_id,
        commit_sha=row.head_sha,
        decision=ApprovalDecision(body.decision),
        decided_by=body.decided_by,
        comment=body.comment,
    )

    # A newer decision supersedes any earlier valid one for this commit, so the "latest
    # valid approval" query has exactly one answer.
    await uow.approvals.invalidate_for_job(
        job_id, reason=f"superseded by a new decision from {body.decided_by}"
    )
    await uow.approvals.record(approval)

    await uow.audit_events.append(
        AuditEvent(
            id=new_id(IdKind.AUDIT_EVENT),
            actor=body.decided_by,
            actor_type=ActorType.HUMAN,
            action="approval.record",
            resource="verification_job",
            resource_id=job_id,
            job_id=job_id,
            correlation_id=row.correlation_id,
            metadata={
                "decision": body.decision,
                "commit_sha": row.head_sha,
                "comment_length": len(body.comment),
            },
        )
    )

    # Closing the verification lifecycle requires a recorded decision; the state machine
    # enforces that, so the status move is attempted and a refusal is surfaced rather than
    # forced. A job still mid-pipeline legitimately refuses and keeps the approval on record.
    current = mapping.coerce(row.status, JobStatus)
    if current in _IN_FLIGHT or current is JobStatus.WAITING_FOR_HUMAN:
        _log.info(
            "approval recorded against a job that is not yet ready for review",
            job_id=job_id,
            job_status=current.value,
            decision=body.decision,
        )
    elif current is JobStatus.READY_FOR_REVIEW:
        row = await uow.jobs.update_status(
            job_id,
            JobStatus.COMPLETED,
            completed_at=utcnow(),
        )

    return await _detail(uow, row)


@router.post(
    "/{job_id}/cancel",
    response_model=JobDetail,
    responses={404: {"model": ErrorBody}, 409: {"model": ErrorBody}},
    summary="Cancel a job",
)
async def cancel_job(job_id: str, body: CancelRequest, uow: UowDep) -> JobDetail:
    """Cancel a job. Terminal jobs cannot be cancelled again."""
    row = await uow.jobs.get(job_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "job_not_found", "message": f"no job with id {job_id}"},
        )

    current = mapping.coerce(row.status, JobStatus)
    if current.is_terminal:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "job_already_terminal",
                "message": f"a job in {current.value} cannot be cancelled",
            },
        )

    row = await uow.jobs.update_status(
        job_id, JobStatus.CANCELLED, cancel_reason=body.reason, completed_at=utcnow()
    )
    await uow.audit_events.append(
        AuditEvent(
            id=new_id(IdKind.AUDIT_EVENT),
            actor="operator",
            actor_type=ActorType.HUMAN,
            action="job.cancel",
            resource="verification_job",
            resource_id=job_id,
            job_id=job_id,
            correlation_id=row.correlation_id,
            metadata={"reason": body.reason, "previous_status": current.value},
        )
    )
    return await _detail(uow, row)


@router.post(
    "/{job_id}/retry",
    response_model=JobDetail,
    responses={404: {"model": ErrorBody}, 409: {"model": ErrorBody}},
    summary="Re-queue a failed job",
)
async def retry_job(job_id: str, body: CancelRequest, request: Request, uow: UowDep) -> JobDetail:
    """Re-queue a failed job, resuming at its first unresolved stage.

    The retry budget is enforced by the domain model (``retries_remaining``) and by the
    state machine's guard on the ``FAILED -> QUEUED`` edge, so this handler does not
    re-implement either check — it attempts the transition and lets the domain refuse.
    """
    state = app_state(request)
    row = await uow.jobs.get(job_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"code": "job_not_found", "message": f"no job with id {job_id}"},
        )

    current = mapping.coerce(row.status, JobStatus)
    next_attempt = row.attempt + 1
    try:
        transition(
            current,
            JobStatus.QUEUED,
            TransitionContext(
                retries_remaining=max(0, row.max_attempts - next_attempt + 1),
                reason=body.reason,
            ),
        )
    except InvalidJobTransition as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"code": "retry_not_permitted", "message": str(exc)},
        ) from exc

    row = await uow.jobs.update_status(
        job_id,
        JobStatus.QUEUED,
        attempt=next_attempt,
        failure_reason=None,
        completed_at=None,
    )
    await uow.audit_events.append(
        AuditEvent(
            id=new_id(IdKind.AUDIT_EVENT),
            actor="operator",
            actor_type=ActorType.HUMAN,
            action="job.retry",
            resource="verification_job",
            resource_id=job_id,
            job_id=job_id,
            correlation_id=row.correlation_id,
            metadata={"reason": body.reason, "attempt": next_attempt},
        )
    )

    queue = state.queue
    if not isinstance(queue, JobQueue):
        queue = RedisJobQueue(state.settings.redis_url)
        state.queue = queue
    await queue.enqueue(
        JobMessage(
            job_id=job_id,
            repository_full_name=row.repository_full_name,
            head_sha=row.head_sha,
            attempt=next_attempt,
        )
    )
    return await _detail(uow, row)


# -- Assembly helpers ----------------------------------------------------------------


async def _summarise(uow: Any, row: Any) -> JobSummary:
    """Build a list row, including the denormalised counts the table renders."""
    counts = await uow.findings.count_by_severity(row.id)
    open_questions = await uow.questions.count_open_for_job(row.id)
    return JobSummary(
        id=row.id,
        repository_full_name=row.repository_full_name,
        pull_request_number=row.pull_request_number,
        pull_request_title=row.pull_request_title or "",
        pull_request_url=row.pull_request_url,
        head_sha=row.head_sha,
        status=mapping.coerce(row.status, JobStatus),
        risk_level=mapping.coerce(row.risk_level, RiskLevel),
        recommendation=(
            mapping.coerce(row.recommendation, Recommendation) if row.recommendation else None
        ),
        verification_status=mapping.coerce(row.verification_status, VerificationStatus),
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        finding_counts={severity: counts.get(severity, 0) for severity in Severity},
        open_question_count=open_questions,
    )


async def _detail(uow: Any, row: Any) -> JobDetail:
    """Assemble the full job view from its parts."""
    job = mapping.to_verification_job(row)

    stages = [mapping.to_job_stage(item) for item in await uow.stages.list_for_job(row.id)]
    findings = [mapping.to_finding(item) for item in await uow.findings.list_for_job(row.id)]

    question_rows = await uow.questions.list_for_job(row.id)
    questions = []
    for question_row in question_rows:
        answers = await uow.questions.list_answers(question_row.id)
        questions.append(mapping.to_question(question_row, answers))

    tool_runs = [mapping.to_tool_result(item) for item in await uow.tool_runs.list_for_job(row.id)]
    agent_runs = [mapping.to_agent_run(item) for item in await uow.agent_runs.list_for_job(row.id)]
    approvals = [mapping.to_approval(item) for item in await uow.approvals.list_for_job(row.id)]

    valid_for_commit = await uow.approvals.count_valid_for_commit(row.id, row.head_sha)

    counts = await uow.findings.count_by_severity(row.id)

    return JobDetail(
        id=row.id,
        repository_full_name=row.repository_full_name,
        pull_request_number=row.pull_request_number,
        pull_request_title=row.pull_request_title or "",
        pull_request_url=row.pull_request_url,
        head_sha=row.head_sha,
        status=job.status,
        risk_level=job.risk_level,
        recommendation=job.recommendation,
        verification_status=job.verification_status,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        finding_counts={severity: counts.get(severity, 0) for severity in Severity},
        open_question_count=sum(1 for question in questions if question.is_open),
        base_sha=row.base_sha,
        pull_request_author=row.pull_request_author or "",
        commits=job.commits,
        attempt=row.attempt,
        max_attempts=row.max_attempts,
        failure_reason=row.failure_reason,
        cancel_reason=row.cancel_reason,
        stages=stages,
        findings=findings,
        questions=questions,
        tool_runs=tool_runs,
        agent_runs=agent_runs,
        approvals=approvals,
        # The dashboard renders a prominent warning when an approval exists but is no longer
        # valid for the current commit; that is the whole point of spec section 74.
        current_approval_valid=valid_for_commit > 0,
    )


async def _load_report(uow: Any, state: Any, job_id: str) -> VerificationReport | None:
    """Load the verification report stored as an artifact, if one exists."""
    rows = await uow.artifacts.list_for_job(job_id, kind=ArtifactKind.VERIFICATION_REPORT)
    if not rows:
        return None
    artifact = mapping.to_artifact(rows[0])
    try:
        payload = state.artifacts.get_bytes(artifact)
        return VerificationReport.model_validate(json.loads(payload))
    except Exception as exc:
        _log.error(
            "stored verification report could not be parsed",
            job_id=job_id,
            artifact_id=artifact.id,
            error=str(exc),
        )
        return None
