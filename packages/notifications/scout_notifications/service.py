"""Notification orchestration: routing, dedup, rendering, delivery (spec sections 28, 47, 72).

The service is the only place that knows the whole story, and it enforces three rules:

**Questions must be answerable.** For a question, the rendered body must contain the
question itself, why Scout is asking, the evidence behind it, a link to the job, and the
suggested options (spec sections 28 and 72). A notification that says "authorization
concerns" without those five things is noise, and there is a test that fails if any of them
disappears from the template context.

**Failures are recorded, never swallowed (Rule 12).** A channel that fails produces a
``FAILED`` :class:`~scout_domain.models.notification.NotificationRecord` carrying the error,
which is returned to the caller. The remaining channels are still attempted: one webhook
being down must not silence the email that would have reached a human.

**The same fact is announced once.** Every record carries a stable ``dedup_key``; a repeat
inside the dedup window is recorded as ``SUPPRESSED`` with a reason rather than silently
dropped, so the audit trail shows the decision that was made.

Persistence of history lives in ``scout_persistence`` (owned by another workstream), so this
module depends on a :class:`NotificationHistory` protocol and ships an in-memory
implementation for tests and offline runs.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Final, Protocol, runtime_checkable

from scout_domain.config import NotificationsConfig, ScoutRepositoryConfig
from scout_domain.enums import NotificationChannel, Recommendation, Severity
from scout_domain.ids import IdKind, new_id
from scout_domain.models.base import utcnow
from scout_domain.models.finding import Finding
from scout_domain.models.job import VerificationJob
from scout_domain.models.notification import NotificationRecord, NotificationStatus
from scout_domain.models.question import Question
from scout_domain.models.report import VerificationReport
from scout_notifications.base import (
    NotificationChannelAdapter,
    NotificationMessage,
)
from scout_notifications.observability import get_logger
from scout_notifications.routing import (
    SUPPRESSED_DUPLICATE,
    NotificationEventKind,
    RoutingPolicy,
    dedup_key_for,
    email_recipients_for,
)
from scout_notifications.templating import TemplateRenderer

__all__ = [
    "DEFAULT_DEDUP_WINDOW_SECONDS",
    "InMemoryNotificationHistory",
    "NotificationHistory",
    "NotificationService",
    "default_job_url",
    "severity_for_report",
]

logger = get_logger(__name__)

#: How long a previously sent notification suppresses a repeat of the same fact. Six hours
#: spans a working session: long enough that a re-run does not re-notify, short enough that
#: an unresolved question resurfaces the next day.
DEFAULT_DEDUP_WINDOW_SECONDS: Final[int] = 6 * 60 * 60

#: Longest subject the domain allows.
_MAX_SUBJECT_LENGTH: Final[int] = 300

#: Deterministic channel ordering, so records and side effects happen in the same order on
#: every run. GitHub first (cheapest, most contextual), email last (loudest).
_CHANNEL_SORT_ORDER: Final[Mapping[NotificationChannel, int]] = {
    NotificationChannel.GITHUB: 0,
    NotificationChannel.GOOGLE_CHAT: 1,
    NotificationChannel.EMAIL: 2,
}

_TEXT_TEMPLATE_SUFFIX: Final[str] = ".md.j2"
_HTML_TEMPLATE_SUFFIX: Final[str] = ".html.j2"


@runtime_checkable
class NotificationHistory(Protocol):
    """Where sent notifications are remembered for deduplication and audit."""

    def was_recently_sent(
        self,
        dedup_key: str,
        channel: NotificationChannel,
        within_seconds: int,
    ) -> bool:
        """Whether a ``SENT`` record with this key and channel exists inside the window."""
        ...

    def record(self, record: NotificationRecord) -> None:
        """Persist a terminal record (``SENT`` or ``FAILED``)."""
        ...


class InMemoryNotificationHistory:
    """List-backed history, for tests and for the offline default configuration."""

    def __init__(self, *, clock: Callable[[], datetime] = utcnow) -> None:
        self._clock = clock
        self._records: list[NotificationRecord] = []

    @property
    def records(self) -> list[NotificationRecord]:
        """A snapshot of every recorded notification."""
        return list(self._records)

    def record(self, record: NotificationRecord) -> None:
        """Remember ``record``."""
        self._records.append(record)

    def was_recently_sent(
        self,
        dedup_key: str,
        channel: NotificationChannel,
        within_seconds: int,
    ) -> bool:
        """Whether a matching ``SENT`` record falls inside the window."""
        cutoff = self._clock() - timedelta(seconds=within_seconds)
        return any(
            record.dedup_key == dedup_key
            and record.channel is channel
            and record.status is NotificationStatus.SENT
            and record.sent_at is not None
            and record.sent_at >= cutoff
            for record in self._records
        )


def default_job_url(job: VerificationJob) -> str:
    """Build the dashboard link for ``job``.

    The dashboard URL is preferred because it shows the report and the questions together;
    the pull-request URL is a reasonable fallback, and a non-HTTP locator is the last
    resort so the field is never empty (the templates require it).
    """
    base = (
        os.environ.get("SCOUT_DASHBOARD_URL", "").strip()
        or os.environ.get("SCOUT_PUBLIC_URL", "").strip()
    )
    if base:
        return f"{base.rstrip('/')}/jobs/{job.id}"
    if job.pull_request_url:
        return job.pull_request_url
    return f"scout://jobs/{job.id}"


def severity_for_report(report: VerificationReport) -> Severity:
    """Map a report's recommendation onto a notification severity.

    A clean ``PASS`` maps to ``INFO``, which the default routing table sends nowhere. That
    is intentional: a successful verification is visible in the dashboard and the GitHub
    check, and announcing every success is how a notification channel gets muted.
    """
    if report.recommendation is Recommendation.FAIL:
        return Severity.CRITICAL
    if report.recommendation in {
        Recommendation.REQUEST_CHANGES,
        Recommendation.REQUIRES_HUMAN_REVIEW,
    }:
        return Severity.HIGH
    if report.recommendation is Recommendation.PASS_WITH_WARNINGS:
        return Severity.MEDIUM
    return Severity.INFO


@dataclass(frozen=True, slots=True)
class _Dispatch:
    """Everything one notification event needs, resolved before routing."""

    event_kind: NotificationEventKind
    severity: Severity
    dedup_key: str
    subject: str
    template: str
    context: dict[str, Any]
    job: VerificationJob
    repository_config: ScoutRepositoryConfig | NotificationsConfig | None = None
    question_id: str | None = None
    finding_id: str | None = None


class NotificationService:
    """Route, render, and deliver notifications; return one record per attempt."""

    def __init__(
        self,
        *,
        adapters: Sequence[NotificationChannelAdapter],
        renderer: TemplateRenderer | None = None,
        history: NotificationHistory | None = None,
        routing: RoutingPolicy | None = None,
        dedup_window_seconds: int = DEFAULT_DEDUP_WINDOW_SECONDS,
        repository_config_resolver: Callable[[str], ScoutRepositoryConfig | None] | None = None,
        job_url_builder: Callable[[VerificationJob], str] | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._adapters: dict[NotificationChannel, NotificationChannelAdapter] = {
            adapter.name: adapter for adapter in adapters
        }
        self._renderer = renderer if renderer is not None else TemplateRenderer()
        self._clock = clock
        # The default history shares this clock so that "sent recently" and the record's
        # ``sent_at`` can never disagree; a mismatched clock silently defeats deduplication.
        self._history = history if history is not None else InMemoryNotificationHistory(clock=clock)
        self._routing = routing if routing is not None else RoutingPolicy()
        self._dedup_window_seconds = dedup_window_seconds
        self._repository_config_resolver = repository_config_resolver
        self._job_url_builder = job_url_builder if job_url_builder is not None else default_job_url

    @property
    def history(self) -> NotificationHistory:
        """The history used for deduplication."""
        return self._history

    # -- Events ---------------------------------------------------------------------

    async def notify_question(
        self,
        question: Question,
        job: VerificationJob,
    ) -> list[NotificationRecord]:
        """Notify that a human answer is required (spec sections 28 and 72)."""
        job_url = self._job_url_builder(job)
        return await self._dispatch(
            _Dispatch(
                event_kind=NotificationEventKind.QUESTION,
                severity=question.severity,
                dedup_key=dedup_key_for(question),
                subject=_truncate_subject(f"[Scout] Question: {question.title}"),
                template="question.md.j2",
                context=_question_context(question, job, job_url),
                job=job,
                repository_config=self._repository_config_for(job),
                question_id=question.id,
            )
        )

    async def notify_high_severity_finding(
        self,
        finding: Finding,
        job: VerificationJob,
    ) -> list[NotificationRecord]:
        """Notify about a HIGH or CRITICAL finding.

        The caller decides what is worth announcing; this method reports the finding's own
        severity, so routing remains the single place that decides channels.
        """
        job_url = self._job_url_builder(job)
        return await self._dispatch(
            _Dispatch(
                event_kind=NotificationEventKind.HIGH_SEVERITY_FINDING,
                severity=finding.severity,
                dedup_key=dedup_key_for(finding),
                subject=_truncate_subject(
                    f"[Scout] {finding.severity.value} finding: {finding.title}"
                ),
                template="critical_failure.md.j2",
                context=_finding_context(finding, job, job_url),
                job=job,
                repository_config=self._repository_config_for(job),
                finding_id=finding.id,
            )
        )

    async def notify_verification_complete(
        self,
        report: VerificationReport,
        job: VerificationJob,
    ) -> list[NotificationRecord]:
        """Notify that verification finished, at the severity the recommendation implies."""
        severity = severity_for_report(report)
        job_url = self._job_url_builder(job)
        return await self._dispatch(
            _Dispatch(
                event_kind=NotificationEventKind.VERIFICATION_COMPLETE,
                severity=severity,
                dedup_key=dedup_key_for(
                    job,
                    event_kind=NotificationEventKind.VERIFICATION_COMPLETE,
                ),
                subject=_truncate_subject(
                    f"[Scout] Verification {report.recommendation.value} — "
                    f"{report.repository_full_name} PR #{report.pull_request_number}"
                ),
                template="verification_complete.md.j2",
                context=_report_context(report, job, job_url, severity),
                job=job,
                repository_config=self._repository_config_for(job),
            )
        )

    async def notify_approval_required(self, job: VerificationJob) -> list[NotificationRecord]:
        """Notify that Scout is waiting for a human approval decision."""
        job_url = self._job_url_builder(job)
        return await self._dispatch(
            _Dispatch(
                event_kind=NotificationEventKind.APPROVAL_REQUIRED,
                severity=Severity.HIGH,
                dedup_key=dedup_key_for(
                    job,
                    event_kind=NotificationEventKind.APPROVAL_REQUIRED,
                ),
                subject=_truncate_subject(
                    f"[Scout] Approval required — {job.repository_full_name} "
                    f"PR #{job.pull_request_number}"
                ),
                template="approval_required.md.j2",
                context=_approval_context(job, job_url),
                job=job,
                repository_config=self._repository_config_for(job),
            )
        )

    # -- Internals ------------------------------------------------------------------

    def _repository_config_for(
        self,
        job: VerificationJob,
    ) -> ScoutRepositoryConfig | None:
        if self._repository_config_resolver is None:
            return None
        return self._repository_config_resolver(job.repository_full_name)

    async def _dispatch(self, request: _Dispatch) -> list[NotificationRecord]:
        """Apply routing and dedup, then attempt every selected channel."""
        policy_channels = self._routing.channels_for(request.severity)
        recently_everywhere = bool(policy_channels) and all(
            self._history.was_recently_sent(request.dedup_key, channel, self._dedup_window_seconds)
            for channel in policy_channels
        )

        decision = self._routing.should_notify(
            severity=request.severity,
            event_kind=request.event_kind,
            repository_config=request.repository_config,
            recently_sent=recently_everywhere,
        )
        if decision.suppressed_reason is not None:
            # Record the suppression once per channel that would have carried it, so the
            # audit trail shows what was considered rather than only what was skipped.
            fallback = decision.channels or policy_channels
            return [
                self._suppressed_record(request, channel, decision.suppressed_reason)
                for channel in _ordered_channels(fallback)
            ]

        records: list[NotificationRecord] = []
        for channel in _ordered_channels(decision.channels):
            if self._history.was_recently_sent(
                request.dedup_key, channel, self._dedup_window_seconds
            ):
                records.append(self._suppressed_record(request, channel, SUPPRESSED_DUPLICATE))
                continue
            records.append(await self._deliver(request, channel))
        return records

    async def _deliver(
        self,
        request: _Dispatch,
        channel: NotificationChannel,
    ) -> NotificationRecord:
        """Render and send on one channel, converting any failure into a record."""
        adapter = self._adapters.get(channel)
        if adapter is None:
            return self._failed_record(
                request,
                channel,
                f"no adapter is registered for channel {channel.value}",
            )

        try:
            body = self._renderer.render(request.template, request.context)
            message = NotificationMessage(
                subject=_truncate_subject(request.subject),
                body=body,
                severity=request.severity,
                job_url=request.context.get("job_url"),
                metadata=self._metadata_for(request, channel),
            )
        except Exception as exc:
            # A template or context bug must not abort delivery on the other channels.
            logger.warning(
                "notification rendering failed",
                extra={"channel": channel.value, "error": type(exc).__name__},
            )
            return self._failed_record(
                request,
                channel,
                f"rendering {request.template} failed ({type(exc).__name__})",
            )

        try:
            result = await adapter.send(message)
        except Exception as exc:
            # Adapters are expected to classify failures themselves; an unclassified
            # exception is still recorded rather than propagated (Rule 12).
            logger.warning(
                "notification adapter raised",
                extra={"channel": channel.value, "error": type(exc).__name__},
            )
            return self._failed_record(
                request,
                channel,
                f"{channel.value} adapter raised {type(exc).__name__}",
            )

        if not result.delivered:
            return self._failed_record(
                request,
                channel,
                result.error if result.error is not None else "delivery failed",
            )

        record = self._sent_record(request, channel, body)
        self._history.record(record)
        logger.info(
            "notification delivered",
            extra={
                "channel": channel.value,
                "severity": request.severity.value,
                "dedup_key": request.dedup_key,
                "provider_message_id": result.provider_message_id,
            },
        )
        return record

    def _metadata_for(
        self,
        request: _Dispatch,
        channel: NotificationChannel,
    ) -> dict[str, str]:
        """Build channel-specific metadata (recipients, HTML alternative)."""
        metadata = {
            "repository_full_name": request.job.repository_full_name,
            "pull_request_number": str(request.job.pull_request_number),
            "job_id": request.job.id,
            "dedup_key": request.dedup_key,
        }
        if channel is not NotificationChannel.EMAIL:
            return metadata

        recipients = email_recipients_for(request.repository_config)
        if recipients:
            metadata["email_recipients"] = ", ".join(recipients)
        try:
            metadata["html_body"] = self._renderer.render(
                _html_template_name(request.template), request.context
            )
        except Exception as exc:
            # The adapter falls back to an escaped plain-text HTML part, so this is a
            # degraded email rather than a lost one.
            logger.warning(
                "html rendering failed; sending a text-only email",
                extra={"error": type(exc).__name__},
            )
        return metadata

    def _sent_record(
        self,
        request: _Dispatch,
        channel: NotificationChannel,
        body: str,
    ) -> NotificationRecord:
        return NotificationRecord(
            id=new_id(IdKind.NOTIFICATION),
            job_id=request.job.id,
            question_id=request.question_id,
            finding_id=request.finding_id,
            channel=channel,
            severity=request.severity,
            subject=_truncate_subject(request.subject),
            body=body,
            dedup_key=request.dedup_key,
            status=NotificationStatus.SENT,
            attempts=1,
            sent_at=self._clock(),
        )

    def _failed_record(
        self,
        request: _Dispatch,
        channel: NotificationChannel,
        error: str,
    ) -> NotificationRecord:
        return NotificationRecord(
            id=new_id(IdKind.NOTIFICATION),
            job_id=request.job.id,
            question_id=request.question_id,
            finding_id=request.finding_id,
            channel=channel,
            severity=request.severity,
            subject=_truncate_subject(request.subject),
            body=f"Delivery failed: {error}",
            dedup_key=request.dedup_key,
            status=NotificationStatus.FAILED,
            attempts=1,
            error=error,
        )

    def _suppressed_record(
        self,
        request: _Dispatch,
        channel: NotificationChannel,
        reason: str,
    ) -> NotificationRecord:
        return NotificationRecord(
            id=new_id(IdKind.NOTIFICATION),
            job_id=request.job.id,
            question_id=request.question_id,
            finding_id=request.finding_id,
            channel=channel,
            severity=request.severity,
            subject=_truncate_subject(request.subject),
            body=f"Suppressed: {reason}",
            dedup_key=request.dedup_key,
            status=NotificationStatus.SUPPRESSED,
            attempts=0,
            error=reason,
        )


def _ordered_channels(channels: Iterable[NotificationChannel]) -> list[NotificationChannel]:
    """Order channels deterministically by the documented preference."""
    return sorted(channels, key=lambda channel: _CHANNEL_SORT_ORDER[channel])


def _html_template_name(text_template: str) -> str:
    """Return the HTML sibling of a text template name."""
    if text_template.endswith(_TEXT_TEMPLATE_SUFFIX):
        return f"{text_template[: -len(_TEXT_TEMPLATE_SUFFIX)]}{_HTML_TEMPLATE_SUFFIX}"
    return text_template


def _truncate_subject(value: str) -> str:
    """Collapse whitespace and fit a subject within the domain's 300-character limit."""
    cleaned = " ".join(value.split())
    if not cleaned:
        return "Scout notification"
    if len(cleaned) <= _MAX_SUBJECT_LENGTH:
        return cleaned
    return f"{cleaned[: _MAX_SUBJECT_LENGTH - 1].rstrip()}\u2026"


def _question_context(question: Question, job: VerificationJob, job_url: str) -> dict[str, Any]:
    """Build the question context, including the five elements spec section 28 requires."""
    return {
        "question": question,
        "job": job,
        "job_url": job_url,
        "repository_full_name": job.repository_full_name,
        "pull_request_number": job.pull_request_number,
        "severity": question.severity.value,
        "why_asking": question.context or question.description,
        "investigation_summary": question.investigation_summary,
        "evidence": list(question.evidence),
        "options": list(question.options),
        "default_assumption": question.default_assumption,
    }


def _finding_context(finding: Finding, job: VerificationJob, job_url: str) -> dict[str, Any]:
    """Build the high-severity-finding context."""
    return {
        "finding": finding,
        "job": job,
        "job_url": job_url,
        "repository_full_name": finding.repository,
        "pull_request_number": job.pull_request_number,
        "severity": finding.severity.value,
        "locator": finding.locator(),
        "evidence": list(finding.evidence),
    }


def _report_context(
    report: VerificationReport,
    job: VerificationJob,
    job_url: str,
    severity: Severity,
) -> dict[str, Any]:
    """Build the verification-complete context."""
    return {
        "report": report,
        "job": job,
        "job_url": job_url,
        "repository_full_name": report.repository_full_name,
        "pull_request_number": report.pull_request_number,
        "commit_sha": report.commit_sha,
        "recommendation": report.recommendation.value,
        "severity": severity.value,
        "summary": report.executive_summary,
        "tests": report.tests,
        "severity_counts": {
            name.value: count for name, count in report.findings_by_severity().items() if count
        },
        "unverified_areas": list(report.unverified_areas),
    }


def _approval_context(job: VerificationJob, job_url: str) -> dict[str, Any]:
    """Build the approval-required context."""
    return {
        "job": job,
        "job_url": job_url,
        "repository_full_name": job.repository_full_name,
        "pull_request_number": job.pull_request_number,
        "pull_request_title": job.pull_request_title,
        "risk_level": job.risk_level.value,
        "commit_sha": job.head_sha,
    }
