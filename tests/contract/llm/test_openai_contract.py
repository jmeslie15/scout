"""OpenAI Chat Completions wire contract.

Field names matter here: this is the test that fails when a provider changes its API. The
full request object is compared, so an added, renamed, or omitted field is a failure rather
than something nobody notices until production.
"""

from __future__ import annotations

import json

import httpx
import pytest
import respx

from scout_llm.providers.openai import OpenAIProvider
from scout_llm.types import CompletionRequest, Message

pytestmark = pytest.mark.contract

API_KEY = "sk-test-abcdef1234567890"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"

RESPONSE = {
    "id": "chatcmpl-1",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "ok"},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
}


def last_request(route: respx.Route) -> tuple[dict[str, object], httpx.Request]:
    request = route.calls[-1].request
    return json.loads(request.content), request


@respx.mock
async def test_full_request_body_matches_the_documented_schema() -> None:
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=RESPONSE))
    async with httpx.AsyncClient() as client:
        provider = OpenAIProvider(api_key=API_KEY, client=client)
        request = CompletionRequest(
            messages=[
                Message(role="system", content="You are Scout."),
                Message(role="user", content="Review this."),
            ],
            max_output_tokens=512,
            temperature=0.5,
            stop=["\n\n"],
            response_format="json",
            metadata={"job_id": "job_1"},
        )

        await provider.complete(request, model="gpt-4o")

    body, http_request = last_request(route)

    assert body == {
        "model": "gpt-4o",
        "messages": [
            {"role": "system", "content": "You are Scout."},
            {"role": "user", "content": "Review this."},
        ],
        "max_completion_tokens": 512,
        "temperature": 0.5,
        "stop": ["\n\n"],
        "response_format": {"type": "json_object"},
    }
    assert str(http_request.url) == OPENAI_URL
    assert http_request.method == "POST"
    assert http_request.headers["authorization"] == f"Bearer {API_KEY}"
    assert http_request.headers["content-type"] == "application/json"


@respx.mock
async def test_minimal_request_omits_every_optional_field() -> None:
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=RESPONSE))
    async with httpx.AsyncClient() as client:
        provider = OpenAIProvider(api_key=API_KEY, client=client)
        request = CompletionRequest(messages=[Message(role="user", content="hi")])

        await provider.complete(request, model="gpt-4o-mini")

    body, _ = last_request(route)

    assert body == {
        "model": "gpt-4o-mini",
        "messages": [{"role": "user", "content": "hi"}],
    }
    # No stray nulls, no invented fields, no gateway-local metadata.
    assert "max_completion_tokens" not in body
    assert "temperature" not in body
    assert "stop" not in body
    assert "response_format" not in body
    assert "metadata" not in body


@respx.mock
async def test_text_format_does_not_request_json_mode() -> None:
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=RESPONSE))
    async with httpx.AsyncClient() as client:
        provider = OpenAIProvider(api_key=API_KEY, client=client)
        request = CompletionRequest(
            messages=[Message(role="user", content="hi")],
            response_format="text",
        )

        await provider.complete(request, model="gpt-4o")

    body, _ = last_request(route)
    assert "response_format" not in body


@respx.mock
async def test_the_path_is_chat_completions_under_the_v1_base() -> None:
    route = respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=RESPONSE))
    async with httpx.AsyncClient() as client:
        provider = OpenAIProvider(api_key=API_KEY, client=client)

        await provider.complete(
            CompletionRequest(messages=[Message(role="user", content="hi")]),
            model="gpt-4o",
        )

    assert route.called
    assert route.calls[0].request.url.path == "/v1/chat/completions"


@respx.mock
async def test_a_custom_base_url_is_respected() -> None:
    """Self-hosted or proxied gateways must work without touching the provider code."""
    route = respx.post("https://llm.internal.example/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=RESPONSE)
    )
    async with httpx.AsyncClient() as client:
        provider = OpenAIProvider(
            api_key=API_KEY,
            base_url="https://llm.internal.example/v1/",
            client=client,
        )

        await provider.complete(
            CompletionRequest(messages=[Message(role="user", content="hi")]),
            model="gpt-4o",
        )

    assert route.called


@respx.mock
async def test_response_usage_field_names_are_mapped_as_documented() -> None:
    """``usage.prompt_tokens``/``completion_tokens`` are the OpenAI names; the gateway's
    transport-agnostic names are ``input_tokens``/``output_tokens``."""
    body = {
        "id": "chatcmpl-2",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {
            "prompt_tokens": 11,
            "completion_tokens": 22,
            "prompt_tokens_details": {"cached_tokens": 3},
        },
    }
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=body))
    async with httpx.AsyncClient() as client:
        provider = OpenAIProvider(api_key=API_KEY, client=client)

        completion = await provider.complete(
            CompletionRequest(messages=[Message(role="user", content="hi")]),
            model="gpt-4o",
        )

    assert completion.usage.input_tokens == 11
    assert completion.usage.output_tokens == 22
    assert completion.usage.cache_read_tokens == 3
    assert completion.finish_reason == "stop"
