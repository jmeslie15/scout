"""Notification records and severity routing (spec section 47).

Scout must not spam. Two mechanisms enforce that: a ``dedup_key`` that collapses
repeated notifications about the same underlying fact, and severity-based routing so
that only actionable items reach a human channel.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from scout_domain.enums import NotificationChannel, Severity
from scout_domain.models.base import ScoutModel, utcnow

__all__ = ["NotificationRecord", "NotificationStatus"]


class NotificationStatus(StrEnum):
    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"
    SUPPRESSED = "SUPPRESSED"


class NotificationRecord(ScoutModel):
    """One attempted delivery on one channel."""

    id: str = Field(min_length=1)
    job_id: str | None = Field(default=None, max_length=100)
    question_id: str | None = Field(default=None, max_length=100)
    finding_id: str | None = Field(default=None, max_length=100)

    channel: NotificationChannel
    severity: Severity = Severity.MEDIUM
    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1)

    #: Stable key for collapsing duplicates, e.g. ``question:{id}:severity``. A second
    #: notification with the same key and no material change is suppressed.
    dedup_key: str = Field(min_length=1, max_length=300)

    status: NotificationStatus = NotificationStatus.PENDING
    attempts: int = Field(default=0, ge=0)
    sent_at: datetime | None = None
    error: str | None = Field(default=None, max_length=4000)

    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _validate(self) -> NotificationRecord:
        if self.status is NotificationStatus.SENT and self.sent_at is None:
            raise ValueError("a SENT notification must record sent_at")
        if self.status is NotificationStatus.FAILED and not self.error:
            raise ValueError("a FAILED notification must record an error")
        if self.status is NotificationStatus.SUPPRESSED and not self.error:
            raise ValueError("a SUPPRESSED notification must record why it was suppressed")
        return self

    @property
    def is_terminal(self) -> bool:
        return self.status in {NotificationStatus.SENT, NotificationStatus.SUPPRESSED}
