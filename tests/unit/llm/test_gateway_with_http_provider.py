"""End-to-end seam test: a real HTTP provider behind the gateway, via ``respx``.

The retry tests use the fake provider and the provider tests use respx; this file closes the
gap between them, proving that a vendor's HTTP 500 becomes an ``LLMTransientError``, that the
*gateway* retries it (not the provider), and that the successful retry is what gets recorded.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
import respx

from scout_domain.enums import AgentRole
from scout_domain.models.model_request import ModelRequestStatus
from scout_llm.errors import LLMAuthenticationError
from scout_llm.gateway import LLMGateway
from scout_llm.providers.openai import OpenAIProvider
from scout_llm.types import ModelTarget

ROLE = AgentRole.SECURITY
BODY = "Find security problems in:\n$diff\n"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
API_KEY = "sk-test-abcdef1234567890"

SUCCESS = {
    "id": "chatcmpl-1",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": '{"findings": []}'},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 50, "completion_tokens": 5},
}


@respx.mock
async def test_a_provider_500_is_retried_by_the_gateway_and_the_retry_is_recorded(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
) -> None:
    write_prompt(ROLE, "v1", BODY)
    route = respx.post(OPENAI_URL).mock(
        side_effect=[
            httpx.Response(500, json={"error": {"message": "overloaded"}}),
            httpx.Response(200, json=SUCCESS),
        ]
    )

    async with httpx.AsyncClient() as client:
        provider = OpenAIProvider(api_key=API_KEY, client=client)
        gateway = make_gateway(
            providers={"openai": provider},
            default_target=ModelTarget(provider="openai", model="gpt-4o"),
        )

        result, payload = await gateway.complete_json(ROLE, "v1", {"diff": "-bad"})

        await gateway.aclose()

    assert route.call_count == 2
    assert payload == {"findings": []}
    assert [record.attempt for record in result.attempts] == [1, 2]
    assert result.attempts[0].status is ModelRequestStatus.FAILED
    assert result.attempts[1].status is ModelRequestStatus.SUCCEEDED
    assert "HTTP 500" in (result.attempts[0].error or "")
    # Accounting comes from the successful attempt's vendor-reported usage.
    assert result.record.input_tokens == 50
    assert result.record.output_tokens == 5
    assert result.record.provider == "openai"


@respx.mock
async def test_a_provider_401_is_not_retried_by_the_gateway(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
) -> None:
    write_prompt(ROLE, "v1", BODY)
    route = respx.post(OPENAI_URL).mock(
        return_value=httpx.Response(401, json={"error": {"message": "bad key"}})
    )

    async with httpx.AsyncClient() as client:
        provider = OpenAIProvider(api_key=API_KEY, client=client)
        gateway = make_gateway(
            providers={"openai": provider},
            default_target=ModelTarget(provider="openai", model="gpt-4o"),
        )

        with pytest.raises(LLMAuthenticationError):
            await gateway.complete(ROLE, "v1", {"diff": "-bad"})

    assert route.call_count == 1
    assert gateway.records[0].status is ModelRequestStatus.REJECTED


@respx.mock
async def test_the_retained_raw_payload_of_a_real_call_has_no_credentials(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
) -> None:
    write_prompt(ROLE, "v1", BODY)
    respx.post(OPENAI_URL).mock(return_value=httpx.Response(200, json=SUCCESS))
    async with httpx.AsyncClient() as client:
        provider = OpenAIProvider(api_key=API_KEY, client=client)
        gateway = make_gateway(
            providers={"openai": provider},
            default_target=ModelTarget(provider="openai", model="gpt-4o"),
        )

        result = await gateway.complete(ROLE, "v1", {"diff": "-bad"})
        captured = result.completion.raw

    assert captured["request_headers"]["authorization"] == "***REDACTED***"
    assert API_KEY not in json.dumps(captured)
