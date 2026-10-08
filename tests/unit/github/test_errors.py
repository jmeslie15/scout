"""Retry classification and shape of the GitHub error hierarchy (spec section 51)."""

from __future__ import annotations

import pytest

from scout_domain.enums import FailureClass
from scout_domain.errors import ScoutDomainError
from scout_domain.state.retry import classify_exception, failure_class_map
from scout_github.errors import (
    GitHubAuthError,
    GitHubError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubTransientError,
    WebhookPayloadError,
    WebhookSignatureError,
)

RETRYABLE_TYPES = [GitHubTransientError, GitHubRateLimitError]
NON_RETRYABLE_TYPES = [
    GitHubError,
    GitHubAuthError,
    GitHubNotFoundError,
    WebhookSignatureError,
    WebhookPayloadError,
]


@pytest.mark.parametrize("error_type", RETRYABLE_TYPES)
def test_transient_errors_are_retryable(error_type: type[GitHubError]) -> None:
    assert error_type.failure_class is FailureClass.RETRYABLE
    assert classify_exception(error_type("boom")) is FailureClass.RETRYABLE


@pytest.mark.parametrize("error_type", NON_RETRYABLE_TYPES)
def test_permanent_errors_are_not_retryable(error_type: type[GitHubError]) -> None:
    assert error_type.failure_class is FailureClass.NON_RETRYABLE
    assert classify_exception(error_type("boom")) is FailureClass.NON_RETRYABLE


def test_rate_limit_error_carries_the_requested_delay() -> None:
    error = GitHubRateLimitError("slow down", retry_after_seconds=42.5)

    assert error.retry_after_seconds == 42.5
    assert "retry_after_seconds=42.5" in str(error)


def test_rate_limit_error_delay_is_optional() -> None:
    error = GitHubRateLimitError("slow down")

    assert error.retry_after_seconds is None


def test_rate_limit_is_a_transient_error_family() -> None:
    error = GitHubRateLimitError("slow down", retry_after_seconds=1.0)

    assert isinstance(error, GitHubTransientError)
    assert isinstance(error, GitHubError)
    assert isinstance(error, ScoutDomainError)


def test_every_github_error_is_registered_at_import_time() -> None:
    registered = failure_class_map()

    for error_type in [*RETRYABLE_TYPES, *NON_RETRYABLE_TYPES]:
        assert error_type in registered, f"{error_type.__name__} was never registered"


def test_error_context_is_rendered_without_secrets() -> None:
    error = GitHubAuthError("installation token rejected", installation_id=99)

    assert "installation_id=99" in str(error)
    assert error.context == {"installation_id": 99}
