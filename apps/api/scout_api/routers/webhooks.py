"""GitHub webhook reception (spec sections 10 and 62).

This is the platform's public entry point and the one place an unauthenticated request can
create work, so the ordering below is deliberate:

1. **Read the raw body.** The signature is computed over the exact bytes GitHub sent.
   Re-serialising the parsed JSON first would break verification, and it is the single most
   common way this check is implemented incorrectly.
2. **Verify the HMAC** before parsing anything. An unsigned payload is never inspected.
3. **Ignore irrelevant deliveries cheaply**, before doing any work.
4. **Record the delivery id and refuse duplicates.** GitHub retries. A duplicate delivery
   must return the existing result, not create a second job (spec section 10: "Never rely on
   the webhook sender to prevent duplicates").
5. **Create the job, keyed on the exact head SHA**, with a uniqueness constraint as the
   backstop. Two concurrent deliveries of the same revision race, and the database decides.
6. **Enqueue**, then mark the delivery processed. A queue failure fails the job loudly
   rather than returning 202 for work that will never happen.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Request, status

from scout_api.deps import AppState, UowDep, app_state, settings_dependency
from scout_api.schemas import ErrorBody
from scout_api.settings import Settings
from scout_domain.enums import ActorType, JobStatus, RiskLevel
from scout_domain.ids import IdKind, new_id
from scout_domain.models.audit import AuditEvent
from scout_domain.models.base import utcnow
from scout_domain.models.job import CommitRange, VerificationJob
from scout_github.webhooks import (
    PullRequestEvent,
    parse_pull_request_event,
    should_process,
    verify_webhook_signature,
)
from scout_observability import get_logger, job_context
from scout_persistence.queue import JobMessage, JobQueue, RedisJobQueue

__all__ = ["router"]

_log = get_logger(__name__)

router = APIRouter(prefix="/api/v1/webhooks", tags=["webhooks"])

#: Header carrying GitHub's delivery UUID. This is the idempotency key.
DELIVERY_HEADER = "X-GitHub-Delivery"
EVENT_HEADER = "X-GitHub-Event"
SIGNATURE_HEADER = "X-Hub-Signature-256"


@router.post(
    "/github",
    status_code=status.HTTP_202_ACCEPTED,
    summary="Receive a GitHub webhook delivery",
    responses={401: {"model": ErrorBody}, 400: {"model": ErrorBody}},
)
async def github_webhook(
    request: Request,
    uow: UowDep,
    settings: Annotated[Settings, settings_dependency],
    x_github_event: Annotated[str | None, Header(alias=EVENT_HEADER)] = None,
    x_github_delivery: Annotated[str | None, Header(alias=DELIVERY_HEADER)] = None,
    x_hub_signature_256: Annotated[str | None, Header(alias=SIGNATURE_HEADER)] = None,
) -> dict[str, Any]:
    """Accept a ``pull_request`` delivery and create a verification job."""
    state = app_state(request)
    raw_body = await request.body()

    # -- 1/2. Signature verification over the raw bytes --------------------------------
    if settings.webhook_require_signature:
        if not settings.github_webhook_secret:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "code": "webhook_secret_missing",
                    "message": (
                        "signature verification is required but SCOUT_GITHUB_WEBHOOK_SECRET "
                        "is not configured"
                    ),
                },
            )
        try:
            verify_webhook_signature(
                payload=raw_body,
                signature_header=x_hub_signature_256,
                secret=settings.github_webhook_secret,
            )
        except Exception as exc:
            _log.warning(
                "rejected a webhook delivery with an invalid signature",
                delivery_id=x_github_delivery,
                event=x_github_event,
                error=str(exc),
            )
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail={
                    "code": "invalid_signature",
                    "message": "webhook signature did not verify",
                },
            ) from exc

    # -- 3. Parse and cheaply ignore anything irrelevant ------------------------------
    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_json", "message": f"body is not valid JSON: {exc}"},
        ) from exc

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_payload", "message": "body must be a JSON object"},
        )

    action = payload.get("action")
    action_value = action if isinstance(action, str) else None
    if not should_process(x_github_event, action_value):
        return {"status": "ignored", "reason": "event or action is not processed"}

    if not x_github_delivery:
        # Without a delivery id there is no idempotency key, so the delivery cannot be
        # deduplicated. Refusing is safer than creating a job that may be created again.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": "missing_delivery_id",
                "message": f"the {DELIVERY_HEADER} header is required for idempotency",
            },
        )

    try:
        event = parse_pull_request_event(
            payload,
            event_type=x_github_event or "",
            action=action_value or "",
        )
        event = event.model_copy(update={"delivery_id": x_github_delivery})
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"code": "invalid_pull_request_payload", "message": str(exc)},
        ) from exc

    with job_context(
        repository=event.repository.full_name,
        pull_request=event.pull_request_number,
        delivery_id=x_github_delivery,
    ):
        return await _intake(
            uow=uow,
            state=state,
            event=event,
            delivery_id=x_github_delivery,
            event_type=x_github_event or "",
            payload=payload,
        )


async def _intake(
    *,
    uow: Any,
    state: AppState,
    event: PullRequestEvent,
    delivery_id: str,
    event_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Record the delivery, create the job, and enqueue it.

    Split out from the handler so the ordering of the writes is visible in one place and so
    it can be exercised without an HTTP request.
    """
    # -- 4. Idempotency: the database decides, not a read-then-write check --------------
    _, created = await uow.webhook_events.record_if_new(
        delivery_id=delivery_id,
        event_type=event_type,
        payload=payload,
        action=event.action,
        repository_full_name=event.repository.full_name,
        pull_request_number=event.pull_request_number,
    )
    if not created:
        existing = await uow.jobs.get_by_delivery(delivery_id)
        _log.info(
            "duplicate webhook delivery ignored",
            delivery_id=delivery_id,
            existing_job_id=existing.id if existing else None,
        )
        return {
            "status": "duplicate",
            "delivery_id": delivery_id,
            "job_id": existing.id if existing else None,
        }

    await uow.repositories.upsert_by_full_name(
        event.repository.full_name,
        github_id=event.repository.github_id,
        default_branch=event.repository.default_branch,
    )

    job = VerificationJob(
        id=new_id(IdKind.JOB),
        repository=event.repository,
        repository_full_name=event.repository.full_name,
        pull_request_number=event.pull_request_number,
        pull_request_title=event.title,
        pull_request_author=event.author,
        pull_request_url=event.html_url or None,
        commits=CommitRange(base_sha=event.base_sha, head_sha=event.head_sha),
        github_delivery_id=delivery_id,
        status=JobStatus.RECEIVED,
        # Provisional. The PREPARING stage refines this from the real diff
        # (scout_domain.risk.classify_change_set). The webhook deliberately does not guess
        # from a title, and it has no diff yet.
        risk_level=RiskLevel.MEDIUM,
        correlation_id=delivery_id,
    )

    # -- 5. One job per (repository, pull request, head SHA) ---------------------------
    row, job_created = await uow.jobs.create_unless_exists(job)
    if not job_created:
        _log.info(
            "a job for this revision already exists",
            job_id=row.id,
            head_sha=job.head_sha,
            delivery_id=delivery_id,
        )
        receipt = await uow.webhook_events.get_by_delivery(delivery_id)
        if receipt is not None:
            await uow.webhook_events.mark_processed(receipt.id, job_id=row.id)
        return {"status": "already_verified", "delivery_id": delivery_id, "job_id": row.id}

    await uow.audit_events.append(
        AuditEvent(
            id=new_id(IdKind.AUDIT_EVENT),
            actor=f"github:{event.author or 'unknown'}",
            actor_type=ActorType.WEBHOOK,
            action="job.create",
            resource="verification_job",
            resource_id=job.id,
            job_id=job.id,
            correlation_id=delivery_id,
            metadata={
                "repository": job.repository_full_name,
                "pull_request": job.pull_request_number,
                "head_sha": job.head_sha,
                "delivery_id": delivery_id,
            },
        )
    )

    # -- 6. Enqueue -------------------------------------------------------------------
    message = JobMessage(
        job_id=job.id,
        repository_full_name=job.repository_full_name,
        head_sha=job.head_sha,
        attempt=1,
    )
    try:
        message_id = await _queue_for(state).enqueue(message)
    except Exception as exc:
        # The job row exists and would otherwise sit in RECEIVED forever. Record the failure
        # on the job and fail the request, rather than acknowledging work that cannot happen.
        _log.error(
            "failed to enqueue a newly created job",
            job_id=job.id,
            error=str(exc),
            exc_info=True,
        )
        await uow.jobs.update_status(
            job.id,
            JobStatus.FAILED,
            failure_reason=f"could not enqueue the job: {type(exc).__name__}: {exc}",
            completed_at=utcnow(),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "queue_unavailable", "message": "the job queue is unavailable"},
        ) from exc

    receipt = await uow.webhook_events.get_by_delivery(delivery_id)
    if receipt is not None:
        await uow.webhook_events.mark_processed(receipt.id, job_id=job.id)

    _log.info(
        "verification job created",
        job_id=job.id,
        message_id=message_id,
        repository=job.repository_full_name,
        pull_request=job.pull_request_number,
        head_sha=job.head_sha,
    )
    return {
        "status": "accepted",
        "delivery_id": delivery_id,
        "job_id": job.id,
        "message_id": message_id,
        "head_sha": job.head_sha,
    }


def _queue_for(state: AppState) -> JobQueue:
    """Return this process's job queue, creating one on first use.

    Lazy so that the API starts even if Redis is briefly unavailable, and so that a test can
    inject an in-memory queue through ``app.state`` without touching Redis at all.
    """
    if isinstance(state.queue, JobQueue):
        return state.queue
    queue = RedisJobQueue(state.settings.redis_url)
    state.queue = queue
    return queue
