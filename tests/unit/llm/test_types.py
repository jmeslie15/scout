"""The transport-agnostic value objects."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from scout_llm.types import (
    Completion,
    CompletionRequest,
    Message,
    ModelTarget,
    TokenUsage,
)


def test_token_usage_total_excludes_cache_tokens() -> None:
    """Cache tokens are a subset of input_tokens, so total_tokens must not add them."""
    usage = TokenUsage(
        input_tokens=1_000,
        output_tokens=200,
        cache_read_tokens=500,
        cache_write_tokens=100,
    )
    assert usage.total_tokens == 1_200


def test_token_usage_rejects_negative_counts() -> None:
    with pytest.raises(ValidationError):
        TokenUsage(input_tokens=-1)


def test_completion_request_defaults_are_text_and_empty_metadata() -> None:
    request = CompletionRequest(messages=[Message(role="user", content="hello")])
    assert request.response_format == "text"
    assert request.model is None
    assert request.temperature is None
    assert request.metadata == {}


def test_completion_request_forbids_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        CompletionRequest(
            messages=[Message(role="user", content="hi")],
            max_tokens=10,  # the field is max_output_tokens; a typo must not be ignored
        )


def test_completion_request_requires_at_least_one_message() -> None:
    with pytest.raises(ValidationError):
        CompletionRequest(messages=[])


def test_message_role_is_restricted() -> None:
    with pytest.raises(ValidationError):
        Message(role="tool", content="x")  # type: ignore[arg-type]


def test_value_objects_are_frozen() -> None:
    request = CompletionRequest(messages=[Message(role="user", content="hi")])
    with pytest.raises(ValidationError):
        request.model = "other"  # type: ignore[misc]


def test_temperature_is_bounded() -> None:
    with pytest.raises(ValidationError):
        CompletionRequest(
            messages=[Message(role="user", content="hi")],
            temperature=5.0,
        )


def test_model_target_key_matches_pricing_key_format() -> None:
    assert ModelTarget(provider="openai", model="gpt-4o").key == "openai/gpt-4o"


def test_model_target_requires_non_empty_parts() -> None:
    with pytest.raises(ValidationError):
        ModelTarget(provider="", model="gpt-4o")


def test_completion_defaults_are_conservative() -> None:
    completion = Completion(text="hi", provider="fake", model="m")
    assert completion.usage.total_tokens == 0
    assert completion.finish_reason is None
    assert completion.latency_seconds == 0.0
    assert completion.raw == {}
