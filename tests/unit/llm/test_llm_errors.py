"""The error hierarchy and its retry classification (Rule 12, spec section 51)."""

from __future__ import annotations

import pytest

from scout_domain.enums import FailureClass
from scout_domain.errors import ScoutDomainError
from scout_domain.state.retry import classify_exception, failure_class_map
from scout_llm.errors import (
    LLMAuthenticationError,
    LLMContextTooLargeError,
    LLMGatewayError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMResponseFormatError,
    LLMTimeoutError,
    LLMTransientError,
    PromptNotFoundError,
    PromptRenderError,
)

RETRYABLE = (
    LLMTransientError,
    LLMRateLimitError,
    LLMTimeoutError,
)
NON_RETRYABLE = (
    LLMAuthenticationError,
    LLMInvalidRequestError,
    LLMResponseFormatError,
    LLMContextTooLargeError,
    PromptNotFoundError,
    PromptRenderError,
)


def test_every_gateway_error_is_a_domain_error() -> None:
    for error_type in RETRYABLE + NON_RETRYABLE + (LLMGatewayError,):
        assert issubclass(error_type, LLMGatewayError)
        assert issubclass(error_type, ScoutDomainError)


@pytest.mark.parametrize("error_type", RETRYABLE)
def test_retryable_errors_classify_as_retryable(error_type: type[LLMGatewayError]) -> None:
    assert error_type.failure_class is FailureClass.RETRYABLE
    assert classify_exception(error_type("boom")) is FailureClass.RETRYABLE


@pytest.mark.parametrize("error_type", NON_RETRYABLE)
def test_non_retryable_errors_classify_as_non_retryable(error_type: type[LLMGatewayError]) -> None:
    assert error_type.failure_class is FailureClass.NON_RETRYABLE
    if error_type is LLMContextTooLargeError:
        exc: LLMGatewayError = error_type("boom", input_tokens=1, max_input_tokens=0)
    else:
        exc = error_type("boom")
    assert classify_exception(exc) is FailureClass.NON_RETRYABLE


def test_the_base_error_fails_closed() -> None:
    assert LLMGatewayError.failure_class is FailureClass.NON_RETRYABLE
    assert classify_exception(LLMGatewayError("boom")) is FailureClass.NON_RETRYABLE


def test_timeout_and_rate_limit_are_transient_so_one_catch_covers_every_retryable_case() -> None:
    assert issubclass(LLMTimeoutError, LLMTransientError)
    assert issubclass(LLMRateLimitError, LLMTransientError)


def test_every_error_type_is_registered_for_classification() -> None:
    registry = failure_class_map()
    for error_type in RETRYABLE + NON_RETRYABLE + (LLMGatewayError,):
        assert error_type in registry


def test_rate_limit_error_carries_retry_after_seconds() -> None:
    exc = LLMRateLimitError("slow down", retry_after_seconds=12.5)
    assert exc.retry_after_seconds == 12.5
    assert LLMRateLimitError("slow down").retry_after_seconds is None


def test_context_too_large_error_carries_token_counts() -> None:
    exc = LLMContextTooLargeError("too big", input_tokens=9_000, max_input_tokens=8_000)
    assert exc.input_tokens == 9_000
    assert exc.max_input_tokens == 8_000


def test_response_format_error_carries_the_raw_text() -> None:
    exc = LLMResponseFormatError("unparseable", raw_text="not json")
    assert exc.raw_text == "not json"


def test_context_is_rendered_into_the_string_form() -> None:
    exc = LLMInvalidRequestError("rejected", provider="openai", status_code=400)
    assert "provider='openai'" in str(exc)
    assert "status_code=400" in str(exc)


def test_classify_exception_prefers_the_most_specific_registration() -> None:
    """A subclass must not be classified by its parent's entry."""
    assert classify_exception(LLMRateLimitError("x")) is FailureClass.RETRYABLE
    assert classify_exception(LLMAuthenticationError("x")) is FailureClass.NON_RETRYABLE
