"""Notification dispatch that can never fail a job (spec section 47).

A notification is a best-effort side channel. A webhook being down, an SMTP server refusing
mail, or the ``scout_notifications`` package being absent must not turn a completed verification
into a failed job -- the report and the GitHub check run are the deliverables, and they are
already recorded by the time this runs.

But "best effort" is not "silent". Every dispatch returns records, and a dispatch that could not
even be attempted returns a *reason* that the caller writes to the audit ledger. So the outcome
of a notification attempt is always visible; only its failure is non-fatal.

The whole ``scout_notifications`` import is deferred and guarded because that package is listed
as an incomplete workstream: the orchestrator must remain importable and runnable without it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final

from scout_domain.enums import Severity
from scout_observability import get_logger

if TYPE_CHECKING:
    from collections.abc import Sequence

    from scout_domain.models.finding import Finding
    from scout_domain.models.job import VerificationJob
    from scout_domain.models.notification import NotificationRecord
    from scout_domain.models.question import Question
    from scout_domain.models.report import VerificationReport
    from scout_orchestrator.settings import Settings

__all__ = ["NotificationDispatch", "NotificationDispatcher"]

_log = get_logger(__name__)

#: Severities worth waking somebody up for. MEDIUM findings ride along in the report; HIGH and
#: CRITICAL are announced (spec section 47).
_NOTIFY_AT_OR_ABOVE: Final = Severity.HIGH

#: Ceiling on how many individual findings are announced in one job. Beyond this the completion
#: notification carries the count, and the dashboard carries the rest.
_MAX_FINDING_NOTIFICATIONS: Final = 10


@dataclass(frozen=True, slots=True)
class NotificationDispatch:
    """What a dispatch attempt produced, and why nothing happened when nothing did."""

    records: tuple[NotificationRecord, ...] = ()
    #: Set when the attempt could not be made at all. Recorded on the audit ledger by the caller.
    skipped_reason: str | None = None
    #: Set when the notification machinery raised. Non-fatal, but recorded.
    error: str | None = None

    @property
    def attempted(self) -> bool:
        """Whether any channel was actually invoked."""
        return bool(self.records)

    def merge(self, other: NotificationDispatch) -> NotificationDispatch:
        """Combine two dispatches, preserving the first concrete reason or error."""
        return NotificationDispatch(
            records=(*self.records, *other.records),
            skipped_reason=self.skipped_reason or other.skipped_reason,
            error=self.error or other.error,
        )


@dataclass(slots=True)
class _ServiceState:
    """Lazily built notification service plus the reason it is unavailable."""

    service: object | None = None
    reason: str | None = None
    built: bool = False
    notes: list[str] = field(default_factory=list)


class NotificationDispatcher:
    """Builds a notification service from settings and dispatches job events through it."""

    def __init__(self, *, settings: Settings) -> None:
        """Remember the settings; the service itself is built on first use."""
        self._settings = settings
        self._state = _ServiceState()

    async def notify_questions(
        self, questions: Sequence[Question], job: VerificationJob
    ) -> NotificationDispatch:
        """Announce every open question that needs an answer."""
        return await self._dispatch_many(
            lambda service, question: service.notify_question(question, job),
            list(questions),
            event="question",
        )

    async def notify_findings(
        self, findings: Sequence[Finding], job: VerificationJob
    ) -> NotificationDispatch:
        """Announce HIGH and CRITICAL findings."""
        serious = [finding for finding in findings if finding.severity >= _NOTIFY_AT_OR_ABOVE][
            :_MAX_FINDING_NOTIFICATIONS
        ]
        return await self._dispatch_many(
            lambda service, finding: service.notify_high_severity_finding(finding, job),
            serious,
            event="finding",
        )

    async def notify_completion(
        self, report: VerificationReport, job: VerificationJob
    ) -> NotificationDispatch:
        """Announce that verification finished, at the severity the recommendation implies."""
        return await self._dispatch_many(
            lambda service, _item: service.notify_verification_complete(report, job),
            [None],
            event="completion",
        )

    # -- Internals -------------------------------------------------------------------

    async def _dispatch_many(
        self,
        call: object,
        items: Sequence[object],
        *,
        event: str,
    ) -> NotificationDispatch:
        """Attempt every item, merging records and never raising."""
        if not items:
            return NotificationDispatch()

        service = self._service()
        if service is None:
            return NotificationDispatch(skipped_reason=self._state.reason or "not configured")

        records: list[NotificationRecord] = []
        for item in items:
            try:
                produced = await call(service, item)  # type: ignore[operator]
            except Exception as exc:
                _log.warning(
                    "notification dispatch raised",
                    event=event,
                    error=f"{type(exc).__name__}: {exc}",
                )
                return NotificationDispatch(
                    records=tuple(records), error=f"{type(exc).__name__}: {exc}"
                )
            records.extend(produced or [])
        return NotificationDispatch(records=tuple(records))

    def _service(self) -> object | None:
        """Build the notification service once, recording why it is unavailable if it is."""
        if self._state.built:
            return self._state.service
        self._state.built = True

        if not self._settings.notifications_enabled:
            self._state.reason = (
                "notifications are disabled (SCOUT_NOTIFICATIONS_ENABLED=false), so nothing "
                "was sent"
            )
            return None

        try:
            from scout_notifications import (
                EmailChannelAdapter,
                GoogleChatChannelAdapter,
                InMemoryNotificationHistory,
                NotificationService,
                SmtpSettings,
            )
        except ImportError as exc:
            self._state.reason = (
                f"the scout_notifications package is not installed ({type(exc).__name__}), so "
                "nothing was sent"
            )
            return None

        adapters: list[Any] = []
        if self._settings.google_chat_webhook_url:
            adapters.append(
                GoogleChatChannelAdapter(webhook_url=self._settings.google_chat_webhook_url)
            )
        if self._settings.smtp_host and self._settings.email_to:
            recipients = [
                address.strip() for address in self._settings.email_to.split(",") if address.strip()
            ]
            adapters.append(
                EmailChannelAdapter(
                    settings=SmtpSettings(
                        host=self._settings.smtp_host,
                        port=self._settings.smtp_port,
                        username=self._settings.smtp_username,
                        password=self._settings.smtp_password,
                        sender=self._settings.email_from,
                        recipients=tuple(recipients),
                    ),
                    recipients=recipients,
                )
            )

        if not adapters:
            self._state.reason = (
                "notifications are enabled but no channel is configured "
                "(SCOUT_GOOGLE_CHAT_WEBHOOK_URL or SCOUT_SMTP_HOST + SCOUT_EMAIL_TO)"
            )
            return None

        self._state.service = NotificationService(
            adapters=adapters,
            history=InMemoryNotificationHistory(),
        )
        return self._state.service
