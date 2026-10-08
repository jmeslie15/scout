"""SMTP email adapter (spec section 47).

``smtplib`` is blocking, so each send runs in a worker thread via ``asyncio.to_thread``.
That is deliberate: an async SMTP client would be a new dependency for one code path, and
Rule 1 says not to add infrastructure before it is needed.

Secret handling: ``SCOUT_SMTP_PASSWORD`` is never logged and never included in a failure
message. Failures are reported by SMTP status code, which is enough to decide whether to
retry and contains no credentials.

STARTTLS: enabled by default and applied only when the server advertises the extension, so
a relay that does not support it still works. Port 465 is treated as implicit TLS
(``SMTP_SSL``), where STARTTLS would be a protocol error.
"""

from __future__ import annotations

import asyncio
import html
import os
import smtplib
import ssl
from collections.abc import Sequence
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Final

from scout_domain.enums import NotificationChannel
from scout_domain.errors import ConfigurationError
from scout_notifications.base import DeliveryResult, NotificationMessage
from scout_notifications.observability import get_logger

__all__ = [
    "EMAIL_FROM_ENV",
    "EMAIL_TO_ENV",
    "SMTP_HOST_ENV",
    "SMTP_PASSWORD_ENV",
    "SMTP_PORT_ENV",
    "SMTP_USERNAME_ENV",
    "EmailChannelAdapter",
    "SmtpSettings",
    "build_email_message",
]

logger = get_logger(__name__)

SMTP_HOST_ENV: Final[str] = "SCOUT_SMTP_HOST"
SMTP_PORT_ENV: Final[str] = "SCOUT_SMTP_PORT"
SMTP_USERNAME_ENV: Final[str] = "SCOUT_SMTP_USERNAME"
SMTP_PASSWORD_ENV: Final[str] = "SCOUT_SMTP_PASSWORD"
EMAIL_FROM_ENV: Final[str] = "SCOUT_EMAIL_FROM"
EMAIL_TO_ENV: Final[str] = "SCOUT_EMAIL_TO"

DEFAULT_SMTP_PORT: Final[int] = 587
DEFAULT_SENDER: Final[str] = "scout@edrevel.ai"
DEFAULT_TIMEOUT_SECONDS: Final[float] = 30.0

#: Port with implicit TLS. STARTTLS on this port would fail, so it uses ``SMTP_SSL``.
_IMPLICIT_TLS_PORT: Final[int] = 465

#: SMTP reply codes in this range are transient (``4xx``); ``5xx`` is permanent.
_TRANSIENT_CODE_FLOOR: Final[int] = 400
_PERMANENT_CODE_FLOOR: Final[int] = 500


@dataclass(frozen=True, slots=True)
class SmtpSettings:
    """Resolved SMTP configuration.

    Reading the environment is separated from sending so a deployment can validate its
    configuration at startup without opening a socket, and so tests can construct settings
    directly.
    """

    host: str = ""
    port: int = DEFAULT_SMTP_PORT
    username: str = ""
    password: str = ""
    sender: str = DEFAULT_SENDER
    recipients: tuple[str, ...] = ()
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @property
    def configured(self) -> bool:
        """Whether a host and at least one recipient are present."""
        return bool(self.host) and bool(self.recipients)

    @classmethod
    def from_environment(cls) -> SmtpSettings:
        """Read settings from the documented ``SCOUT_*`` environment variables.

        Raises:
            ConfigurationError: if ``SCOUT_SMTP_PORT`` is not an integer. Silently falling
                back to 587 would turn a typo into mail sent to the wrong place.
        """
        raw_port = os.environ.get(SMTP_PORT_ENV, "").strip()
        if raw_port:
            try:
                port = int(raw_port)
            except ValueError as exc:
                raise ConfigurationError(
                    f"{SMTP_PORT_ENV} must be an integer",
                    value=raw_port,
                ) from exc
        else:
            port = DEFAULT_SMTP_PORT

        raw_recipients = os.environ.get(EMAIL_TO_ENV, "")
        recipients = tuple(
            address.strip() for address in raw_recipients.split(",") if address.strip()
        )
        return cls(
            host=os.environ.get(SMTP_HOST_ENV, "").strip(),
            port=port,
            username=os.environ.get(SMTP_USERNAME_ENV, "").strip(),
            password=os.environ.get(SMTP_PASSWORD_ENV, ""),
            sender=os.environ.get(EMAIL_FROM_ENV, "").strip() or DEFAULT_SENDER,
            recipients=recipients,
        )


def _sanitise_header(value: str) -> str:
    """Strip CR/LF from a header value.

    Header injection is blocked by the email library as well, but sanitising here means a
    repository-derived PR title containing a newline cannot turn into a delivery failure.
    """
    return " ".join(value.split())


def build_email_message(
    message: NotificationMessage,
    *,
    sender: str,
    recipients: Sequence[str],
    html_body: str | None = None,
) -> EmailMessage:
    """Build a multipart/alternative message with a plain-text and an HTML part.

    When no HTML rendering is supplied, the plain text is escaped into a ``<pre>`` block so
    the message is still well-formed — and so untrusted text can never be interpreted as
    markup.
    """
    email = EmailMessage()
    email["Subject"] = _sanitise_header(message.subject)
    email["From"] = sender
    email["To"] = ", ".join(recipients)
    email["X-Scout-Severity"] = message.severity.value
    if message.job_url:
        email["X-Scout-Job-Url"] = _sanitise_header(message.job_url)

    email.set_content(message.body)
    rendered_html = (
        html_body
        if html_body is not None
        else (f"<html><body><pre>{html.escape(message.body)}</pre></body></html>")
    )
    email.add_alternative(rendered_html, subtype="html")
    return email


class EmailChannelAdapter:
    """Deliver notifications over SMTP."""

    name: NotificationChannel = NotificationChannel.EMAIL

    def __init__(
        self,
        *,
        settings: SmtpSettings | None = None,
        recipients: Sequence[str] | None = None,
        use_starttls: bool = True,
    ) -> None:
        self._settings = settings or SmtpSettings.from_environment()
        self._recipients = (
            tuple(recipients) if recipients is not None else self._settings.recipients
        )
        self._use_starttls = use_starttls

    def supports(self, channel: NotificationChannel) -> bool:
        """Whether this adapter serves ``channel``."""
        return channel is self.name

    async def send(self, message: NotificationMessage) -> DeliveryResult:
        """Build and send the message, classifying any SMTP failure."""
        recipients = self._recipients_for(message)
        prepared = self._prepare(message, recipients)
        if isinstance(prepared, DeliveryResult):
            return prepared

        try:
            await asyncio.to_thread(self._send_blocking, prepared, recipients)
        except (
            TimeoutError,
            smtplib.SMTPServerDisconnected,
            smtplib.SMTPConnectError,
            ConnectionError,
        ):
            # Checked before the response branch: ``SMTPConnectError`` and
            # ``SMTPServerDisconnected`` subclass ``SMTPResponseException``, which itself
            # subclasses ``OSError``, so ordering here is load-bearing. A dropped or
            # refused connection is transient and worth retrying.
            logger.warning("smtp transport failure", extra={"severity": message.severity.value})
            return DeliveryResult.failure("smtp transport failure", retryable=True)
        except smtplib.SMTPResponseException as exc:
            code = int(getattr(exc, "smtp_code", 0))
            retryable = _TRANSIENT_CODE_FLOOR <= code < _PERMANENT_CODE_FLOOR
            logger.warning(
                "smtp rejected the message",
                extra={"severity": message.severity.value, "smtp_code": code},
            )
            return DeliveryResult.failure(
                f"smtp rejected the message (code {code})", retryable=retryable
            )
        except smtplib.SMTPException as exc:
            # Recipient refusal and similar permanent protocol errors land here.
            logger.warning(
                "smtp protocol error",
                extra={"severity": message.severity.value, "error": type(exc).__name__},
            )
            return DeliveryResult.failure(
                f"smtp protocol error ({type(exc).__name__})",
                retryable=False,
            )
        except OSError:
            logger.warning("smtp transport failure", extra={"severity": message.severity.value})
            return DeliveryResult.failure("smtp transport failure", retryable=True)

        logger.info("email delivered", extra={"severity": message.severity.value})
        return DeliveryResult.success()

    def _prepare(
        self,
        message: NotificationMessage,
        recipients: Sequence[str],
    ) -> EmailMessage | DeliveryResult:
        """Validate configuration and build the message, or return the failure to report.

        Returning a result rather than raising keeps ``send`` readable: every early exit is
        a ``DeliveryResult`` the caller records, never an exception that could abort the
        other notification channels.
        """
        precheck = self._precheck(recipients)
        if precheck is not None:
            return precheck
        try:
            return build_email_message(
                message,
                sender=self._settings.sender,
                recipients=recipients,
                html_body=message.metadata.get("html_body"),
            )
        except (TypeError, ValueError) as exc:
            return DeliveryResult.failure(
                f"could not build the email message ({type(exc).__name__})",
                retryable=False,
            )

    def _precheck(self, recipients: Sequence[str]) -> DeliveryResult | None:
        """Return a configuration failure, or ``None`` when a send can be attempted.

        Configuration problems are reported as non-retryable results rather than raised, so
        a missing SMTP host silences email without preventing the GitHub or Google Chat
        channels from delivering (Rule 12: record the failure, do not abort the event).
        """
        if not self._settings.host:
            return DeliveryResult.failure(
                f"{SMTP_HOST_ENV} is not configured",
                retryable=False,
            )
        if not recipients:
            return DeliveryResult.failure(
                f"no email recipients configured ({EMAIL_TO_ENV})",
                retryable=False,
            )
        return None

    def _recipients_for(self, message: NotificationMessage) -> tuple[str, ...]:
        """Return per-message recipients, falling back to the configured list.

        The per-repository override travels in ``metadata["email_recipients"]`` so routing
        can add recipients without the adapter knowing about repository configuration.
        """
        override = message.metadata.get("email_recipients", "")
        if override:
            return tuple(address.strip() for address in override.split(",") if address.strip())
        return self._recipients

    def _send_blocking(self, message: EmailMessage, recipients: Sequence[str]) -> None:
        """Send ``message`` synchronously; runs inside ``asyncio.to_thread``."""
        sender = self._settings.sender
        if self._settings.port == _IMPLICIT_TLS_PORT:
            context = ssl.create_default_context()
            with smtplib.SMTP_SSL(
                self._settings.host,
                self._settings.port,
                timeout=self._settings.timeout_seconds,
                context=context,
            ) as smtp:
                self._authenticate(smtp)
                smtp.send_message(message, from_addr=sender, to_addrs=list(recipients))
            return

        with smtplib.SMTP(
            self._settings.host,
            self._settings.port,
            timeout=self._settings.timeout_seconds,
        ) as smtp:
            if self._use_starttls:
                smtp.ehlo()
                if smtp.has_extn("starttls"):
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
            self._authenticate(smtp)
            smtp.send_message(message, from_addr=sender, to_addrs=list(recipients))

    def _authenticate(self, smtp: smtplib.SMTP) -> None:
        """Authenticate when credentials are configured.

        The password is passed straight to ``login`` and never logged or attached to an
        error message.
        """
        if self._settings.username:
            smtp.login(self._settings.username, self._settings.password)
