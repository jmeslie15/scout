"""Notification channel contract (spec section 47, Rule 2).

Every delivery channel sits behind :class:`NotificationChannelAdapter`, so the routing and
templating logic never knows whether a message left the process through an HTTP webhook,
SMTP, or a GitHub comment. Offline operation is then a matter of substituting adapters,
not of disabling the notification code path (Rule 2's "every external integration must
have an interface").
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from pydantic import Field, model_validator

from scout_domain.enums import NotificationChannel, Severity
from scout_domain.models.base import ScoutModel

__all__ = [
    "DeliveryResult",
    "NotificationChannelAdapter",
    "NotificationMessage",
]


class NotificationMessage(ScoutModel):
    """A rendered notification, ready for one channel.

    ``metadata`` carries channel-specific routing facts (repository, pull request number,
    an HTML alternative for email). It is ``dict[str, str]`` rather than a union of typed
    payloads so adding a channel does not change this contract — but values are strings,
    so a secret object can never be smuggled through it by accident.
    """

    subject: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1)
    severity: Severity
    #: Deep link to the job in the dashboard. Rendered into the body by the templates.
    job_url: str | None = Field(default=None, max_length=2000)
    metadata: dict[str, str] = Field(default_factory=dict)


class DeliveryResult(ScoutModel):
    """The outcome of one delivery attempt.

    The validator encodes Rule 12 at the boundary: a failure must carry an error, and a
    success must not pretend one happened. The caller therefore never has to guess whether
    ``delivered=False`` means "refused" or "silently did nothing".
    """

    delivered: bool
    provider_message_id: str | None = Field(default=None, max_length=500)
    error: str | None = Field(default=None, max_length=4000)
    #: Whether retrying the same message could succeed. 429 and 5xx are retryable; a 4xx
    #: that is not 429 will fail identically forever, so it is not.
    retryable: bool = False

    @model_validator(mode="after")
    def _validate(self) -> DeliveryResult:
        if self.delivered and self.error is not None:
            raise ValueError("a delivered result must not carry an error")
        if not self.delivered and not self.error:
            raise ValueError("a failed delivery must record an error")
        return self

    @classmethod
    def success(cls, provider_message_id: str | None = None) -> DeliveryResult:
        """Build a successful result."""
        return cls(delivered=True, provider_message_id=provider_message_id)

    @classmethod
    def failure(cls, error: str, *, retryable: bool) -> DeliveryResult:
        """Build a failed result with an explicit retry classification."""
        return cls(delivered=False, error=error, retryable=retryable)


@runtime_checkable
class NotificationChannelAdapter(Protocol):
    """One delivery channel.

    ``name`` is the channel this adapter serves; ``supports`` lets a caller filter a
    multi-channel adapter without knowing its type. ``send`` must not raise for an
    expected provider failure — it returns a :class:`DeliveryResult` so the service can
    record the failure and continue with the other channels.
    """

    name: NotificationChannel

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        """Attempt delivery and classify the outcome."""
        ...

    def supports(self, channel: NotificationChannel) -> bool:
        """Whether this adapter can deliver on ``channel``."""
        ...
