"""Gateway happy path, accounting records, and the recorder seam."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scout_domain.enums import AgentRole
from scout_domain.models.model_request import ModelRequestRecord, ModelRequestStatus
from scout_llm.errors import (
    LLMInvalidRequestError,
    LLMResponseFormatError,
    PromptNotFoundError,
    PromptRenderError,
)
from scout_llm.gateway import InMemoryRecorder, LLMGateway
from scout_llm.prompts import PromptLibrary
from scout_llm.providers.fake import FakeProvider
from scout_llm.types import ModelPricing, ModelTarget

ROLE = AgentRole.CODE_REVIEWER
BODY = "Review this diff:\n$diff\n"


class BrokenRecorder:
    """Recorder that always fails, to prove accounting never masks a model result."""

    def __init__(self) -> None:
        self.attempts = 0

    async def record(self, record: ModelRequestRecord) -> None:
        self.attempts += 1
        raise RuntimeError("database is down")


class ClosingProvider(FakeProvider):
    """Fake provider that reports whether it was closed."""

    def __init__(self) -> None:
        super().__init__()
        self.closed = 0

    async def aclose(self) -> None:
        self.closed += 1


def build_gateway(providers: dict[str, Any], **overrides: Any) -> LLMGateway:
    """Build a gateway directly, for the cases the shared fixture does not cover."""
    kwargs: dict[str, Any] = {
        "providers": providers,
        "default_target": ModelTarget(provider="fake", model="fake-deterministic"),
        "prompts": PromptLibrary(Path("/nonexistent-prompts")),
    }
    kwargs.update(overrides)
    return LLMGateway(**kwargs)


async def test_complete_returns_text_and_a_succeeded_record(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
    fake_provider: FakeProvider,
    recorder: InMemoryRecorder,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    gateway = make_gateway()
    fake_provider.enqueue('{"verdict": "pass"}')

    result = await gateway.complete(ROLE, "v1", {"diff": "+x"}, job_id="job_1")

    assert result.text == '{"verdict": "pass"}'
    assert result.prompt_version == "v1"
    assert result.parsed is None  # text format was requested

    record = result.record
    assert record.status is ModelRequestStatus.SUCCEEDED
    assert record.attempt == 1
    assert record.agent is ROLE
    assert record.provider == "fake"
    assert record.model == "fake-deterministic"
    assert record.prompt_version == "v1"
    assert record.job_id == "job_1"
    assert record.output_tokens > 0
    assert record.estimated_cost_usd == 0.0  # fake pricing is zero by construction
    assert recorder.records == [record]
    assert result.attempts == (record,)
    assert record.id.startswith("mreq_")


async def test_prompt_digest_is_the_rendered_prompt_and_sha_is_the_template(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
) -> None:
    write_prompt(ROLE, "v1", BODY)
    gateway = make_gateway()

    result = await gateway.complete(ROLE, "v1", {"diff": "+added"})

    rendered = "Review this diff:\n+added\n"
    assert result.record.prompt_digest == hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    assert result.prompt_sha256 == hashlib.sha256(BODY.encode("utf-8")).hexdigest()
    assert result.record.prompt_digest != result.prompt_sha256


async def test_the_provider_receives_the_rendered_prompt_and_correlation_metadata(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
    fake_provider: FakeProvider,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    gateway = make_gateway()

    await gateway.complete(
        ROLE,
        "v1",
        {"diff": "+added"},
        job_id="job_9",
        agent_run_id="arun_9",
    )

    request = fake_provider.calls[0]
    assert request.messages[0].content == "Review this diff:\n+added\n"
    assert request.model == "fake-deterministic"
    assert request.metadata["agent_role"] == ROLE.value
    assert request.metadata["job_id"] == "job_9"
    assert request.metadata["agent_run_id"] == "arun_9"
    assert request.metadata["prompt_version"] == "v1"


async def test_model_and_provider_overrides_win_over_the_default_target(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
) -> None:
    write_prompt(ROLE, "v1", BODY)
    gateway = make_gateway()

    result = await gateway.complete(ROLE, "v1", {"diff": "x"}, model="gpt-4o")

    assert result.record.model == "gpt-4o"
    assert result.record.provider == "fake"
    assert result.completion.model == "gpt-4o"


async def test_an_unknown_provider_override_raises_a_typed_error_without_calling_anyone(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
    fake_provider: FakeProvider,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    gateway = make_gateway()

    with pytest.raises(LLMInvalidRequestError) as excinfo:
        await gateway.complete(ROLE, "v1", {"diff": "x"}, provider="gemini")

    assert excinfo.value.context["registered"] == ["fake"]
    assert fake_provider.calls == []
    assert gateway.records == ()


async def test_a_missing_prompt_is_not_recorded_as_a_model_request(
    make_gateway: Callable[..., LLMGateway],
    fake_provider: FakeProvider,
) -> None:
    gateway = make_gateway()

    with pytest.raises(PromptNotFoundError):
        await gateway.complete(ROLE, "v9", {"diff": "x"})

    assert fake_provider.calls == []
    assert gateway.records == ()


async def test_a_render_mismatch_never_reaches_the_provider(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
    fake_provider: FakeProvider,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    gateway = make_gateway()

    with pytest.raises(PromptRenderError):
        await gateway.complete(ROLE, "v1", {"dif": "typo"})

    assert fake_provider.calls == []


async def test_a_completion_with_no_output_tokens_is_rejected_as_a_format_error(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
    fake_provider: FakeProvider,
) -> None:
    """The domain refuses to record a hollow success, so the gateway surfaces it."""
    write_prompt(ROLE, "v1", BODY)
    fake_provider.default_response = ""
    gateway = make_gateway()

    with pytest.raises(LLMResponseFormatError) as excinfo:
        await gateway.complete(ROLE, "v1", {"diff": "x"})

    assert "zero output tokens" in str(excinfo.value)
    record = gateway.records[0]
    assert record.status is ModelRequestStatus.FAILED
    assert "zero output tokens" in (record.error or "")
    assert len(fake_provider.calls) == 1  # non-retryable, so exactly one attempt


async def test_max_output_tokens_is_forwarded_to_the_provider(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
    fake_provider: FakeProvider,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    gateway = make_gateway()

    await gateway.complete(ROLE, "v1", {"diff": "x"}, max_output_tokens=256)

    assert fake_provider.calls[0].max_output_tokens == 256


async def test_default_recorder_discards_records_but_the_gateway_still_counts_them(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
) -> None:
    write_prompt(ROLE, "v1", BODY)
    gateway = make_gateway(recorder=None)

    result = await gateway.complete(ROLE, "v1", {"diff": "x"})

    assert result.record.status is ModelRequestStatus.SUCCEEDED
    assert gateway.records == (result.record,)
    assert (await gateway.usage_summary())["total_attempts"] == 1


async def test_a_failing_recorder_does_not_destroy_a_successful_call(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
) -> None:
    write_prompt(ROLE, "v1", BODY)
    broken = BrokenRecorder()
    gateway = make_gateway(recorder=broken)

    result = await gateway.complete(ROLE, "v1", {"diff": "x"})

    assert result.text
    assert broken.attempts == 1
    # The record is still available in memory, so usage reporting keeps working.
    assert gateway.records == (result.record,)


async def test_usage_summary_aggregates_by_provider_and_model(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
) -> None:
    write_prompt(ROLE, "v1", BODY)
    gateway = make_gateway()

    await gateway.complete(ROLE, "v1", {"diff": "x"})
    await gateway.complete(ROLE, "v1", {"diff": "y"}, model="fake-other")

    summary = await gateway.usage_summary()

    assert summary["total_attempts"] == 2
    assert set(summary["by_model"]) == {"fake/fake-deterministic", "fake/fake-other"}
    assert summary["by_model"]["fake/fake-deterministic"]["succeeded"] == 1
    assert summary["total_output_tokens"] > 0
    assert summary["total_estimated_cost_usd"] == 0.0


async def test_usage_summary_of_a_fresh_gateway_is_empty(
    make_gateway: Callable[..., LLMGateway],
) -> None:
    summary = await make_gateway().usage_summary()

    assert summary == {
        "total_attempts": 0,
        "total_input_tokens": 0,
        "total_output_tokens": 0,
        "total_estimated_cost_usd": 0.0,
        "by_model": {},
    }


async def test_aclose_closes_each_distinct_provider_once() -> None:
    provider = ClosingProvider()
    gateway = build_gateway({"fake": provider, "alias": provider})

    await gateway.aclose()

    assert provider.closed == 1


async def test_async_context_manager_closes_providers() -> None:
    provider = ClosingProvider()
    gateway = build_gateway({"fake": provider})

    async with gateway as entered:
        assert entered is gateway

    assert provider.closed == 1


async def test_estimated_cost_is_computed_from_the_injected_pricing_table(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
) -> None:
    """The cost path is exercised end to end with a non-zero rate."""
    write_prompt(ROLE, "v1", BODY)
    # 1_000_000 USD per million tokens == 1 USD per token, so the arithmetic is obvious.
    pricing = {
        "fake/fake-deterministic": ModelPricing(
            input_usd_per_million=1_000_000.0,
            output_usd_per_million=1_000_000.0,
        )
    }
    gateway = make_gateway(pricing=pricing)

    result = await gateway.complete(ROLE, "v1", {"diff": "x"})

    usage = result.completion.usage
    assert usage.total_tokens > 0
    assert result.record.estimated_cost_usd == pytest.approx(float(usage.total_tokens))
    summary = await gateway.usage_summary()
    assert summary["total_estimated_cost_usd"] == pytest.approx(result.record.estimated_cost_usd)


def test_constructor_rejects_empty_providers() -> None:
    with pytest.raises(ValueError, match="at least one provider"):
        build_gateway({})


def test_constructor_rejects_a_non_positive_concurrency_limit() -> None:
    with pytest.raises(ValueError, match="max_concurrent_calls"):
        build_gateway({"fake": FakeProvider()}, max_concurrent_calls=0)


def test_constructor_rejects_a_non_positive_acquire_timeout() -> None:
    with pytest.raises(ValueError, match="acquire_timeout_seconds"):
        build_gateway({"fake": FakeProvider()}, acquire_timeout_seconds=0.0)


async def test_records_property_is_ordered_and_distinct(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
) -> None:
    write_prompt(ROLE, "v1", BODY)
    gateway = make_gateway()

    await gateway.complete(ROLE, "v1", {"diff": "a"})
    await gateway.complete(ROLE, "v1", {"diff": "b"})

    records = gateway.records
    assert isinstance(records, tuple)
    assert [record.attempt for record in records] == [1, 1]
    assert len({record.id for record in records}) == 2
