"""GitHub integration error hierarchy (spec sections 51, 54, 55).

Every way a GitHub interaction can fail has a named type here, and each type declares
its retry classification *at import time* through
:func:`scout_domain.state.retry.register_failure_class`. Spec section 51 is explicit
that retry decisions must come from the exception type rather than from a message:
message-based classification means a wording change can silently turn a permanent
authentication failure into a retry storm against GitHub.

Two rules encoded below:

* Transport-shaped failures (5xx, connection resets, timeouts, rate limits) are
  ``RETRYABLE``: the request may succeed if repeated after a delay.
* Everything else — bad credentials, a missing repository, a forged webhook signature,
  a payload Scout cannot understand — is ``NON_RETRYABLE``: repeating the request would
  fail identically and would hide the real fault from an operator.

Nothing in this module ever includes a credential in a message. See
:mod:`scout_github.auth` for the token-handling rules.
"""

from __future__ import annotations

from typing import Any

from scout_domain.enums import FailureClass
from scout_domain.errors import ScoutDomainError
from scout_domain.state.retry import register_failure_class

__all__ = [
    "GitHubAuthError",
    "GitHubError",
    "GitHubNotFoundError",
    "GitHubRateLimitError",
    "GitHubTransientError",
    "WebhookPayloadError",
    "WebhookSignatureError",
]


class GitHubError(ScoutDomainError):
    """Base class for every GitHub integration failure.

    Deliberately non-retryable by default: an unclassified GitHub failure is not a
    licence to retry (spec section 51 fails closed). Subclasses that *are* safe to
    repeat say so explicitly.
    """

    failure_class: FailureClass = FailureClass.NON_RETRYABLE


class GitHubAuthError(GitHubError):
    """Raised when Scout cannot authenticate to GitHub.

    Covers a missing or unreadable private key, a rejected app JWT, an installation
    token that GitHub refuses to mint, and a 401 from the REST API. Retrying cannot
    fix a credential problem, so this is ``NON_RETRYABLE`` and must surface to an
    operator instead (spec section 54).
    """

    failure_class = FailureClass.NON_RETRYABLE


class GitHubNotFoundError(GitHubError):
    """Raised when GitHub reports that a resource does not exist (HTTP 404).

    Retrying would return 404 again. Note that for a private repository a 404 can also
    mean "the installation cannot see this repository"; the message says so, because
    the two causes are indistinguishable over the API and misdiagnosing them wastes
    operator time.
    """

    failure_class = FailureClass.NON_RETRYABLE


class WebhookSignatureError(GitHubError):
    """Raised when a webhook delivery's HMAC signature cannot be trusted.

    A bad signature is either a misconfigured secret or a forgery; in both cases the
    correct response is to reject the delivery, never to retry it (spec section 10).
    """

    failure_class = FailureClass.NON_RETRYABLE


class WebhookPayloadError(GitHubError):
    """Raised when a webhook payload is well-signed but not usable.

    A missing head SHA or an unexpected action means Scout cannot build a verifiable
    job from this delivery; retrying the same bytes changes nothing.
    """

    failure_class = FailureClass.NON_RETRYABLE


class GitHubTransientError(GitHubError):
    """Raised for GitHub failures that are expected to be temporary.

    HTTP 5xx responses and connection/timeout errors. Retrying with exponential
    backoff is correct, and the retry loop lives in the orchestrator, never inside the
    HTTP client (spec section 51).
    """

    failure_class = FailureClass.RETRYABLE


class GitHubRateLimitError(GitHubTransientError):
    """Raised when GitHub asked Scout to slow down (HTTP 403/429).

    Subclasses :class:`GitHubTransientError` because waiting and retrying is the right
    response, but it carries ``retry_after_seconds`` — the delay GitHub itself asked
    for, parsed from ``Retry-After`` or ``X-RateLimit-Reset`` — so the orchestrator can
    honour the upstream's own backpressure instead of guessing (spec section 51).
    """

    failure_class = FailureClass.RETRYABLE

    def __init__(
        self,
        message: str,
        *,
        retry_after_seconds: float | None = None,
        **context: Any,
    ) -> None:
        """Build a rate-limit error carrying the upstream's requested delay.

        Args:
            message: Human-readable description, without credentials.
            retry_after_seconds: Seconds to wait before retrying. ``None`` means GitHub
                did not say; callers should fall back to their backoff policy.
            **context: Additional non-secret diagnostic context (status code, resource).
        """
        if retry_after_seconds is not None:
            context["retry_after_seconds"] = retry_after_seconds
        super().__init__(message, **context)
        self.retry_after_seconds = retry_after_seconds


# Retry classification registration. This runs at import time so that
# ``scout_domain.state.retry.classify_exception`` recognises Scout's GitHub errors even
# if the caller imported only ``scout_domain``. ``classify_exception`` walks the MRO and
# takes the most specific match, so the retryable subclasses are found before the
# non-retryable base.
register_failure_class(GitHubError, FailureClass.NON_RETRYABLE)
register_failure_class(GitHubAuthError, FailureClass.NON_RETRYABLE)
register_failure_class(GitHubNotFoundError, FailureClass.NON_RETRYABLE)
register_failure_class(WebhookSignatureError, FailureClass.NON_RETRYABLE)
register_failure_class(WebhookPayloadError, FailureClass.NON_RETRYABLE)
register_failure_class(GitHubTransientError, FailureClass.RETRYABLE)
register_failure_class(GitHubRateLimitError, FailureClass.RETRYABLE)
