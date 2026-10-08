"""The offline fake provider: determinism, scripting, failure injection, accounting."""

from __future__ import annotations

import pytest

from scout_llm.errors import LLMTransientError
from scout_llm.providers.fake import (
    CHARS_PER_TOKEN,
    DEFAULT_RESPONSE,
    FakeProvider,
    estimate_tokens,
)
from scout_llm.types import CompletionRequest, Message


def make_request(content: str = "review this diff") -> CompletionRequest:
    return CompletionRequest(messages=[Message(role="user", content=content)])


async def test_identical_input_produces_identical_output() -> None:
    provider = FakeProvider()
    first = await provider.complete(make_request(), model="fake-deterministic")
    second = await provider.complete(make_request(), model="fake-deterministic")

    assert first.text == second.text == DEFAULT_RESPONSE
    assert first.usage == second.usage
    assert first.finish_reason == second.finish_reason


async def test_scripted_responses_are_returned_in_order_then_the_default() -> None:
    provider = FakeProvider(script=["one", "two"], default_response="fallback")

    assert (await provider.complete(make_request(), model="m")).text == "one"
    assert (await provider.complete(make_request(), model="m")).text == "two"
    assert (await provider.complete(make_request(), model="m")).text == "fallback"
    assert provider.script_remaining == 0


async def test_enqueue_appends_to_the_script() -> None:
    provider = FakeProvider(default_response="default")
    provider.enqueue("queued")
    assert (await provider.complete(make_request(), model="m")).text == "queued"

    provider.extend(["a", "b"])
    assert provider.script_remaining == 2


async def test_calls_record_the_exact_request_that_was_sent() -> None:
    provider = FakeProvider()
    request = make_request("the rendered prompt")

    await provider.complete(request, model="m")

    assert provider.calls == [request]
    assert provider.calls[0].messages[0].content == "the rendered prompt"


async def test_synthesised_token_usage_follows_the_documented_ratio() -> None:
    prompt = "x" * 400
    provider = FakeProvider(script=["y" * 40])

    completion = await provider.complete(make_request(prompt), model="m")

    assert completion.usage.input_tokens == 400 // CHARS_PER_TOKEN
    assert completion.usage.output_tokens == 40 // CHARS_PER_TOKEN
    assert completion.usage.total_tokens == (400 // CHARS_PER_TOKEN) + (40 // CHARS_PER_TOKEN)
    assert completion.usage.cache_read_tokens == 0


def test_estimate_tokens_never_reports_a_zero_token_non_empty_answer() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("a") == 1
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("a" * 100) == 25


async def test_fail_next_raises_the_scripted_exception_once() -> None:
    provider = FakeProvider()
    error = LLMTransientError("upstream hiccup")
    provider.fail_next(error)

    with pytest.raises(LLMTransientError) as excinfo:
        await provider.complete(make_request(), model="m")
    assert excinfo.value is error

    completion = await provider.complete(make_request(), model="m")
    assert completion.text == DEFAULT_RESPONSE


async def test_fail_with_repeats_the_failure_n_times() -> None:
    provider = FakeProvider()
    provider.fail_with(LLMTransientError("down"), times=2)

    assert provider.pending_failures == 2
    for _ in range(2):
        with pytest.raises(LLMTransientError):
            await provider.complete(make_request(), model="m")
    assert (await provider.complete(make_request(), model="m")).text == DEFAULT_RESPONSE


async def test_a_scripted_failure_still_records_the_call() -> None:
    """Every attempt really happened, so retry assertions on ``calls`` must see it."""
    provider = FakeProvider()
    provider.fail_next(LLMTransientError("down"))

    with pytest.raises(LLMTransientError):
        await provider.complete(make_request(), model="m")

    assert len(provider.calls) == 1


def test_fail_with_rejects_a_negative_count() -> None:
    provider = FakeProvider()
    with pytest.raises(ValueError, match="times must be >= 0"):
        provider.fail_with(LLMTransientError("x"), times=-1)


async def test_latency_is_deterministically_zero() -> None:
    completion = await FakeProvider().complete(make_request(), model="m")
    assert completion.latency_seconds == 0.0


async def test_raw_payload_is_marked_synthetic() -> None:
    completion = await FakeProvider().complete(make_request(), model="m")
    assert completion.raw["synthetic"] is True
    assert completion.raw["provider"] == "fake"


async def test_fake_provider_supports_any_model_identifier() -> None:
    provider = FakeProvider()
    assert provider.supports("gpt-4o") is True
    assert provider.supports("") is True


async def test_aclose_is_a_no_op() -> None:
    provider = FakeProvider()
    await provider.aclose()
    await provider.aclose()


async def test_default_response_is_settable() -> None:
    provider = FakeProvider()
    provider.default_response = "custom"
    assert (await provider.complete(make_request(), model="m")).text == "custom"


async def test_raw_debug_payload_snapshots_state() -> None:
    provider = FakeProvider(script=["a"])
    provider.fail_next(LLMTransientError("x"))

    assert provider.raw_debug_payload() == {
        "calls": 0,
        "script_remaining": 1,
        "pending_failures": 1,
    }
