"""The offline provider (``SCOUT_LLM_DEFAULT_PROVIDER=fake``).

This is not a mock of OpenAI or Anthropic; it is a real implementation of the same
:class:`~scout_llm.providers.base.LLMProvider` interface that needs no network, no
credentials, and no account. That is what makes "the whole platform runs offline by
default" a fact rather than an aspiration (Rule 2).

Its two jobs:

1. **Deterministic completions.** Identical input yields identical output — no randomness,
   no clock, no network — so an orchestrator test can assert on an exact rendered prompt
   and an agent test can assert on an exact parsed result.
2. **Scripted failure injection.** ``fail_next`` / ``fail_with`` let a retry test prove the
   gateway's backoff and per-attempt recording without a flaky stub server.

Token counts are synthesised from character counts using :data:`CHARS_PER_TOKEN`. They are
plausible, not real, and exist so token/cost accounting is exercisable with no provider at
all. ``Completion.raw`` marks the payload as synthetic so nobody mistakes it for a vendor
response.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Sequence
from typing import Any, Final

from scout_llm.redaction import redact_payload
from scout_llm.types import Completion, CompletionRequest, TokenUsage

__all__ = ["CHARS_PER_TOKEN", "DEFAULT_RESPONSE", "FakeProvider", "estimate_tokens"]

#: Documented approximation used to synthesise usage: four characters per token. It is the
#: standard rough ratio for English text and is deliberately a named constant so a test can
#: assert on it instead of hard-coding 4.
CHARS_PER_TOKEN: Final = 4

#: Returned when no scripted response and no override are configured.
DEFAULT_RESPONSE: Final = "scout fake provider: deterministic offline completion."


def estimate_tokens(text: str) -> int:
    """Estimate tokens in ``text`` from its length.

    Empty text yields ``0`` (there is genuinely nothing to bill); non-empty text yields at
    least ``1`` so that a one-character answer is not reported as a zero-token success.
    """
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN)


class FakeProvider:
    """Deterministic, offline :class:`LLMProvider` implementation."""

    name = "fake"

    def __init__(
        self,
        *,
        script: Sequence[str] | None = None,
        default_response: str = DEFAULT_RESPONSE,
    ) -> None:
        self._script: deque[str] = deque(script or ())
        self._default_response = default_response
        self._failures: deque[Exception] = deque()
        #: Every :class:`CompletionRequest` this provider received, in order. Exposed so an
        #: orchestrator or agent test can assert on the exact prompt that was sent without
        #: reaching into provider internals.
        self.calls: list[CompletionRequest] = []

    @property
    def default_response(self) -> str:
        """The response used once the script is exhausted."""
        return self._default_response

    @default_response.setter
    def default_response(self, value: str) -> None:
        self._default_response = value

    @property
    def script_remaining(self) -> int:
        """How many scripted responses are still queued."""
        return len(self._script)

    @property
    def pending_failures(self) -> int:
        """How many scripted failures are still queued."""
        return len(self._failures)

    def enqueue(self, text: str) -> None:
        """Append a scripted response, consumed by the next call."""
        self._script.append(text)

    def extend(self, texts: Iterable[str]) -> None:
        """Append several scripted responses at once."""
        self._script.extend(texts)

    def fail_next(self, exc: Exception) -> None:
        """Make the next call raise ``exc`` instead of returning a completion."""
        self._failures.append(exc)

    def fail_with(self, exc: Exception, *, times: int) -> None:
        """Make the next ``times`` calls raise ``exc``.

        Used to make "retried exactly ``max_attempts - 1`` times, then surfaced as the
        original typed error" a testable claim.
        """
        if times < 0:
            raise ValueError("times must be >= 0")
        for _ in range(times):
            self._failures.append(exc)

    def supports(self, model: str) -> bool:
        """The fake provider serves any model identifier, including real ones."""
        del model  # every identifier is supported; there is no catalogue to consult
        return True

    async def aclose(self) -> None:
        """No transport to close. Present so ``FakeProvider`` satisfies ``LLMProvider``."""

    async def complete(self, request: CompletionRequest, *, model: str) -> Completion:
        """Return a scripted/derived completion, or raise a scripted failure.

        The incoming request is recorded *before* a scripted failure is raised: the call
        really did happen, and a retry test asserting on ``calls`` must see every attempt.
        """
        self.calls.append(request)

        if self._failures:
            raise self._failures.popleft()

        text = self._script.popleft() if self._script else self._default_response
        prompt_text = "\n".join(message.content for message in request.messages)

        return Completion(
            text=text,
            usage=TokenUsage(
                input_tokens=estimate_tokens(prompt_text),
                output_tokens=estimate_tokens(text),
            ),
            provider=self.name,
            model=model,
            finish_reason="stop",
            # Deterministic zero latency: a real duration would make a "identical input
            # produces identical output" assertion false on every call.
            latency_seconds=0.0,
            raw=redact_payload(
                {
                    "provider": self.name,
                    "synthetic": True,
                    "model": model,
                    "response_format": request.response_format,
                }
            ),
        )

    def raw_debug_payload(self) -> dict[str, Any]:
        """Return a snapshot of provider state for assertions and test failure output."""
        return {
            "calls": len(self.calls),
            "script_remaining": len(self._script),
            "pending_failures": len(self._failures),
        }
