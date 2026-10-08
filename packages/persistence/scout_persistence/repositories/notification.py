"""Notification persistence and deduplication (spec section 47).

Scout must not spam, and "must not" here means the database enforces it. The unique
constraint on ``(dedup_key, channel)`` makes a second notification about the same
underlying fact on the same channel impossible, so two workers racing to notify about
the same question at the same severity produce one message rather than two.

:meth:`NotificationRepository.was_recently_sent` is the *read* half of that story and is
deliberately narrower than the constraint: it answers "was something with this key
actually delivered in the last N minutes?", which is the question a notifier asks before
composing a message. A row that exists but is ``PENDING`` or ``FAILED`` must not suppress
a retry — otherwise a transient SMTP failure would silence Scout permanently, which is
the worst possible outcome for an alerting system.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scout_domain.enums import NotificationChannel, Severity
from scout_domain.ids import IdKind, new_id
from scout_domain.models.base import utcnow
from scout_domain.models.notification import NotificationRecord, NotificationStatus
from scout_persistence.mapping import from_notification, to_notification
from scout_persistence.repositories.base import BaseRepository
from scout_persistence.tables import NotificationRow

__all__ = ["NotificationRepository"]


class NotificationRepository(BaseRepository):
    """Reads and writes ``notifications`` rows."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to ``session``."""
        super().__init__(session)

    async def record(self, notification: NotificationRecord) -> NotificationRow:
        """Insert a notification attempt and return its row."""
        row = from_notification(notification)
        self.session.add(row)
        await self.session.flush()
        return row

    async def enqueue(
        self,
        *,
        channel: NotificationChannel,
        subject: str,
        body: str,
        dedup_key: str,
        severity: Severity = Severity.MEDIUM,
        job_id: str | None = None,
        question_id: str | None = None,
        finding_id: str | None = None,
    ) -> NotificationRow:
        """Record a notification that is about to be attempted.

        The row is written *before* delivery rather than after, so a crash mid-send still
        leaves evidence that Scout tried — an alert that vanished without a trace is
        indistinguishable from one that was never raised.
        """
        notification = NotificationRecord(
            id=new_id(IdKind.NOTIFICATION),
            channel=channel,
            subject=subject,
            body=body,
            dedup_key=dedup_key,
            severity=severity,
            job_id=job_id,
            question_id=question_id,
            finding_id=finding_id,
            status=NotificationStatus.PENDING,
        )
        return await self.record(notification)

    async def get(self, notification_id: str) -> NotificationRow | None:
        """Return the notification row with ``notification_id``, or ``None``."""
        return await self.session.get(NotificationRow, notification_id)

    async def get_by_dedup_key(
        self,
        dedup_key: str,
        channel: NotificationChannel,
    ) -> NotificationRow | None:
        """Return the row for a ``(dedup_key, channel)`` pair, or ``None``.

        At most one row can exist per pair: the unique constraint guarantees it, so this
        returns a row rather than a list.
        """
        return await self.session.scalar(
            select(NotificationRow).where(
                NotificationRow.dedup_key == dedup_key,
                NotificationRow.channel == channel.value,
            )
        )

    async def was_recently_sent(
        self,
        dedup_key: str,
        channel: NotificationChannel,
        within: timedelta,
        *,
        now: datetime | None = None,
    ) -> bool:
        """Whether a delivery for ``(dedup_key, channel)`` succeeded within ``within``.

        Only ``SENT`` rows count. A ``PENDING`` row means a delivery is in flight (and the
        caller should not start a second one), a ``FAILED`` or ``SUPPRESSED`` row means
        the fact was never delivered, and neither should suppress a fresh attempt.
        """
        cutoff = (now or utcnow()) - within
        row = await self.session.scalar(
            select(NotificationRow.id)
            .where(
                NotificationRow.dedup_key == dedup_key,
                NotificationRow.channel == channel.value,
                NotificationRow.status == NotificationStatus.SENT.value,
                NotificationRow.sent_at.is_not(None),
                NotificationRow.sent_at >= cutoff,
            )
            .limit(1)
        )
        return row is not None

    async def list_for_job(self, job_id: str) -> list[NotificationRow]:
        """Return notifications associated with ``job_id``, newest first."""
        result = await self.session.scalars(
            select(NotificationRow)
            .where(NotificationRow.job_id == job_id)
            .order_by(NotificationRow.created_at.desc(), NotificationRow.id.desc())
        )
        return list(result)

    async def list_pending(self, *, limit: int = 100) -> list[NotificationRow]:
        """Return notifications awaiting delivery, oldest first."""
        result = await self.session.scalars(
            select(NotificationRow)
            .where(NotificationRow.status == NotificationStatus.PENDING.value)
            .order_by(NotificationRow.created_at.asc())
            .limit(limit)
        )
        return list(result)

    async def mark_sent(
        self,
        notification_id: str,
        *,
        sent_at: datetime | None = None,
    ) -> NotificationRow:
        """Mark a notification delivered and stamp the delivery attempt."""
        row = await self.session.get(NotificationRow, notification_id)
        if row is None:
            raise LookupError(f"no notification {notification_id!r}")
        row.status = NotificationStatus.SENT.value
        row.sent_at = sent_at or utcnow()
        row.attempts = row.attempts + 1
        row.error = None
        self.stamp(row)
        await self.session.flush()
        return row

    async def mark_failed(self, notification_id: str, *, error: str) -> NotificationRow:
        """Record a failed delivery attempt, keeping the row retryable."""
        row = await self.session.get(NotificationRow, notification_id)
        if row is None:
            raise LookupError(f"no notification {notification_id!r}")
        row.status = NotificationStatus.FAILED.value
        row.attempts = row.attempts + 1
        row.error = error
        self.stamp(row)
        await self.session.flush()
        return row

    async def suppress(self, notification_id: str, *, reason: str) -> NotificationRow:
        """Record that a notification was deliberately not sent, and why.

        ``reason`` is required by the domain model: a suppressed alert with no explanation
        is indistinguishable from a bug that dropped it.
        """
        row = await self.session.get(NotificationRow, notification_id)
        if row is None:
            raise LookupError(f"no notification {notification_id!r}")
        row.status = NotificationStatus.SUPPRESSED.value
        row.error = reason
        self.stamp(row)
        await self.session.flush()
        return row

    @staticmethod
    def to_domain(row: NotificationRow) -> NotificationRecord:
        """Convert a notification row into its domain model."""
        return to_notification(row)
