"""Provider HTTP error mapping, via ``respx``. No test here touches the network."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx
import pytest
import respx

from scout_llm.errors import (
    LLMAuthenticationError,
    LLMContextTooLargeError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMResponseFormatError,
    LLMTimeoutError,
    LLMTransientError,
)
from scout_llm.providers.anthropic import AnthropicProvider
from scout_llm.providers.base import LLMProvider, parse_retry_after
from scout_llm.providers.openai import OpenAIProvider
from scout_llm.redaction import REDACTED
from scout_llm.types import CompletionRequest, Message

API_KEY = "sk-test-abcdef1234567890"

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

URLS = {"openai": OPENAI_URL, "anthropic": ANTHROPIC_URL}


def build_provider(name: str, client: httpx.AsyncClient) -> LLMProvider:
    """Construct a real provider pointed at an injected (mocked) client."""
    if name == "openai":
        return OpenAIProvider(api_key=API_KEY, client=client)
    return AnthropicProvider(api_key=API_KEY, client=client)


def request_fixture() -> CompletionRequest:
    return CompletionRequest(messages=[Message(role="user", content="hello")], max_output_tokens=64)


@pytest.fixture(params=sorted(URLS))
def provider_name(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@pytest.mark.parametrize("status", [401, 403])
@respx.mock
async def test_credential_rejections_map_to_authentication_errors(
    provider_name: str,
    status: int,
) -> None:
    respx.post(URLS[provider_name]).mock(return_value=httpx.Response(status, json={"error": "no"}))
    async with httpx.AsyncClient() as client:
        provider = build_provider(provider_name, client)

        with pytest.raises(LLMAuthenticationError) as excinfo:
            await provider.complete(request_fixture(), model="m")

    assert excinfo.value.failure_class.value == "non_retryable"
    assert excinfo.value.context["status_code"] == status


@pytest.mark.parametrize("status", [400, 404, 422])
@respx.mock
async def test_client_errors_map_to_invalid_request(provider_name: str, status: int) -> None:
    respx.post(URLS[provider_name]).mock(return_value=httpx.Response(status, json={"error": "no"}))
    async with httpx.AsyncClient() as client:
        provider = build_provider(provider_name, client)

        with pytest.raises(LLMInvalidRequestError) as excinfo:
            await provider.complete(request_fixture(), model="m")

    assert excinfo.value.context["status_code"] == status


@respx.mock
async def test_rate_limit_parses_retry_after_seconds(provider_name: str) -> None:
    respx.post(URLS[provider_name]).mock(
        return_value=httpx.Response(429, headers={"retry-after": "7"}, json={"error": "slow"})
    )
    async with httpx.AsyncClient() as client:
        provider = build_provider(provider_name, client)

        with pytest.raises(LLMRateLimitError) as excinfo:
            await provider.complete(request_fixture(), model="m")

    assert excinfo.value.retry_after_seconds == pytest.approx(7.0)
    assert excinfo.value.failure_class.value == "retryable"


@respx.mock
async def test_rate_limit_without_a_header_reports_no_retry_after(provider_name: str) -> None:
    respx.post(URLS[provider_name]).mock(return_value=httpx.Response(429, json={}))
    async with httpx.AsyncClient() as client:
        provider = build_provider(provider_name, client)

        with pytest.raises(LLMRateLimitError) as excinfo:
            await provider.complete(request_fixture(), model="m")

    assert excinfo.value.retry_after_seconds is None


@pytest.mark.parametrize("status", [500, 502, 503])
@respx.mock
async def test_server_errors_map_to_transient(provider_name: str, status: int) -> None:
    respx.post(URLS[provider_name]).mock(
        return_value=httpx.Response(status, json={"error": "oops"})
    )
    async with httpx.AsyncClient() as client:
        provider = build_provider(provider_name, client)

        with pytest.raises(LLMTransientError) as excinfo:
            await provider.complete(request_fixture(), model="m")

    assert excinfo.value.failure_class.value == "retryable"


@respx.mock
async def test_a_read_timeout_maps_to_a_timeout_error(provider_name: str) -> None:
    respx.post(URLS[provider_name]).mock(side_effect=httpx.ReadTimeout("too slow"))
    async with httpx.AsyncClient() as client:
        provider = build_provider(provider_name, client)

        with pytest.raises(LLMTimeoutError):
            await provider.complete(request_fixture(), model="m")


@respx.mock
async def test_a_connect_error_maps_to_a_transient_error(provider_name: str) -> None:
    respx.post(URLS[provider_name]).mock(side_effect=httpx.ConnectError("no route"))
    async with httpx.AsyncClient() as client:
        provider = build_provider(provider_name, client)

        with pytest.raises(LLMTransientError):
            await provider.complete(request_fixture(), model="m")


@respx.mock
async def test_a_successful_status_with_a_non_json_body_is_a_format_error(
    provider_name: str,
) -> None:
    respx.post(URLS[provider_name]).mock(
        return_value=httpx.Response(200, text="<html>gateway error</html>")
    )
    async with httpx.AsyncClient() as client:
        provider = build_provider(provider_name, client)

        with pytest.raises(LLMResponseFormatError):
            await provider.complete(request_fixture(), model="m")


@respx.mock
async def test_an_error_body_that_echoes_the_api_key_is_scrubbed(provider_name: str) -> None:
    body = {"error": {"message": f"invalid api key {API_KEY}"}}
    respx.post(URLS[provider_name]).mock(return_value=httpx.Response(401, json=body))
    async with httpx.AsyncClient() as client:
        provider = build_provider(provider_name, client)

        with pytest.raises(LLMAuthenticationError) as excinfo:
            await provider.complete(request_fixture(), model="m")

    rendered = str(excinfo.value)
    assert API_KEY not in rendered
    assert REDACTED in rendered


@respx.mock
async def test_openai_context_overflow_is_reported_as_context_too_large() -> None:
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(
            400,
            json={"error": {"code": "context_length_exceeded", "message": "too long"}},
        )
    )
    async with httpx.AsyncClient() as client:
        provider = OpenAIProvider(api_key=API_KEY, client=client)

        with pytest.raises(LLMContextTooLargeError) as excinfo:
            await provider.complete(request_fixture(), model="m")

    assert excinfo.value.input_tokens == 0  # unknown, so recorded as unknown rather than guessed
    assert excinfo.value.max_input_tokens == 0


@respx.mock
async def test_anthropic_context_overflow_is_reported_as_context_too_large() -> None:
    respx.post(ANTHROPIC_URL).mock(
        return_value=httpx.Response(
            400,
            json={
                "type": "error",
                "error": {
                    "type": "invalid_request_error",
                    "message": "prompt is too long: 250000 tokens > 200000 maximum",
                },
            },
        )
    )
    async with httpx.AsyncClient() as client:
        provider = AnthropicProvider(api_key=API_KEY, client=client)

        with pytest.raises(LLMContextTooLargeError):
            await provider.complete(request_fixture(), model="m")


@respx.mock
async def test_a_plain_openai_400_stays_an_invalid_request() -> None:
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(400, json={"error": {"code": "bad_model"}})
    )
    async with httpx.AsyncClient() as client:
        provider = OpenAIProvider(api_key=API_KEY, client=client)

        with pytest.raises(LLMInvalidRequestError):
            await provider.complete(request_fixture(), model="m")


def test_parse_retry_after_accepts_seconds() -> None:
    assert parse_retry_after("7") == pytest.approx(7.0)
    assert parse_retry_after("0.5") == pytest.approx(0.5)
    assert parse_retry_after("-3") == 0.0


def test_parse_retry_after_accepts_an_http_date() -> None:
    when = datetime.now(UTC) + timedelta(seconds=30)
    parsed = parse_retry_after(format_datetime(when, usegmt=True))

    assert parsed is not None
    assert 20.0 < parsed <= 31.0


def test_parse_retry_after_rejects_unusable_values() -> None:
    assert parse_retry_after(None) is None
    assert parse_retry_after("") is None
    assert parse_retry_after("   ") is None
    assert parse_retry_after("soon") is None


async def test_both_real_providers_satisfy_the_provider_protocol() -> None:
    """A cheap runtime check that this module's providers are substitutable."""
    openai_provider = OpenAIProvider(api_key=API_KEY)
    anthropic_provider = AnthropicProvider(api_key=API_KEY)
    try:
        assert isinstance(openai_provider, LLMProvider)
        assert isinstance(anthropic_provider, LLMProvider)
    finally:
        await openai_provider.aclose()
        await anthropic_provider.aclose()
