"""Shared fixtures for the LLM gateway unit tests.

Every fixture here is offline and deterministic. Prompts live in ``tmp_path`` rather than in
the repository's ``prompts/`` tree, which is owned by a different workstream and may be empty
at any moment.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from scout_domain.enums import AgentRole
from scout_domain.state.retry import RetryPolicy
from scout_llm.gateway import InMemoryRecorder, LLMGateway
from scout_llm.prompts import PromptLibrary
from scout_llm.providers.fake import FakeProvider
from scout_llm.types import ModelTarget

#: Deterministic policy: no jitter, whole-second delays, three attempts.
#: Delays are therefore exactly 1.0s (after attempt 1) and 2.0s (after attempt 2).
TEST_RETRY_POLICY = RetryPolicy(
    max_attempts=3,
    base_delay_seconds=1.0,
    multiplier=2.0,
    max_delay_seconds=60.0,
    jitter_ratio=0.0,
)

PROMPT_BODY = "Review this diff and report JSON.\n\nDiff:\n$diff\n"


class RecordingSleeper:
    """Stand-in for :func:`asyncio.sleep` that records instead of waiting."""

    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


@pytest.fixture
def prompts_root(tmp_path: Path) -> Path:
    """An empty prompts tree unique to each test."""
    root = tmp_path / "prompts"
    root.mkdir()
    return root


@pytest.fixture
def write_prompt(prompts_root: Path) -> Callable[[AgentRole, str, str], Path]:
    """Return a helper that writes ``prompts/<role>/<version>.md``."""

    def _write(role: AgentRole, version: str, body: str) -> Path:
        directory = prompts_root / role.value
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{version}.md"
        path.write_text(body, encoding="utf-8")
        return path

    return _write


@pytest.fixture
def prompt_library(prompts_root: Path) -> PromptLibrary:
    """A library pointed at the per-test prompts tree."""
    return PromptLibrary(prompts_root)


@pytest.fixture
def fake_provider() -> FakeProvider:
    """A fresh offline provider per test."""
    return FakeProvider()


@pytest.fixture
def recorder() -> InMemoryRecorder:
    """A recorder that keeps every record it is handed."""
    return InMemoryRecorder()


@pytest.fixture
def sleeper() -> RecordingSleeper:
    """A sleep function that never actually sleeps."""
    return RecordingSleeper()


@pytest.fixture
def make_gateway(
    fake_provider: FakeProvider,
    prompt_library: PromptLibrary,
    recorder: InMemoryRecorder,
    sleeper: RecordingSleeper,
) -> Callable[..., LLMGateway]:
    """Return a gateway factory with offline defaults and injectable overrides."""

    def _make(**overrides: Any) -> LLMGateway:
        kwargs: dict[str, Any] = {
            "providers": {"fake": fake_provider},
            "default_target": ModelTarget(provider="fake", model="fake-deterministic"),
            "prompts": prompt_library,
            "recorder": recorder,
            "retry_policy": TEST_RETRY_POLICY,
            "max_concurrent_calls": 4,
            "sleep": sleeper,
        }
        kwargs.update(overrides)
        return LLMGateway(**kwargs)

    return _make
