"""Every provider satisfies the ``LLMProvider`` protocol at runtime (Rule 2).

This is the substitutability contract: the offline fake and the two real vendors must be
interchangeable from the gateway's point of view, and a non-conforming object must be
rejected by the same check.
"""

from __future__ import annotations

import inspect

import pytest

from scout_llm.providers.anthropic import AnthropicProvider
from scout_llm.providers.base import LLMProvider
from scout_llm.providers.fake import FakeProvider
from scout_llm.providers.openai import OpenAIProvider
from scout_llm.types import CompletionRequest, Message

pytestmark = pytest.mark.contract

API_KEY = "sk-test-abcdef1234567890"


def build_all() -> list[LLMProvider]:
    """One instance of each provider, typed through the protocol."""
    return [
        FakeProvider(),
        OpenAIProvider(api_key=API_KEY),
        AnthropicProvider(api_key=API_KEY),
    ]


async def test_all_providers_are_runtime_instances_of_the_protocol() -> None:
    providers = build_all()
    try:
        for provider in providers:
            assert isinstance(provider, LLMProvider), provider.name
    finally:
        for provider in providers:
            await provider.aclose()


def test_an_object_missing_the_interface_is_not_a_provider() -> None:
    class NotAProvider:
        name = "impostor"

    assert not isinstance(NotAProvider(), LLMProvider)


def test_the_protocol_declares_a_name_attribute() -> None:
    assert "name" in LLMProvider.__annotations__


@pytest.mark.parametrize(
    ("method", "is_async"),
    [("complete", True), ("aclose", True), ("supports", False)],
)
def test_the_protocol_declares_the_expected_methods(method: str, is_async: bool) -> None:
    assert hasattr(LLMProvider, method)
    assert inspect.iscoroutinefunction(getattr(LLMProvider, method)) is is_async


def test_the_protocol_signature_matches_what_the_gateway_calls() -> None:
    """The gateway calls ``complete(request, model=...)``; this pins that shape."""
    signature = inspect.signature(LLMProvider.complete)
    assert list(signature.parameters) == ["self", "request", "model"]
    assert signature.parameters["model"].kind is inspect.Parameter.KEYWORD_ONLY


async def test_provider_names_are_stable_and_distinct() -> None:
    providers = build_all()
    try:
        assert {provider.name for provider in providers} == {"fake", "openai", "anthropic"}
    finally:
        for provider in providers:
            await provider.aclose()


async def test_every_provider_accepts_a_request_and_reports_support() -> None:
    providers = build_all()
    request = CompletionRequest(messages=[Message(role="user", content="hi")])
    try:
        for provider in providers:
            assert provider.supports("some-model-identifier") is True
            assert request.messages[0].content == "hi"
    finally:
        for provider in providers:
            await provider.aclose()
