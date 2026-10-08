"""Retry classification, backoff, and per-attempt recording (spec section 51)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scout_domain.enums import AgentRole
from scout_domain.models.model_request import ModelRequestStatus
from scout_domain.state.retry import RetryPolicy
from scout_llm.errors import (
    LLMAuthenticationError,
    LLMContextTooLargeError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMTransientError,
)
from scout_llm.gateway import InMemoryRecorder, LLMGateway
from scout_llm.providers.fake import FakeProvider

ROLE = AgentRole.CODE_REVIEWER
BODY = "Review this diff:\n$diff\n"


async def test_a_transient_failure_is_retried_then_succeeds(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
    fake_provider: FakeProvider,
    sleeper: Any,
    recorder: InMemoryRecorder,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    fake_provider.fail_with(LLMTransientError("upstream 500"), times=2)
    gateway = make_gateway()

    result = await gateway.complete(ROLE, "v1", {"diff": "x"})

    assert len(fake_provider.calls) == 3  # two failures plus the success
    assert [record.attempt for record in result.attempts] == [1, 2, 3]
    assert [record.status for record in result.attempts] == [
        ModelRequestStatus.FAILED,
        ModelRequestStatus.FAILED,
        ModelRequestStatus.SUCCEEDED,
    ]
    assert result.record.attempt == 3
    assert result.record.status is ModelRequestStatus.SUCCEEDED
    # Exponential backoff after attempt 1 and attempt 2, with jitter disabled in the fixture.
    assert sleeper.delays == [1.0, 2.0]
    # One record per attempt, delivered to the recorder in order.
    assert len(recorder.records) == 3
    assert [record.attempt for record in recorder.records] == [1, 2, 3]


async def test_every_attempt_of_a_failed_call_is_recorded_and_the_original_error_is_raised(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
    fake_provider: FakeProvider,
    sleeper: Any,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    error = LLMTransientError("provider is down")
    fake_provider.fail_with(error, times=3)
    gateway = make_gateway()

    with pytest.raises(LLMTransientError) as excinfo:
        await gateway.complete(ROLE, "v1", {"diff": "x"})

    # Rule 12: the original typed error surfaces unchanged, not a wrapper.
    assert excinfo.value is error
    assert len(fake_provider.calls) == 3  # max_attempts, i.e. max_attempts - 1 retries
    assert len(gateway.records) == 3
    assert {record.status for record in gateway.records} == {ModelRequestStatus.FAILED}
    assert [record.attempt for record in gateway.records] == [1, 2, 3]
    assert all(record.error for record in gateway.records)
    assert sleeper.delays == [1.0, 2.0]  # no sleep after the final attempt


async def test_a_non_retryable_error_is_not_retried(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
    fake_provider: FakeProvider,
    sleeper: Any,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    error = LLMInvalidRequestError("malformed request")
    fake_provider.fail_with(error, times=3)
    gateway = make_gateway()

    with pytest.raises(LLMInvalidRequestError) as excinfo:
        await gateway.complete(ROLE, "v1", {"diff": "x"})

    assert excinfo.value is error
    assert len(fake_provider.calls) == 1
    assert sleeper.delays == []
    assert len(gateway.records) == 1
    assert gateway.records[0].status is ModelRequestStatus.REJECTED


async def test_an_unregistered_exception_fails_closed_and_is_not_retried(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
    fake_provider: FakeProvider,
) -> None:
    """An unknown failure is not a licence to retry."""
    write_prompt(ROLE, "v1", BODY)
    fake_provider.fail_next(ValueError("something unforeseen"))
    gateway = make_gateway()

    with pytest.raises(ValueError, match="something unforeseen"):
        await gateway.complete(ROLE, "v1", {"diff": "x"})

    assert len(fake_provider.calls) == 1
    assert gateway.records[0].status is ModelRequestStatus.FAILED


async def test_authentication_failure_is_rejected_and_not_retried(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
    fake_provider: FakeProvider,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    fake_provider.fail_with(LLMAuthenticationError("bad key"), times=3)
    gateway = make_gateway()

    with pytest.raises(LLMAuthenticationError):
        await gateway.complete(ROLE, "v1", {"diff": "x"})

    assert len(fake_provider.calls) == 1
    assert gateway.records[0].status is ModelRequestStatus.REJECTED


async def test_timeout_is_retryable_and_recorded_as_a_timeout(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
    fake_provider: FakeProvider,
    sleeper: Any,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    fake_provider.fail_with(LLMTimeoutError("no answer"), times=3)
    gateway = make_gateway()

    with pytest.raises(LLMTimeoutError):
        await gateway.complete(ROLE, "v1", {"diff": "x"})

    assert len(fake_provider.calls) == 3
    assert {record.status for record in gateway.records} == {ModelRequestStatus.TIMEOUT}
    assert sleeper.delays == [1.0, 2.0]


async def test_context_too_large_is_rejected_with_its_token_counts(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
    fake_provider: FakeProvider,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    error = LLMContextTooLargeError("too big", input_tokens=200_000, max_input_tokens=128_000)
    fake_provider.fail_with(error, times=1)
    gateway = make_gateway()

    with pytest.raises(LLMContextTooLargeError) as excinfo:
        await gateway.complete(ROLE, "v1", {"diff": "x"})

    assert excinfo.value.input_tokens == 200_000
    assert excinfo.value.max_input_tokens == 128_000
    assert len(fake_provider.calls) == 1
    assert gateway.records[0].status is ModelRequestStatus.REJECTED


async def test_rate_limit_honours_retry_after_as_a_floor(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
    fake_provider: FakeProvider,
    sleeper: Any,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    fake_provider.fail_with(LLMRateLimitError("slow down", retry_after_seconds=5.0), times=3)
    gateway = make_gateway()

    with pytest.raises(LLMRateLimitError):
        await gateway.complete(ROLE, "v1", {"diff": "x"})

    # max(backoff, retry-after) for both retries; no sleep after the final attempt.
    assert sleeper.delays == [5.0, 5.0]
    assert {record.status for record in gateway.records} == {ModelRequestStatus.RATE_LIMITED}


async def test_retry_after_shorter_than_backoff_does_not_shorten_it(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
    fake_provider: FakeProvider,
    sleeper: Any,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    fake_provider.fail_with(LLMRateLimitError("slow down", retry_after_seconds=0.25), times=3)
    gateway = make_gateway()

    with pytest.raises(LLMRateLimitError):
        await gateway.complete(ROLE, "v1", {"diff": "x"})

    assert sleeper.delays == [1.0, 2.0]


async def test_retry_after_is_capped_by_the_policy_ceiling(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
    fake_provider: FakeProvider,
    sleeper: Any,
) -> None:
    """A hostile or mistaken header must not park a worker for an hour."""
    write_prompt(ROLE, "v1", BODY)
    fake_provider.fail_with(LLMRateLimitError("slow down", retry_after_seconds=3600.0), times=2)
    gateway = make_gateway(
        retry_policy=RetryPolicy(
            max_attempts=2,
            base_delay_seconds=1.0,
            multiplier=2.0,
            max_delay_seconds=3.0,
            jitter_ratio=0.0,
        )
    )

    with pytest.raises(LLMRateLimitError):
        await gateway.complete(ROLE, "v1", {"diff": "x"})

    assert sleeper.delays == [3.0]


async def test_max_attempts_of_one_means_no_retry_at_all(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
    fake_provider: FakeProvider,
    sleeper: Any,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    fake_provider.fail_with(LLMTransientError("down"), times=1)
    gateway = make_gateway(retry_policy=RetryPolicy(max_attempts=1, jitter_ratio=0.0))

    with pytest.raises(LLMTransientError):
        await gateway.complete(ROLE, "v1", {"diff": "x"})

    assert len(fake_provider.calls) == 1
    assert sleeper.delays == []


async def test_every_attempt_carries_the_prompt_digest_and_correlation_ids(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
    fake_provider: FakeProvider,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    fake_provider.fail_with(LLMTransientError("down"), times=1)
    gateway = make_gateway()

    result = await gateway.complete(ROLE, "v1", {"diff": "x"}, job_id="job_7", agent_run_id="a1")

    digests = {record.prompt_digest for record in result.attempts}
    assert len(digests) == 1
    assert all(record.prompt_digest for record in result.attempts)
    assert all(record.job_id == "job_7" for record in result.attempts)
    assert all(record.agent_run_id == "a1" for record in result.attempts)
    assert all(record.agent is ROLE for record in result.attempts)
    # Failed attempts report zero tokens rather than inventing usage they never saw.
    assert result.attempts[0].input_tokens == 0
    assert result.attempts[0].output_tokens == 0


async def test_usage_summary_counts_every_attempt_of_a_retried_call(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
    fake_provider: FakeProvider,
) -> None:
    """The provider bills per attempt, so the accounting must too."""
    write_prompt(ROLE, "v1", BODY)
    fake_provider.fail_with(LLMTransientError("down"), times=2)
    gateway = make_gateway()

    await gateway.complete(ROLE, "v1", {"diff": "x"})

    summary = await gateway.usage_summary()
    assert summary["total_attempts"] == 3
    assert summary["by_model"]["fake/fake-deterministic"]["attempts"] == 3
    assert summary["by_model"]["fake/fake-deterministic"]["failed"] == 2
    assert summary["by_model"]["fake/fake-deterministic"]["succeeded"] == 1


async def test_the_backoff_sleep_happens_between_attempts_not_after_the_last_one(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
    fake_provider: FakeProvider,
    sleeper: Any,
    recorder: InMemoryRecorder,
) -> None:
    write_prompt(ROLE, "v1", BODY)
    fake_provider.fail_with(LLMTransientError("down"), times=2)
    gateway = make_gateway()

    await gateway.complete(ROLE, "v1", {"diff": "x"})

    assert len(sleeper.delays) == len(fake_provider.calls) - 1
    assert len(recorder.records) == len(fake_provider.calls)
