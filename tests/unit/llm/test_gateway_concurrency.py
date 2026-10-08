"""Concurrency limiting: the semaphore cap and the acquire timeout."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scout_domain.enums import AgentRole
from scout_domain.models.model_request import ModelRequestStatus
from scout_domain.state.retry import RetryPolicy
from scout_llm.errors import LLMTransientError
from scout_llm.gateway import LLMGateway
from scout_llm.types import Completion, CompletionRequest, ModelTarget, TokenUsage

ROLE = AgentRole.CODE_REVIEWER
BODY = "Review this diff:\n$diff\n"


class TrackingProvider:
    """Provider that records the peak number of simultaneous in-flight calls."""

    name = "tracking"

    def __init__(self, *, hold_seconds: float = 0.01) -> None:
        self.active = 0
        self.peak = 0
        self.calls = 0
        self._hold_seconds = hold_seconds

    def supports(self, model: str) -> bool:
        return True

    async def aclose(self) -> None:
        """No transport to close."""

    async def complete(self, request: CompletionRequest, *, model: str) -> Completion:
        self.calls += 1
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self._hold_seconds)
        finally:
            self.active -= 1
        return Completion(
            text="a plausible completion body for accounting",
            usage=TokenUsage(input_tokens=10, output_tokens=9),
            provider=self.name,
            model=model,
        )


class BlockingProvider(TrackingProvider):
    """Provider whose first call blocks until released, to test the acquire timeout."""

    def __init__(self) -> None:
        super().__init__(hold_seconds=0.0)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request: CompletionRequest, *, model: str) -> Completion:
        self.started.set()
        await self.release.wait()
        return await super().complete(request, model=model)


def tracking_gateway(
    make_gateway: Callable[..., LLMGateway],
    provider: TrackingProvider,
    **overrides: Any,
) -> LLMGateway:
    """Build a gateway whose only provider is ``provider``."""
    kwargs: dict[str, Any] = {
        "providers": {provider.name: provider},
        "default_target": ModelTarget(provider=provider.name, model="m"),
    }
    kwargs.update(overrides)
    return make_gateway(**kwargs)


async def test_peak_concurrency_never_exceeds_the_configured_limit(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
) -> None:
    write_prompt(ROLE, "v1", BODY)
    provider = TrackingProvider(hold_seconds=0.005)
    gateway = tracking_gateway(make_gateway, provider, max_concurrent_calls=3)

    results = await asyncio.gather(
        *(gateway.complete(ROLE, "v1", {"diff": str(index)}) for index in range(12))
    )

    assert provider.calls == 12
    assert provider.peak <= 3, f"observed peak {provider.peak} exceeded the cap of 3"
    assert provider.peak == 3, "the cap should actually be used, not merely respected"
    assert len(results) == 12
    assert all(result.record.status is ModelRequestStatus.SUCCEEDED for result in results)


async def test_a_limit_of_one_serialises_every_call(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
) -> None:
    write_prompt(ROLE, "v1", BODY)
    provider = TrackingProvider(hold_seconds=0.002)
    gateway = tracking_gateway(make_gateway, provider, max_concurrent_calls=1)

    await asyncio.gather(*(gateway.complete(ROLE, "v1", {"diff": str(i)}) for i in range(5)))

    assert provider.peak == 1


async def test_a_call_that_cannot_acquire_a_slot_times_out_with_a_typed_error(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
) -> None:
    write_prompt(ROLE, "v1", BODY)
    provider = BlockingProvider()
    gateway = tracking_gateway(
        make_gateway,
        provider,
        max_concurrent_calls=1,
        acquire_timeout_seconds=0.05,
        retry_policy=RetryPolicy(max_attempts=1, jitter_ratio=0.0),
    )

    holder = asyncio.create_task(gateway.complete(ROLE, "v1", {"diff": "holder"}))
    await provider.started.wait()
    try:
        with pytest.raises(LLMTransientError) as excinfo:
            await gateway.complete(ROLE, "v1", {"diff": "waiter"})
        assert "model-call slots" in str(excinfo.value)
        assert excinfo.value.context["max_concurrent_calls"] == 1
    finally:
        provider.release.set()
        await holder

    # The refusal is still an attempt, so it is recorded rather than vanishing.
    statuses = [record.status for record in gateway.records]
    assert ModelRequestStatus.FAILED in statuses
    assert ModelRequestStatus.SUCCEEDED in statuses
    # Only the holder ever reached the provider.
    assert provider.calls == 1


async def test_a_slot_is_released_when_the_provider_raises(
    make_gateway: Callable[..., LLMGateway],
    write_prompt: Callable[[AgentRole, str, str], Path],
) -> None:
    """A failed call must not leak its slot, or the gateway would deadlock over time."""
    write_prompt(ROLE, "v1", BODY)

    class ExplodingProvider(TrackingProvider):
        async def complete(self, request: CompletionRequest, *, model: str) -> Completion:
            raise LLMTransientError("boom")

    provider = ExplodingProvider()
    gateway = tracking_gateway(
        make_gateway,
        provider,
        max_concurrent_calls=1,
        acquire_timeout_seconds=1.0,
        retry_policy=RetryPolicy(max_attempts=1, jitter_ratio=0.0),
    )

    for _ in range(3):
        with pytest.raises(LLMTransientError):
            await gateway.complete(ROLE, "v1", {"diff": "x"})

    # If a slot leaked, the third call would have blocked until the acquire timeout.
    assert len(gateway.records) == 3
