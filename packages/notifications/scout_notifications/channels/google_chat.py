"""Google Chat incoming-webhook adapter (spec section 47).

Payload choice: the plain ``{"text": ...}`` message format, not a card. Cards bring a
versioned schema (``cardsV2``) whose field names change between Google releases, and the
notification body is already rendered deliberate Markdown with everything the reader
needs. ``text`` is the format Google documents for incoming webhooks and it renders the
bold/heading subset the templates emit.

Secret handling: the webhook URL embeds a key, so it is treated as a credential. It is
never logged, and provider errors are reported by HTTP status rather than by echoing an
exception message — ``httpx`` error strings can contain the request URL, which would leak
the key into ``NotificationRecord.error`` and from there into the database and dashboard.
"""

from __future__ import annotations

import os
from typing import Final

import httpx

from scout_domain.enums import NotificationChannel, Severity
from scout_notifications.base import DeliveryResult, NotificationMessage
from scout_notifications.observability import get_logger

__all__ = ["GOOGLE_CHAT_WEBHOOK_ENV", "GoogleChatChannelAdapter"]

logger = get_logger(__name__)

#: Environment variable holding the incoming-webhook URL.
GOOGLE_CHAT_WEBHOOK_ENV: Final[str] = "SCOUT_GOOGLE_CHAT_WEBHOOK_URL"

#: Request timeout. A hung webhook must not stall the orchestrator's notification step.
DEFAULT_TIMEOUT_SECONDS: Final[float] = 10.0

#: Inclusive lower bound of the HTTP success range.
_SUCCESS_FLOOR: Final[int] = 200

#: Exclusive upper bound of the HTTP success range.
_SUCCESS_CEILING: Final[int] = 300

#: HTTP status above which a failure is a server-side problem and worth retrying.
_SERVER_ERROR_THRESHOLD: Final[int] = 500

_TOO_MANY_REQUESTS: Final[int] = 429


class GoogleChatChannelAdapter:
    """Deliver notifications to a Google Chat space via incoming webhook."""

    name: NotificationChannel = NotificationChannel.GOOGLE_CHAT

    def __init__(
        self,
        *,
        webhook_url: str | None = None,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        resolved = (
            webhook_url if webhook_url is not None else os.environ.get(GOOGLE_CHAT_WEBHOOK_ENV, "")
        )
        self._webhook_url = resolved.strip()
        self._client = client
        self._owns_client = client is None
        self._timeout_seconds = timeout_seconds

    @property
    def configured(self) -> bool:
        """Whether a webhook URL was supplied (via argument or environment)."""
        return bool(self._webhook_url)

    def supports(self, channel: NotificationChannel) -> bool:
        """Whether this adapter serves ``channel``."""
        return channel is self.name

    async def aclose(self) -> None:
        """Close the client this adapter created, if it owns one."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        """POST the rendered message to the configured webhook."""
        if not self._webhook_url:
            return DeliveryResult.failure(
                f"{GOOGLE_CHAT_WEBHOOK_ENV} is not configured",
                retryable=False,
            )

        payload = {"text": self._render_text(message)}
        try:
            response = await self._get_client().post(self._webhook_url, json=payload)
        except httpx.TimeoutException:
            logger.warning(
                "google chat delivery timed out",
                extra={"severity": message.severity.value},
            )
            return DeliveryResult.failure("google chat request timed out", retryable=True)
        except httpx.HTTPError:
            # The exception text is intentionally not included: it may contain the URL.
            logger.warning(
                "google chat transport error",
                extra={"severity": message.severity.value},
            )
            return DeliveryResult.failure(
                "google chat request failed (transport error)",
                retryable=True,
            )

        return self._classify(response, severity=message.severity)

    def _get_client(self) -> httpx.AsyncClient:
        """Return the injected client, or lazily create and retain one."""
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout_seconds)
            self._owns_client = True
        return self._client

    @staticmethod
    def _render_text(message: NotificationMessage) -> str:
        """Render the webhook text: bold subject, blank line, body."""
        return f"*{message.subject}*\n\n{message.body}"

    def _classify(self, response: httpx.Response, *, severity: Severity) -> DeliveryResult:
        """Map an HTTP response onto a delivery result, following the retry policy."""
        status = response.status_code
        if _SUCCESS_FLOOR <= status < _SUCCESS_CEILING:
            logger.info(
                "google chat delivered",
                extra={"severity": severity.value, "status": status},
            )
            return DeliveryResult.success(self._message_id(response))

        retryable = status == _TOO_MANY_REQUESTS or status >= _SERVER_ERROR_THRESHOLD
        logger.warning(
            "google chat rejected the message",
            extra={"severity": severity.value, "status": status, "retryable": retryable},
        )
        return DeliveryResult.failure(
            f"google chat returned HTTP {status}",
            retryable=retryable,
        )

    @staticmethod
    def _message_id(response: httpx.Response) -> str | None:
        """Extract the provider message id when the response carries one."""
        try:
            payload = response.json()
        except ValueError:
            return None
        if isinstance(payload, dict):
            name = payload.get("name")
            if isinstance(name, str) and name:
                return name
        return None
