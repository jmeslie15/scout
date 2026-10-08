"""Shared HTTP plumbing for GitHub REST calls.

Everything in this module is transport detail that both the authenticator and the REST
client need: the request headers GitHub requires, URL paging, and — most importantly —
the single place where an HTTP status code is translated into a typed Scout error.

Why a shared module: if the authenticator and the REST client each mapped status codes
independently they would eventually disagree, and a 403 rate limit would be classified
as an authentication failure in one code path and as retryable in the other. Spec
section 51 makes retry classification load-bearing, so it is derived from one table.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Final

import httpx

from scout_github.errors import (
    GitHubAuthError,
    GitHubError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubTransientError,
)

__all__ = [
    "ACCEPT_HEADER",
    "API_VERSION",
    "DEFAULT_API_BASE_URL",
    "DEFAULT_RATE_LIMIT_RETRY_SECONDS",
    "DEFAULT_USER_AGENT",
    "MAX_PAGES",
    "PER_PAGE",
    "build_request_headers",
    "next_page_url",
    "parse_retry_after_seconds",
    "raise_for_github_status",
]

#: The REST API version Scout is written against. Pinned on every request so that a
#: future GitHub default cannot change response shapes underneath a running Scout.
API_VERSION: Final = "2022-11-28"

#: GitHub's recommended media type; selects the current JSON representation.
ACCEPT_HEADER: Final = "application/vnd.github+json"

#: Scout's User-Agent. GitHub blocks requests without one, and an identifiable agent
#: makes Scout's API traffic attributable during an incident.
DEFAULT_USER_AGENT: Final = "scout-edrevel"

#: Public GitHub. GitHub Enterprise Server overrides this with ``https://<host>/api/v3``.
DEFAULT_API_BASE_URL: Final = "https://api.github.com"

#: Fallback delay when GitHub says "you are rate limited" but supplies no numeric hint.
#: GitHub's own documentation tells clients to wait at least one minute before retrying
#: a secondary rate limit, so 60 s is the conservative reading of "no information".
DEFAULT_RATE_LIMIT_RETRY_SECONDS: Final = 60.0

#: Page size for list endpoints. GitHub's maximum, which minimises round trips.
PER_PAGE: Final = 100

#: Hard cap on pages followed, so a paging loop cannot pull unbounded data into memory.
#: 100 per page * 30 pages = 3000 files, which is GitHub's own ceiling for a PR diff.
MAX_PAGES: Final = 30

#: Error bodies are truncated before being placed in an exception message: a proxy's
#: HTML error page is not diagnostic and must not fill a log line.
MAX_ERROR_BODY_CHARS: Final = 500

#: HTTP status codes with specific meanings for retry classification.
_STATUS_UNAUTHORIZED: Final = 401
_STATUS_FORBIDDEN: Final = 403
_STATUS_NOT_FOUND: Final = 404
_STATUS_RATE_LIMITED: Final = 429
_STATUS_SERVER_ERROR: Final = 500

_RETRY_AFTER_HEADER: Final = "Retry-After"
_RATE_LIMIT_RESET_HEADER: Final = "X-RateLimit-Reset"
_RATE_LIMIT_REMAINING_HEADER: Final = "X-RateLimit-Remaining"
_SECONDARY_RATE_LIMIT_MARKER: Final = "rate limit"

_LINK_NEXT_PATTERN: Final = re.compile(r'<([^>]+)>\s*;\s*rel="next"')


def build_request_headers(
    *,
    token: str,
    api_version: str = API_VERSION,
    user_agent: str = DEFAULT_USER_AGENT,
) -> dict[str, str]:
    """Build the header set GitHub requires for an authenticated REST call.

    Why a function: the required headers are a contract (version, media type, agent,
    bearer scheme) and duplicating the literals invites drift between call sites.

    SECURITY: the returned mapping contains a bearer token. It must be passed straight
    to the HTTP client and never logged, not even at debug level.

    Args:
        token: A JWT (for ``/app/...`` endpoints) or an installation access token.
        api_version: Value for ``X-GitHub-Api-Version``.
        user_agent: Value for ``User-Agent``.
    """
    return {
        "Authorization": f"Bearer {token}",
        "Accept": ACCEPT_HEADER,
        "X-GitHub-Api-Version": api_version,
        "User-Agent": user_agent,
    }


def parse_retry_after_seconds(
    response: httpx.Response,
    *,
    now: datetime | None = None,
) -> float | None:
    """Parse how long GitHub asked Scout to wait, in seconds.

    GitHub signals backpressure in two shapes: ``Retry-After`` (seconds, or an HTTP
    date) on secondary rate limits and 429s, and ``X-RateLimit-Reset`` (epoch seconds)
    once the primary quota is exhausted. Honouring the upstream's own hint is strictly
    better than guessing, because a too-short wait burns another request against the
    budget and a too-long wait idles a worker.

    Args:
        response: The rate-limited response.
        now: Reference instant, injectable so the calculation is testable and
            deterministic. Defaults to the current UTC time.

    Returns:
        A non-negative delay in seconds, or ``None`` when the response carried no
        parseable hint.
    """
    reference = now or datetime.now(UTC)

    retry_after = response.headers.get(_RETRY_AFTER_HEADER)
    if retry_after:
        parsed_delay = _parse_retry_after_value(retry_after.strip(), reference)
        if parsed_delay is not None:
            return parsed_delay

    reset = response.headers.get(_RATE_LIMIT_RESET_HEADER)
    if reset:
        try:
            reset_epoch = float(reset.strip())
        except ValueError:
            return None
        return max(0.0, reset_epoch - reference.timestamp())

    return None


def raise_for_github_status(response: httpx.Response, *, resource: str) -> None:
    """Translate a non-success GitHub response into the matching Scout error.

    Mapping (spec sections 51 and 54):

    * ``401`` — credentials rejected: :class:`GitHubAuthError`.
    * ``403`` — a rate limit when GitHub says so (``X-RateLimit-Remaining: 0``, a
      ``Retry-After`` header, or a secondary-rate-limit body message), otherwise a
      missing permission or blocked resource: :class:`GitHubAuthError`, because
      re-running the identical request cannot help.
    * ``404`` — :class:`GitHubNotFoundError`; note the message explains that private
      repositories also 404 when the installation cannot see them.
    * ``429`` — always a rate limit: :class:`GitHubRateLimitError`.
    * ``5xx`` — :class:`GitHubTransientError`.
    * any other 4xx — a plain non-retryable :class:`GitHubError`, so an unexpected
      status surfaces loudly instead of being silently retried.

    This function never retries. Backoff and retry limits belong to the orchestrator
    (spec section 51).

    Args:
        response: The completed response.
        resource: Human-readable description of what was requested, for the message.

    Raises:
        GitHubError: A subclass matching the status code.
    """
    if response.is_success:
        return

    status = response.status_code
    detail = (
        f"GitHub API request for {resource} failed with HTTP {status}: {_api_message(response)}"
    )
    context: dict[str, object] = {"status_code": status, "resource": resource}

    if status == _STATUS_UNAUTHORIZED:
        raise GitHubAuthError(detail, **context)
    if status == _STATUS_NOT_FOUND:
        raise GitHubNotFoundError(
            f"{detail} (a 404 also means the GitHub App installation cannot see a "
            "private repository)",
            **context,
        )
    if status in {_STATUS_FORBIDDEN, _STATUS_RATE_LIMITED}:
        if status == _STATUS_RATE_LIMITED or _looks_rate_limited(response):
            delay = parse_retry_after_seconds(response)
            raise GitHubRateLimitError(
                detail,
                retry_after_seconds=(
                    delay if delay is not None else DEFAULT_RATE_LIMIT_RETRY_SECONDS
                ),
                **context,
            )
        raise GitHubAuthError(
            f"{detail} (403 without a rate-limit signal means the installation lacks "
            "the required permission)",
            **context,
        )
    if status >= _STATUS_SERVER_ERROR:
        raise GitHubTransientError(detail, **context)
    raise GitHubError(detail, **context)


def next_page_url(response: httpx.Response) -> str | None:
    """Return the ``rel="next"`` URL from a response's ``Link`` header, if any.

    GitHub pages list endpoints with the RFC 5988 ``Link`` header and exposes no total
    count, so following this link is the only correct way to enumerate a large pull
    request's files.

    Args:
        response: A successful list response.
    """
    link_header = response.headers.get("Link")
    if not link_header:
        return None
    match = _LINK_NEXT_PATTERN.search(link_header)
    return match.group(1) if match else None


def _looks_rate_limited(response: httpx.Response) -> bool:
    """Return ``True`` when a 403 really is a rate limit rather than a permission denial.

    GitHub overloads 403 for both cases. The discriminators are documented: the primary
    limit leaves ``X-RateLimit-Remaining: 0``, the secondary limit usually carries
    ``Retry-After``, and both include "rate limit" in the body message.
    """
    remaining = response.headers.get(_RATE_LIMIT_REMAINING_HEADER)
    if remaining is not None and remaining.strip() == "0":
        return True
    if response.headers.get(_RETRY_AFTER_HEADER):
        return True
    return _SECONDARY_RATE_LIMIT_MARKER in _api_message(response).lower()


def _parse_retry_after_value(value: str, reference: datetime) -> float | None:
    """Parse a single ``Retry-After`` value: delta-seconds, or an HTTP date."""
    try:
        return max(0.0, float(value))
    except ValueError:
        parsed = _parse_http_date(value)
        if parsed is None:
            return None
        return max(0.0, (parsed - reference).total_seconds())


def _parse_http_date(value: str) -> datetime | None:
    """Parse an RFC 7231 HTTP date, treating a naive result as UTC."""
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _api_message(response: httpx.Response) -> str:
    """Extract GitHub's own error message, bounded and never echoing credentials.

    GitHub error bodies are JSON with a ``message`` field. Anything else (an HTML
    gateway page, an empty body) is truncated raw text, because a truncated raw body is
    still more useful to an operator than nothing.
    """
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()[:MAX_ERROR_BODY_CHARS]
    return response.text.strip()[:MAX_ERROR_BODY_CHARS]
