"""``complete_json``: JSON mode, extraction, and strict failure."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from scout_domain.enums import AgentRole
from scout_domain.models.model_request import ModelRequestStatus
from scout_llm.errors import LLMResponseFormatError
from scout_llm.gateway import LLMGateway
from scout_llm.providers.fake import FakeProvider

ROLE = AgentRole.REPORT_GENERATOR
BODY = "Summarise as JSON:\n$diff\n"

FENCED = 'Here is the result:\n```json\n{"summary": "ok", "count": 2}\n```'


async def test_complete_json_returns_the_parsed_object(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
    fake_provider: FakeProvider,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    fake_provider.enqueue(FENCED)
    gateway = make_gateway()

    result, payload = await gateway.complete_json(ROLE, "v1", {"diff": "x"})

    assert payload == {"summary": "ok", "count": 2}
    assert result.parsed == payload
    assert result.record.status is ModelRequestStatus.SUCCEEDED


async def test_complete_json_requests_json_response_format_from_the_provider(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
    fake_provider: FakeProvider,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    fake_provider.enqueue('{"a": 1}')
    gateway = make_gateway()

    await gateway.complete_json(ROLE, "v1", {"diff": "x"})

    assert fake_provider.calls[0].response_format == "json"


async def test_complete_json_accepts_prose_wrapped_json(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
    fake_provider: FakeProvider,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    fake_provider.enqueue('Sure! {"a": {"b": [1, 2]}} — anything else?')
    gateway = make_gateway()

    _, payload = await gateway.complete_json(ROLE, "v1", {"diff": "x"})

    assert payload == {"a": {"b": [1, 2]}}


async def test_complete_json_without_any_json_raises_with_the_raw_text_attached(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
    fake_provider: FakeProvider,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    fake_provider.enqueue("I am unable to summarise this change.")
    gateway = make_gateway()

    with pytest.raises(LLMResponseFormatError) as excinfo:
        await gateway.complete_json(ROLE, "v1", {"diff": "x"})

    assert excinfo.value.raw_text == "I am unable to summarise this change."
    assert excinfo.value.context["provider"] == "fake"


async def test_complete_json_rejects_a_json_array(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
    fake_provider: FakeProvider,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    fake_provider.enqueue("[1, 2, 3]")
    gateway = make_gateway()

    with pytest.raises(LLMResponseFormatError) as excinfo:
        await gateway.complete_json(ROLE, "v1", {"diff": "x"})

    assert "not an object" in str(excinfo.value)


async def test_complete_with_json_format_leaves_parsed_none_when_unparseable(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
    fake_provider: FakeProvider,
) -> None:
    """``complete`` is best-effort parsing; the raw text is still returned unchanged."""
    write_prompt(ROLE, "v1", BODY)
    fake_provider.enqueue("no json at all")
    gateway = make_gateway()

    result = await gateway.complete(ROLE, "v1", {"diff": "x"}, response_format="json")

    assert result.parsed is None
    assert result.text == "no json at all"
    assert result.record.status is ModelRequestStatus.SUCCEEDED


async def test_complete_with_text_format_never_parses(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
    fake_provider: FakeProvider,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    fake_provider.enqueue('{"looks": "like json"}')
    gateway = make_gateway()

    result = await gateway.complete(ROLE, "v1", {"diff": "x"})

    assert result.parsed is None
    assert result.text == '{"looks": "like json"}'


async def test_an_empty_json_object_is_a_valid_parse(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
    fake_provider: FakeProvider,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    fake_provider.enqueue("{}")
    gateway = make_gateway()

    result, payload = await gateway.complete_json(ROLE, "v1", {"diff": "x"})

    assert payload == {}
    assert result.parsed == {}
