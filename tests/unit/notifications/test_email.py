"""SMTP adapter behaviour: message shape, TLS, failure classification, secret hygiene."""

from __future__ import annotations

import email
import email.policy
import smtplib
from dataclasses import replace
from email.message import EmailMessage
from typing import ClassVar

import pytest

from scout_domain.enums import NotificationChannel, Severity
from scout_domain.errors import ConfigurationError
from scout_notifications.base import NotificationMessage
from scout_notifications.channels.email import (
    EMAIL_FROM_ENV,
    EMAIL_TO_ENV,
    SMTP_HOST_ENV,
    SMTP_PASSWORD_ENV,
    SMTP_PORT_ENV,
    SMTP_USERNAME_ENV,
    EmailChannelAdapter,
    SmtpSettings,
    build_email_message,
)

PASSWORD = "hunter2-do-not-log"


class FakeSMTP:
    """A recording stand-in for ``smtplib.SMTP``/``SMTP_SSL``."""

    instances: ClassVar[list[FakeSMTP]] = []
    starttls_available: bool = True
    send_error: Exception | None = None
    login_error: Exception | None = None
    connect_error: Exception | None = None

    def __init__(self, host: str, port: int, timeout: float | None = None, **kwargs) -> None:
        if FakeSMTP.connect_error is not None:
            raise FakeSMTP.connect_error
        self.host = host
        self.port = port
        self.timeout = timeout
        self.kwargs = kwargs
        self.ehlo_calls = 0
        self.starttls_calls = 0
        self.login_call: tuple[str, str] | None = None
        self.sent: tuple[EmailMessage, str | None, list[str]] | None = None
        FakeSMTP.instances.append(self)

    def __enter__(self) -> FakeSMTP:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def ehlo(self) -> None:
        self.ehlo_calls += 1

    def has_extn(self, name: str) -> bool:
        return FakeSMTP.starttls_available and name == "starttls"

    def starttls(self, context: object | None = None) -> None:
        self.starttls_calls += 1
        self.tls_context = context

    def login(self, username: str, password: str) -> None:
        self.login_call = (username, password)
        if FakeSMTP.login_error is not None:
            raise FakeSMTP.login_error

    def send_message(
        self,
        message: EmailMessage,
        from_addr: str | None = None,
        to_addrs: list[str] | None = None,
    ) -> None:
        self.sent = (message, from_addr, list(to_addrs or []))
        if FakeSMTP.send_error is not None:
            raise FakeSMTP.send_error


@pytest.fixture(autouse=True)
def reset_fake_smtp() -> None:
    FakeSMTP.instances = []
    FakeSMTP.starttls_available = True
    FakeSMTP.send_error = None
    FakeSMTP.login_error = None
    FakeSMTP.connect_error = None


@pytest.fixture
def message() -> NotificationMessage:
    return NotificationMessage(
        subject="Question: export metadata",
        body="Should managers see export metadata?",
        severity=Severity.HIGH,
        job_url="https://scout.test/jobs/job_1",
    )


@pytest.fixture
def settings() -> SmtpSettings:
    return SmtpSettings(
        host="smtp.test",
        port=587,
        username="scout",
        password=PASSWORD,
        sender="scout@acme.test",
        recipients=("oncall@acme.test",),
    )


@pytest.fixture
def patched_smtp(monkeypatch: pytest.MonkeyPatch) -> type[FakeSMTP]:
    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
    return FakeSMTP


def test_build_email_message_is_multipart_alternative(message: NotificationMessage) -> None:
    raw = build_email_message(
        message,
        sender="scout@acme.test",
        recipients=["oncall@acme.test"],
        html_body="<p>Hello</p>",
    )

    parsed = email.message_from_string(raw.as_string(), policy=email.policy.default)

    assert parsed.is_multipart()
    subtypes = {part.get_content_subtype() for part in parsed.iter_parts()}
    assert subtypes == {"plain", "html"}
    assert parsed["Subject"] == "Question: export metadata"
    assert parsed["To"] == "oncall@acme.test"
    assert parsed["X-Scout-Severity"] == "HIGH"
    assert parsed["X-Scout-Job-Url"] == "https://scout.test/jobs/job_1"


def test_build_email_message_sanitises_header_injection(message: NotificationMessage) -> None:
    injected = message.evolve(subject="Hello\nBcc: attacker@example.com")

    raw = build_email_message(injected, sender="scout@acme.test", recipients=["a@b.test"])
    parsed = email.message_from_string(raw.as_string(), policy=email.policy.default)

    assert parsed["Bcc"] is None
    assert "\n" not in str(parsed["Subject"])


def test_build_email_message_escapes_plain_text_without_an_html_part() -> None:
    message = NotificationMessage(
        subject="Subject",
        body="<script>alert('x')</script>",
        severity=Severity.LOW,
    )

    raw = build_email_message(message, sender="s@t.test", recipients=["a@b.test"])
    parsed = email.message_from_string(raw.as_string(), policy=email.policy.default)
    html_part = parsed.get_payload(1).get_payload(decode=True).decode()

    assert "<script>" not in html_part
    assert "&lt;script&gt;" in html_part


async def test_send_uses_starttls_and_authenticates(
    patched_smtp: type[FakeSMTP],
    message: NotificationMessage,
    settings: SmtpSettings,
) -> None:
    adapter = EmailChannelAdapter(settings=settings)

    result = await adapter.send(message)

    assert result.delivered is True
    connection = patched_smtp.instances[0]
    assert connection.host == "smtp.test"
    assert connection.port == 587
    assert connection.starttls_calls == 1
    assert connection.login_call == ("scout", PASSWORD)
    assert connection.sent is not None
    assert connection.sent[2] == ["oncall@acme.test"]


async def test_send_skips_starttls_when_the_server_does_not_advertise_it(
    patched_smtp: type[FakeSMTP],
    message: NotificationMessage,
    settings: SmtpSettings,
) -> None:
    patched_smtp.starttls_available = False
    adapter = EmailChannelAdapter(settings=settings)

    result = await adapter.send(message)

    assert result.delivered is True
    assert patched_smtp.instances[0].starttls_calls == 0


async def test_send_uses_implicit_tls_on_port_465(
    monkeypatch: pytest.MonkeyPatch,
    patched_smtp: type[FakeSMTP],
    message: NotificationMessage,
) -> None:
    ssl_connections: list[FakeSMTP] = []

    class RecordingSSL(FakeSMTP):
        def __init__(self, host: str, port: int, timeout: float | None = None, **kwargs) -> None:
            super().__init__(host, port, timeout, **kwargs)
            ssl_connections.append(self)

    monkeypatch.setattr(smtplib, "SMTP_SSL", RecordingSSL)
    settings = SmtpSettings(
        host="smtp.test",
        port=465,
        username="scout",
        password=PASSWORD,
        recipients=("oncall@acme.test",),
    )

    result = await EmailChannelAdapter(settings=settings).send(message)

    assert result.delivered is True
    assert len(ssl_connections) == 1
    assert ssl_connections[0].starttls_calls == 0
    assert ssl_connections[0].login_call == ("scout", PASSWORD)


async def test_unconfigured_host_fails_non_retryably(message: NotificationMessage) -> None:
    adapter = EmailChannelAdapter(settings=SmtpSettings())

    result = await adapter.send(message)

    assert result.delivered is False
    assert result.retryable is False
    assert result.error is not None
    assert SMTP_HOST_ENV in result.error


async def test_no_recipients_fails_non_retryably(
    message: NotificationMessage,
    settings: SmtpSettings,
) -> None:
    adapter = EmailChannelAdapter(settings=replace(settings, recipients=()))

    result = await adapter.send(message)

    assert result.delivered is False
    assert result.retryable is False
    assert result.error is not None
    assert EMAIL_TO_ENV in result.error


async def test_transient_smtp_code_is_retryable(
    patched_smtp: type[FakeSMTP],
    message: NotificationMessage,
    settings: SmtpSettings,
) -> None:
    patched_smtp.send_error = smtplib.SMTPResponseException(451, "try again later")

    result = await EmailChannelAdapter(settings=settings).send(message)

    assert result.delivered is False
    assert result.retryable is True
    assert result.error is not None
    assert "451" in result.error


async def test_permanent_smtp_code_is_not_retryable(
    patched_smtp: type[FakeSMTP],
    message: NotificationMessage,
    settings: SmtpSettings,
) -> None:
    patched_smtp.send_error = smtplib.SMTPResponseException(550, "mailbox unavailable")

    result = await EmailChannelAdapter(settings=settings).send(message)

    assert result.delivered is False
    assert result.retryable is False


async def test_connection_failure_is_retryable(
    patched_smtp: type[FakeSMTP],
    message: NotificationMessage,
    settings: SmtpSettings,
) -> None:
    patched_smtp.connect_error = ConnectionRefusedError("connection refused")

    result = await EmailChannelAdapter(settings=settings).send(message)

    assert result.delivered is False
    assert result.retryable is True


async def test_authentication_failure_never_leaks_the_password(
    patched_smtp: type[FakeSMTP],
    message: NotificationMessage,
    settings: SmtpSettings,
) -> None:
    patched_smtp.login_error = smtplib.SMTPAuthenticationError(535, b"authentication failed")

    result = await EmailChannelAdapter(settings=settings).send(message)

    assert result.delivered is False
    assert result.retryable is False
    assert result.error is not None
    assert PASSWORD not in result.error


async def test_metadata_can_override_recipients(
    patched_smtp: type[FakeSMTP],
    message: NotificationMessage,
    settings: SmtpSettings,
) -> None:
    overridden = message.evolve(metadata={"email_recipients": "oncall@acme.test, lead@acme.test"})

    result = await EmailChannelAdapter(settings=settings).send(overridden)

    assert result.delivered is True
    assert patched_smtp.instances[0].sent is not None
    assert patched_smtp.instances[0].sent[2] == ["oncall@acme.test", "lead@acme.test"]


async def test_html_body_metadata_is_used(
    patched_smtp: type[FakeSMTP],
    message: NotificationMessage,
    settings: SmtpSettings,
) -> None:
    with_html = message.evolve(metadata={"html_body": "<p>Rich body</p>"})

    await EmailChannelAdapter(settings=settings).send(with_html)

    sent = patched_smtp.instances[0].sent
    assert sent is not None
    parsed = email.message_from_string(sent[0].as_string(), policy=email.policy.default)
    html_part = parsed.get_payload(1).get_payload(decode=True).decode()
    assert "<p>Rich body</p>" in html_part


def test_settings_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SMTP_HOST_ENV, "smtp.acme.test")
    monkeypatch.setenv(SMTP_PORT_ENV, "2525")
    monkeypatch.setenv(SMTP_USERNAME_ENV, "scout")
    monkeypatch.setenv(SMTP_PASSWORD_ENV, PASSWORD)
    monkeypatch.setenv(EMAIL_FROM_ENV, "scout@acme.test")
    monkeypatch.setenv(EMAIL_TO_ENV, "a@acme.test, b@acme.test ,")

    settings = SmtpSettings.from_environment()

    assert settings.host == "smtp.acme.test"
    assert settings.port == 2525
    assert settings.username == "scout"
    assert settings.password == PASSWORD
    assert settings.sender == "scout@acme.test"
    assert settings.recipients == ("a@acme.test", "b@acme.test")
    assert settings.configured is True


def test_settings_from_environment_rejects_a_bad_port(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SMTP_PORT_ENV, "not-a-port")

    with pytest.raises(ConfigurationError, match="must be an integer"):
        SmtpSettings.from_environment()


def test_supports_only_its_own_channel() -> None:
    adapter = EmailChannelAdapter(settings=SmtpSettings())

    assert adapter.name is NotificationChannel.EMAIL
    assert adapter.supports(NotificationChannel.EMAIL) is True
    assert adapter.supports(NotificationChannel.GITHUB) is False
