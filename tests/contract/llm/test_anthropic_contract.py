"""Anthropic Messages wire contract.

Two Anthropic-specific facts are pinned here because getting either wrong breaks every call:
the system prompt is a **top-level field** (never a ``role="system"`` message), and
``max_tokens`` is **required** with no server-side default.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from scout_llm.providers.anthropic import (
    ANTHROPIC_VERSION,
    DEFAULT_MAX_OUTPUT_TOKENS,
    AnthropicProvider,
)
from scout_llm.types import CompletionRequest, Message

pytestmark = pytest.mark.contract

API_KEY = "sk-test-abcdef1234567890"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"

RESPONSE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "content": [{"type": "text", "text": "ok"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 5, "output_tokens": 6},
}


def last_request(route: respx.Route) -> tuple[dict[str, object], httpx.Request]:
    request = route.calls[-1].request
    return json.loads(request.content), request


@respx.mock
async def test_full_request_body_matches_the_documented_schema() -> None:
    route = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json=RESPONSE))
    async with httpx.AsyncClient() as client:
        provider = AnthropicProvider(api_key=API_KEY, client=client)
        request = CompletionRequest(
            messages=[
                Message(role="system", content="You are Scout."),
                Message(role="user", content="Review this."),
            ],
            max_output_tokens=512,
            temperature=0.5,
            stop=["\n\n"],
            metadata={"job_id": "job_1"},
        )

        await provider.complete(request, model="claude-3-5-sonnet-20241022")

    body, http_request = last_request(route)

    assert body == {
        "model": "claude-3-5-sonnet-20241022",
        "max_tokens": 512,
        "system": "You are Scout.",
        "messages": [{"role": "user", "content": "Review this."}],
        "temperature": 0.5,
        "stop_sequences": ["\n\n"],
    }
    assert str(http_request.url) == ANTHROPIC_URL
    assert http_request.method == "POST"
    assert http_request.headers["x-api-key"] == API_KEY
    assert http_request.headers["anthropic-version"] == ANTHROPIC_VERSION
    assert http_request.headers["content-type"] == "application/json"


@respx.mock
async def test_multiple_system_messages_are_joined_into_the_top_level_field() -> None:
    route = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json=RESPONSE))
    async with httpx.AsyncClient() as client:
        provider = AnthropicProvider(api_key=API_KEY, client=client)
        request = CompletionRequest(
            messages=[
                Message(role="system", content="First."),
                Message(role="system", content="Second."),
                Message(role="user", content="Go."),
            ]
        )

        await provider.complete(request, model="m")

    body, _ = last_request(route)

    assert body["system"] == "First.\n\nSecond."
    assert body["messages"] == [{"role": "user", "content": "Go."}]
    messages = body["messages"]
    assert isinstance(messages, list)
    assert all(message["role"] != "system" for message in messages)


@respx.mock
async def test_a_system_message_anywhere_in_the_conversation_is_still_hoisted() -> None:
    route = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json=RESPONSE))
    async with httpx.AsyncClient() as client:
        provider = AnthropicProvider(api_key=API_KEY, client=client)
        request = CompletionRequest(
            messages=[
                Message(role="user", content="Hi."),
                Message(role="system", content="Late instruction."),
            ]
        )

        await provider.complete(request, model="m")

    body, _ = last_request(route)

    assert body["system"] == "Late instruction."
    assert body["messages"] == [{"role": "user", "content": "Hi."}]


@respx.mock
async def test_no_system_message_means_no_system_key_at_all() -> None:
    route = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json=RESPONSE))
    async with httpx.AsyncClient() as client:
        provider = AnthropicProvider(api_key=API_KEY, client=client)
        request = CompletionRequest(messages=[Message(role="user", content="hi")])

        await provider.complete(request, model="m")

    body, _ = last_request(route)

    assert body == {
        "model": "m",
        "max_tokens": DEFAULT_MAX_OUTPUT_TOKENS,
        "messages": [{"role": "user", "content": "hi"}],
    }


@respx.mock
async def test_max_tokens_is_always_present_because_anthropic_requires_it() -> None:
    route = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json=RESPONSE))
    async with httpx.AsyncClient() as client:
        provider = AnthropicProvider(api_key=API_KEY, client=client)

        await provider.complete(
            CompletionRequest(messages=[Message(role="user", content="hi")]),
            model="m",
        )
        await provider.complete(
            CompletionRequest(messages=[Message(role="user", content="hi")], max_output_tokens=7),
            model="m",
        )

    bodies = [json.loads(call.request.content) for call in route.calls]
    assert all("max_tokens" in body for body in bodies)
    assert bodies[0]["max_tokens"] == DEFAULT_MAX_OUTPUT_TOKENS
    assert bodies[1]["max_tokens"] == 7


@respx.mock
async def test_json_response_format_is_never_sent_as_a_field() -> None:
    """Anthropic's Messages API has no ``response_format``; inventing one would break it."""
    route = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json=RESPONSE))
    async with httpx.AsyncClient() as client:
        provider = AnthropicProvider(api_key=API_KEY, client=client)
        request = CompletionRequest(
            messages=[Message(role="user", content="hi")],
            response_format="json",
        )

        await provider.complete(request, model="m")

    body, _ = last_request(route)
    assert "response_format" not in body


@respx.mock
async def test_the_path_is_v1_messages() -> None:
    route = respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json=RESPONSE))
    async with httpx.AsyncClient() as client:
        provider = AnthropicProvider(api_key=API_KEY, client=client)

        await provider.complete(
            CompletionRequest(messages=[Message(role="user", content="hi")]),
            model="m",
        )

    assert route.called
    assert route.calls[0].request.url.path == "/v1/messages"
    assert route.calls[0].request.url.host == "api.anthropic.com"


@respx.mock
async def test_a_custom_base_url_is_respected() -> None:
    route = respx.post("https://anthropic.internal.example/v1/messages").mock(
        return_value=httpx.Response(200, json=RESPONSE)
    )
    async with httpx.AsyncClient() as client:
        provider = AnthropicProvider(
            api_key=API_KEY,
            base_url="https://anthropic.internal.example",
            client=client,
        )

        await provider.complete(
            CompletionRequest(messages=[Message(role="user", content="hi")]),
            model="m",
        )

    assert route.called


@respx.mock
async def test_response_usage_field_names_and_cache_normalisation() -> None:
    body = {
        "content": [{"type": "text", "text": "ok"}],
        "stop_reason": "max_tokens",
        "usage": {
            "input_tokens": 5,
            "output_tokens": 6,
            "cache_read_input_tokens": 7,
            "cache_creation_input_tokens": 8,
        },
    }
    respx.post(ANTHROPIC_URL).mock(return_value=httpx.Response(200, json=body))
    async with httpx.AsyncClient() as client:
        provider = AnthropicProvider(api_key=API_KEY, client=client)

        completion = await provider.complete(
            CompletionRequest(messages=[Message(role="user", content="hi")]),
            model="m",
        )

    # Normalisation: Anthropic reports cache tokens outside input_tokens.
    assert completion.usage.input_tokens == 5 + 7 + 8
    assert completion.usage.cache_read_tokens == 7
    assert completion.usage.cache_write_tokens == 8
    assert completion.usage.output_tokens == 6
    assert completion.finish_reason == "max_tokens"
