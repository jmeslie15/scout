"""Google Chat webhook adapter behaviour (spec section 47).

The webhook URL embeds a key, so these tests also assert the secret-handling property: a
transport or provider failure must be reported without echoing the URL or the exception
text that contains it.
"""

from __future__ import annotations

import json

import httpx
import pytest

from scout_domain.enums import NotificationChannel, Severity
from scout_notifications.base import NotificationMessage
from scout_notifications.channels.google_chat import (
    GOOGLE_CHAT_WEBHOOK_ENV,
    GoogleChatChannelAdapter,
)

#: Deliberately includes key/token parameters so a leak would be observable.
WEBHOOK_URL = "https://chat.googleapis.com/v1/spaces/AAA/messages?key=SECRET-KEY&token=SECRET-TOKEN"


@pytest.fixture
def message() -> NotificationMessage:
    return NotificationMessage(
        subject="Question: export metadata",
        body="Should managers see export metadata?",
        severity=Severity.HIGH,
        job_url="https://scout.test/jobs/job_1",
    )


async def _send(adapter: GoogleChatChannelAdapter, message: NotificationMessage):
    try:
        return await adapter.send(message)
    finally:
        await adapter.aclose()


async def test_successful_delivery_posts_a_text_payload(
    respx_mock,
    message: NotificationMessage,
) -> None:
    route = respx_mock.post(WEBHOOK_URL).mock(
        return_value=httpx.Response(200, json={"name": "spaces/AAA/messages/BBB"})
    )
    adapter = GoogleChatChannelAdapter(webhook_url=WEBHOOK_URL)

    result = await _send(adapter, message)

    assert result.delivered is True
    assert result.error is None
    assert result.provider_message_id == "spaces/AAA/messages/BBB"

    payload = json.loads(route.calls[0].request.content)
    assert payload["text"].startswith("*Question: export metadata*")
    assert "Should managers see export metadata?" in payload["text"]


async def test_success_without_a_json_body_still_delivers(
    respx_mock,
    message: NotificationMessage,
) -> None:
    respx_mock.post(WEBHOOK_URL).mock(return_value=httpx.Response(200, text="ok"))
    adapter = GoogleChatChannelAdapter(webhook_url=WEBHOOK_URL)

    result = await _send(adapter, message)

    assert result.delivered is True
    assert result.provider_message_id is None


@pytest.mark.parametrize("status", [429, 500, 502, 503])
async def test_retryable_failures(
    respx_mock,
    message: NotificationMessage,
    status: int,
) -> None:
    respx_mock.post(WEBHOOK_URL).mock(return_value=httpx.Response(status))
    adapter = GoogleChatChannelAdapter(webhook_url=WEBHOOK_URL)

    result = await _send(adapter, message)

    assert result.delivered is False
    assert result.retryable is True
    assert result.error is not None
    assert str(status) in result.error


@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_non_retryable_client_failures(
    respx_mock,
    message: NotificationMessage,
    status: int,
) -> None:
    respx_mock.post(WEBHOOK_URL).mock(return_value=httpx.Response(status))
    adapter = GoogleChatChannelAdapter(webhook_url=WEBHOOK_URL)

    result = await _send(adapter, message)

    assert result.delivered is False
    assert result.retryable is False
    assert result.error is not None
    assert str(status) in result.error


async def test_timeout_is_retryable(respx_mock, message: NotificationMessage) -> None:
    respx_mock.post(WEBHOOK_URL).mock(side_effect=httpx.ReadTimeout("timed out"))
    adapter = GoogleChatChannelAdapter(webhook_url=WEBHOOK_URL)

    result = await _send(adapter, message)

    assert result.delivered is False
    assert result.retryable is True
    assert result.error == "google chat request timed out"


async def test_transport_error_never_echoes_the_webhook_url(
    respx_mock,
    message: NotificationMessage,
) -> None:
    respx_mock.post(WEBHOOK_URL).mock(
        side_effect=httpx.ConnectError(f"failed to connect to {WEBHOOK_URL}")
    )
    adapter = GoogleChatChannelAdapter(webhook_url=WEBHOOK_URL)

    result = await _send(adapter, message)

    assert result.delivered is False
    assert result.retryable is True
    assert result.error is not None
    assert "SECRET-KEY" not in result.error
    assert "SECRET-TOKEN" not in result.error
    assert "chat.googleapis.com" not in result.error


async def test_http_failure_never_echoes_the_webhook_url(
    respx_mock,
    message: NotificationMessage,
) -> None:
    respx_mock.post(WEBHOOK_URL).mock(return_value=httpx.Response(500, text=WEBHOOK_URL))
    adapter = GoogleChatChannelAdapter(webhook_url=WEBHOOK_URL)

    result = await _send(adapter, message)

    assert result.error is not None
    assert "SECRET-KEY" not in result.error
    assert "SECRET-TOKEN" not in result.error


async def test_unconfigured_webhook_fails_without_a_request(
    respx_mock,
    message: NotificationMessage,
) -> None:
    adapter = GoogleChatChannelAdapter(webhook_url="")

    result = await adapter.send(message)

    assert result.delivered is False
    assert result.retryable is False
    assert result.error is not None
    assert GOOGLE_CHAT_WEBHOOK_ENV in result.error
    assert len(respx_mock.calls) == 0
    assert adapter.configured is False


async def test_webhook_url_is_read_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
    respx_mock,
    message: NotificationMessage,
) -> None:
    monkeypatch.setenv(GOOGLE_CHAT_WEBHOOK_ENV, WEBHOOK_URL)
    route = respx_mock.post(WEBHOOK_URL).mock(return_value=httpx.Response(200, json={}))
    adapter = GoogleChatChannelAdapter()

    result = await _send(adapter, message)

    assert result.delivered is True
    assert route.called
    assert adapter.configured is True


def test_supports_only_its_own_channel() -> None:
    adapter = GoogleChatChannelAdapter(webhook_url=WEBHOOK_URL)

    assert adapter.name is NotificationChannel.GOOGLE_CHAT
    assert adapter.supports(NotificationChannel.GOOGLE_CHAT) is True
    assert adapter.supports(NotificationChannel.EMAIL) is False


async def test_injected_client_is_not_closed_by_the_adapter(
    respx_mock,
    message: NotificationMessage,
) -> None:
    respx_mock.post(WEBHOOK_URL).mock(return_value=httpx.Response(200, json={}))
    client = httpx.AsyncClient()
    adapter = GoogleChatChannelAdapter(webhook_url=WEBHOOK_URL, client=client)

    await adapter.send(message)
    await adapter.aclose()

    assert client.is_closed is False
    await client.aclose()
