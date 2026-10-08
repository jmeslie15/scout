"""LLM gateway error hierarchy (spec sections 37 and 51).

Every failure mode the orchestrator must reason about has a named type here, so retry
classification never depends on matching error strings (Rule 12). Classification is
attached to the *exception type* via :func:`register_failure_class` at import time, and
:func:`scout_domain.state.retry.classify_exception` reads that registry.

Retryable vs not (spec section 51):

* retryable — a transient transport/provider fault, a rate limit, a timeout;
* non-retryable — bad credentials, a malformed request, a response Scout cannot parse, a
  prompt that is too large, a prompt version that does not exist.

``LLMRateLimitError`` and ``LLMTimeoutError`` subclass ``LLMTransientError`` so that a
caller who only wants "is this worth retrying?" can catch one type, while the gateway can
still distinguish them for accounting (``RATE_LIMITED`` vs ``TIMEOUT``).
"""

from __future__ import annotations

from typing import Any

from scout_domain.enums import FailureClass
from scout_domain.errors import ScoutDomainError
from scout_domain.state.retry import register_failure_class

__all__ = [
    "LLMAuthenticationError",
    "LLMContextTooLargeError",
    "LLMGatewayError",
    "LLMInvalidRequestError",
    "LLMRateLimitError",
    "LLMResponseFormatError",
    "LLMTimeoutError",
    "LLMTransientError",
    "PromptNotFoundError",
    "PromptRenderError",
]


class LLMGatewayError(ScoutDomainError):
    """Base class for every error raised by ``scout_llm``.

    The default classification is *non-retryable*: a new error type is not a licence to
    retry until it explicitly says so (fail closed).
    """

    failure_class: FailureClass = FailureClass.NON_RETRYABLE


class LLMAuthenticationError(LLMGatewayError):
    """The provider rejected Scout's credentials (HTTP 401/403).

    Non-retryable: retrying a bad key only burns the rate-limit budget and delays the
    operator-visible "your key is wrong" signal.
    """

    failure_class = FailureClass.NON_RETRYABLE


class LLMInvalidRequestError(LLMGatewayError):
    """The provider rejected the request as malformed (HTTP 400/422, or an unknown route).

    Non-retryable: an identical request produces an identical rejection.
    """

    failure_class = FailureClass.NON_RETRYABLE


class LLMTransientError(LLMGatewayError):
    """A temporary provider or transport failure (HTTP 5xx, connection reset, ...).

    Retryable with exponential backoff, which the *gateway* (never a provider) owns.
    """

    failure_class = FailureClass.RETRYABLE


class LLMRateLimitError(LLMTransientError):
    """The provider throttled Scout (HTTP 429).

    Carries ``retry_after_seconds`` when the provider advertised a ``retry-after``
    header, so the gateway can wait at least as long as the provider asked instead of
    retrying straight into the same wall.
    """

    failure_class = FailureClass.RETRYABLE

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float | None = None,
        **context: Any,
    ) -> None:
        super().__init__(message, retry_after_seconds=retry_after_seconds, **context)
        self.retry_after_seconds: float | None = retry_after_seconds


class LLMTimeoutError(LLMTransientError):
    """The provider did not answer within the configured timeout.

    Retryable: a slow provider is usually a busy provider.
    """

    failure_class = FailureClass.RETRYABLE


class LLMResponseFormatError(LLMGatewayError):
    """The provider answered, but the payload is not usable.

    Raised for a body that is not JSON, a body missing the expected fields, an empty
    completion, or (from :meth:`scout_llm.gateway.LLMGateway.complete_json`) text that
    contains no JSON object. Non-retryable: re-asking the same prompt would most likely
    reproduce the same malformed shape, and a retry storm is worse than a clear failure.
    """

    failure_class = FailureClass.NON_RETRYABLE

    def __init__(self, message: str, *, raw_text: str | None = None, **context: Any) -> None:
        super().__init__(message, raw_text=raw_text, **context)
        self.raw_text: str | None = raw_text


class LLMContextTooLargeError(LLMGatewayError):
    """The rendered prompt exceeds the target model's context window.

    Carries both token counts so the caller can shrink the context instead of retrying
    the same oversized prompt. Non-retryable.
    """

    failure_class = FailureClass.NON_RETRYABLE

    def __init__(
        self,
        message: str,
        *,
        input_tokens: int,
        max_input_tokens: int,
        **context: Any,
    ) -> None:
        super().__init__(
            message,
            input_tokens=input_tokens,
            max_input_tokens=max_input_tokens,
            **context,
        )
        self.input_tokens = input_tokens
        self.max_input_tokens = max_input_tokens


class PromptNotFoundError(LLMGatewayError):
    """No prompt template exists at ``prompts/<role>/<version>.md``.

    Non-retryable, and deliberately actionable: the message lists the versions that do
    exist, because the usual cause is a typo or a version that was never committed.
    """

    failure_class = FailureClass.NON_RETRYABLE


class PromptRenderError(LLMGatewayError):
    """A prompt template could not be rendered strictly.

    Raised for a missing variable, an unused variable, or a malformed ``$`` placeholder.
    Non-retryable: this is a programming error in the calling agent, not a transient
    condition, and silently substituting an empty string would corrupt the prompt while
    looking like success (Rule 13).
    """

    failure_class = FailureClass.NON_RETRYABLE


# Register at import time so ``classify_exception`` works without the caller having to
# know anything about this module. The registry is keyed by exact type and walked along
# the MRO, so the most specific registration wins.
register_failure_class(LLMGatewayError, FailureClass.NON_RETRYABLE)
register_failure_class(LLMAuthenticationError, FailureClass.NON_RETRYABLE)
register_failure_class(LLMInvalidRequestError, FailureClass.NON_RETRYABLE)
register_failure_class(LLMTransientError, FailureClass.RETRYABLE)
register_failure_class(LLMRateLimitError, FailureClass.RETRYABLE)
register_failure_class(LLMTimeoutError, FailureClass.RETRYABLE)
register_failure_class(LLMResponseFormatError, FailureClass.NON_RETRYABLE)
register_failure_class(LLMContextTooLargeError, FailureClass.NON_RETRYABLE)
register_failure_class(PromptNotFoundError, FailureClass.NON_RETRYABLE)
register_failure_class(PromptRenderError, FailureClass.NON_RETRYABLE)
