"""Provider response parsing and credential scrubbing of retained payloads."""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from scout_llm.errors import LLMAuthenticationError, LLMInvalidRequestError, LLMResponseFormatError
from scout_llm.providers.anthropic import (
    ANTHROPIC_VERSION,
    DEFAULT_MAX_OUTPUT_TOKENS,
    AnthropicProvider,
)
from scout_llm.providers.openai import OpenAIProvider
from scout_llm.redaction import REDACTED
from scout_llm.types import CompletionRequest, Message

API_KEY = "sk-test-abcdef1234567890"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

OPENAI_BODY = {
    "id": "chatcmpl-1",
    "object": "chat.completion",
    "model": "gpt-4o",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "The change looks correct."},
            "finish_reason": "stop",
        }
    ],
    "usage": {
        "prompt_tokens": 120,
        "completion_tokens": 30,
        "total_tokens": 150,
        "prompt_tokens_details": {"cached_tokens": 40},
    },
}

ANTHROPIC_BODY = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-3-5-sonnet-20241022",
    "content": [
        {"type": "text", "text": "The change looks "},
        {"type": "text", "text": "correct."},
    ],
    "stop_reason": "end_turn",
    "usage": {
        "input_tokens": 100,
        "output_tokens": 25,
        "cache_read_input_tokens": 40,
        "cache_creation_input_tokens": 10,
    },
}


def request_fixture() -> CompletionRequest:
    return CompletionRequest(
        messages=[
            Message(role="system", content="You are Scout."),
            Message(role="user", content="Review this."),
        ],
        max_output_tokens=256,
        temperature=0.2,
    )


@respx.mock
async def test_openai_response_is_mapped_onto_the_transport_agnostic_completion() -> None:
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=OPENAI_BODY))
    async with httpx.AsyncClient() as client:
        provider = OpenAIProvider(api_key=API_KEY, client=client)

        completion = await provider.complete(request_fixture(), model="gpt-4o")

    assert completion.text == "The change looks correct."
    assert completion.provider == "openai"
    assert completion.model == "gpt-4o"
    assert completion.finish_reason == "stop"
    assert completion.usage.input_tokens == 120  # prompt_tokens already includes cached tokens
    assert completion.usage.output_tokens == 30
    assert completion.usage.cache_read_tokens == 40
    assert completion.usage.total_tokens == 150
    assert completion.latency_seconds >= 0.0


@respx.mock
async def test_openai_raw_payload_keeps_the_body_but_scrubs_the_bearer_token() -> None:
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=OPENAI_BODY))
    async with httpx.AsyncClient() as client:
        provider = OpenAIProvider(api_key=API_KEY, client=client)

        completion = await provider.complete(request_fixture(), model="gpt-4o")

    assert completion.raw["request_headers"]["authorization"] == REDACTED
    assert completion.raw["status_code"] == 200
    assert completion.raw["response"]["id"] == "chatcmpl-1"
    assert API_KEY not in json.dumps(completion.raw)


@respx.mock
async def test_openai_response_without_choices_is_a_format_error() -> None:
    respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(200, json={"id": "x", "usage": {"prompt_tokens": 1}})
    )
    async with httpx.AsyncClient() as client:
        provider = OpenAIProvider(api_key=API_KEY, client=client)

        with pytest.raises(LLMResponseFormatError) as excinfo:
            await provider.complete(request_fixture(), model="gpt-4o")

    assert excinfo.value.context["provider"] == "openai"


@respx.mock
async def test_openai_response_with_a_null_message_content_is_a_format_error() -> None:
    body = {
        "choices": [{"message": {"role": "assistant", "content": None}, "finish_reason": "stop"}]
    }
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=body))
    async with httpx.AsyncClient() as client:
        provider = OpenAIProvider(api_key=API_KEY, client=client)

        with pytest.raises(LLMResponseFormatError, match="no text content"):
            await provider.complete(request_fixture(), model="gpt-4o")


@respx.mock
async def test_openai_tolerates_a_missing_usage_block() -> None:
    body = {
        "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]
    }
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=body))
    async with httpx.AsyncClient() as client:
        provider = OpenAIProvider(api_key=API_KEY, client=client)

        completion = await provider.complete(request_fixture(), model="gpt-4o")

    assert completion.usage.total_tokens == 0  # unknown, not fabricated


@respx.mock
async def test_anthropic_response_concatenates_text_blocks_and_folds_cache_tokens() -> None:
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json=ANTHROPIC_BODY))
    async with httpx.AsyncClient() as client:
        provider = AnthropicProvider(api_key=API_KEY, client=client)

        completion = await provider.complete(request_fixture(), model="claude-3-5-sonnet-20241022")

    assert completion.text == "The change looks correct."
    assert completion.provider == "anthropic"
    assert completion.finish_reason == "end_turn"
    # Normalisation rule: input_tokens is the total prompt token count, cache included.
    assert completion.usage.input_tokens == 100 + 40 + 10
    assert completion.usage.cache_read_tokens == 40
    assert completion.usage.cache_write_tokens == 10
    assert completion.usage.output_tokens == 25
    assert completion.usage.total_tokens == 150 + 25


@respx.mock
async def test_anthropic_raw_payload_scrubs_the_api_key_header() -> None:
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json=ANTHROPIC_BODY))
    async with httpx.AsyncClient() as client:
        provider = AnthropicProvider(api_key=API_KEY, client=client)

        completion = await provider.complete(request_fixture(), model="claude-3-5-sonnet-20241022")

    assert completion.raw["request_headers"]["x-api-key"] == REDACTED
    assert completion.raw["request_headers"]["anthropic-version"] == ANTHROPIC_VERSION
    assert API_KEY not in json.dumps(completion.raw)


@respx.mock
async def test_anthropic_response_with_only_tool_use_blocks_is_a_format_error() -> None:
    body = {"content": [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]}
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json=body))
    async with httpx.AsyncClient() as client:
        provider = AnthropicProvider(api_key=API_KEY, client=client)

        with pytest.raises(LLMResponseFormatError) as excinfo:
            await provider.complete(request_fixture(), model="m")

    assert excinfo.value.context["block_types"] == ["tool_use"]


@respx.mock
async def test_anthropic_response_without_content_is_a_format_error() -> None:
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json={"id": "msg"}))
    async with httpx.AsyncClient() as client:
        provider = AnthropicProvider(api_key=API_KEY, client=client)

        with pytest.raises(LLMResponseFormatError, match="no content blocks"):
            await provider.complete(request_fixture(), model="m")


@respx.mock
async def test_anthropic_rejects_a_request_with_only_system_messages() -> None:
    route = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json=ANTHROPIC_BODY))
    async with httpx.AsyncClient() as client:
        provider = AnthropicProvider(api_key=API_KEY, client=client)
        request = CompletionRequest(messages=[Message(role="system", content="only system")])

        with pytest.raises(LLMInvalidRequestError, match="at least one non-system message"):
            await provider.complete(request, model="m")

    assert route.call_count == 0  # rejected locally, before spending a request


@respx.mock
async def test_anthropic_sends_its_default_max_tokens_when_the_caller_omits_one() -> None:
    route = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json=ANTHROPIC_BODY))
    async with httpx.AsyncClient() as client:
        provider = AnthropicProvider(api_key=API_KEY, client=client)
        request = CompletionRequest(messages=[Message(role="user", content="hi")])

        await provider.complete(request, model="m")

    sent = json.loads(route.calls[0].request.content)
    assert sent["max_tokens"] == DEFAULT_MAX_OUTPUT_TOKENS


def test_constructing_a_real_provider_without_a_key_raises_immediately() -> None:
    with pytest.raises(LLMAuthenticationError):
        OpenAIProvider(api_key="")
    with pytest.raises(LLMAuthenticationError):
        AnthropicProvider(api_key="")


async def test_real_providers_read_their_keys_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCOUT_OPENAI_API_KEY", API_KEY)
    monkeypatch.setenv("SCOUT_ANTHROPIC_API_KEY", API_KEY)

    openai_provider = OpenAIProvider()
    anthropic_provider = AnthropicProvider()
    try:
        assert openai_provider.default_headers()["authorization"] == f"Bearer {API_KEY}"
        assert anthropic_provider.default_headers()["x-api-key"] == API_KEY
    finally:
        await openai_provider.aclose()
        await anthropic_provider.aclose()


async def test_an_owned_client_is_closed_by_aclose() -> None:
    provider = OpenAIProvider(api_key=API_KEY)
    await provider.aclose()
    assert provider.client.is_closed


async def test_an_injected_client_is_left_open_for_its_owner() -> None:
    """The caller owns an injected client; closing it would break the caller's fixture."""
    client = httpx.AsyncClient()
    try:
        provider = OpenAIProvider(api_key=API_KEY, client=client)
        await provider.aclose()
        assert not client.is_closed
    finally:
        await client.aclose()
