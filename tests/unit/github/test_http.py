"""Transport-level behaviour: required headers and HTTP status classification.

These tests call the shared helpers directly with hand-built ``httpx.Response`` objects,
so the status table is pinned independently of any client wiring.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from scout_github._http import (
    ACCEPT_HEADER,
    API_VERSION,
    DEFAULT_RATE_LIMIT_RETRY_SECONDS,
    DEFAULT_USER_AGENT,
    build_request_headers,
    next_page_url,
    parse_retry_after_seconds,
    raise_for_github_status,
)
from scout_github.errors import (
    GitHubAuthError,
    GitHubError,
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubTransientError,
)

NOW = datetime(2024, 8, 1, 12, 0, 0, tzinfo=UTC)


def _response(status: int, *, headers: dict[str, str] | None = None, json=None) -> httpx.Response:
    request = httpx.Request("GET", "https://api.github.com/repos/edrevel/scout-demo")
    return httpx.Response(status, headers=headers, json=json, request=request)


def test_request_headers_match_the_pinned_api_contract() -> None:
    headers = build_request_headers(token="secret-token")

    assert headers["Authorization"] == "Bearer secret-token"
    assert headers["Accept"] == ACCEPT_HEADER
    assert headers["X-GitHub-Api-Version"] == API_VERSION == "2022-11-28"
    assert headers["User-Agent"] == DEFAULT_USER_AGENT == "scout-edrevel"


def test_success_response_raises_nothing() -> None:
    raise_for_github_status(_response(200, json={"ok": True}), resource="repository")


@pytest.mark.parametrize("status", [201, 204])
def test_other_success_statuses_raise_nothing(status: int) -> None:
    raise_for_github_status(_response(status), resource="repository")


def test_401_is_an_auth_error() -> None:
    with pytest.raises(GitHubAuthError, match="HTTP 401"):
        raise_for_github_status(_response(401, json={"message": "Bad credentials"}), resource="r")


def test_404_is_not_found() -> None:
    with pytest.raises(GitHubNotFoundError, match="cannot see a private repository"):
        raise_for_github_status(_response(404, json={"message": "Not Found"}), resource="r")


def test_403_without_a_rate_limit_signal_is_an_auth_error() -> None:
    response = _response(403, json={"message": "Resource not accessible by integration"})

    with pytest.raises(GitHubAuthError, match="lacks"):
        raise_for_github_status(response, resource="r")


def test_403_with_exhausted_quota_is_a_rate_limit() -> None:
    reset = int((NOW + timedelta(seconds=90)).timestamp())
    response = _response(
        403,
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(reset)},
        json={"message": "API rate limit exceeded"},
    )

    with pytest.raises(GitHubRateLimitError) as excinfo:
        raise_for_github_status(response, resource="r")

    assert excinfo.value.retry_after_seconds is not None


def test_403_secondary_rate_limit_is_detected_from_the_body() -> None:
    response = _response(
        403,
        headers={"Retry-After": "45"},
        json={
            "message": (
                "You have exceeded a secondary rate limit. Please wait a few minutes "
                "before you try again."
            )
        },
    )

    with pytest.raises(GitHubRateLimitError) as excinfo:
        raise_for_github_status(response, resource="r")

    assert excinfo.value.retry_after_seconds == 45.0


def test_429_is_a_rate_limit_with_the_default_delay_when_unspecified() -> None:
    response = _response(429, json={"message": "Too Many Requests"})

    with pytest.raises(GitHubRateLimitError) as excinfo:
        raise_for_github_status(response, resource="r")

    assert excinfo.value.retry_after_seconds == DEFAULT_RATE_LIMIT_RETRY_SECONDS


@pytest.mark.parametrize("status", [500, 502, 503])
def test_5xx_is_transient(status: int) -> None:
    with pytest.raises(GitHubTransientError, match=f"HTTP {status}"):
        raise_for_github_status(_response(status, json={"message": "Server Error"}), resource="r")


def test_unexpected_4xx_is_a_plain_non_retryable_github_error() -> None:
    with pytest.raises(GitHubError, match="HTTP 422") as excinfo:
        raise_for_github_status(_response(422, json={"message": "Validation Failed"}), resource="r")

    assert not isinstance(excinfo.value, GitHubTransientError)


def test_error_body_that_is_not_json_is_still_reported() -> None:
    request = httpx.Request("GET", "https://api.github.com/x")
    response = httpx.Response(503, text="<html>bad gateway</html>", request=request)

    with pytest.raises(GitHubTransientError, match="bad gateway"):
        raise_for_github_status(response, resource="r")


def test_retry_after_seconds_are_parsed() -> None:
    response = _response(429, headers={"Retry-After": "17"})

    assert parse_retry_after_seconds(response) == 17.0


def test_retry_after_http_date_is_parsed_against_the_injected_clock() -> None:
    response = _response(429, headers={"Retry-After": "Fri, 01 Aug 2025 12:00:30 GMT"})

    delay = parse_retry_after_seconds(
        response,
        now=datetime(2025, 8, 1, 12, 0, 0, tzinfo=UTC),
    )

    assert delay == 30.0


def test_retry_after_in_the_past_clamps_to_zero() -> None:
    response = _response(429, headers={"Retry-After": "Fri, 01 Aug 2025 11:59:00 GMT"})

    delay = parse_retry_after_seconds(response, now=datetime(2025, 8, 1, 12, 0, 0, tzinfo=UTC))

    assert delay == 0.0


def test_rate_limit_reset_epoch_is_used_when_retry_after_is_absent() -> None:
    reset = int((NOW + timedelta(seconds=120)).timestamp())
    response = _response(403, headers={"X-RateLimit-Reset": str(reset)})

    assert parse_retry_after_seconds(response, now=NOW) == 120.0


def test_unparseable_rate_limit_headers_yield_none() -> None:
    response = _response(429, headers={"Retry-After": "soon", "X-RateLimit-Reset": "later"})

    assert parse_retry_after_seconds(response, now=NOW) is None


def test_next_page_url_extracts_the_rel_next_link() -> None:
    response = _response(
        200,
        headers={
            "Link": (
                '<https://api.github.com/repositories/1/pulls/2/files?page=2>; rel="next", '
                '<https://api.github.com/repositories/1/pulls/2/files?page=9>; rel="last"'
            )
        },
        json=[],
    )

    assert next_page_url(response) == ("https://api.github.com/repositories/1/pulls/2/files?page=2")


def test_next_page_url_is_none_without_a_link_header() -> None:
    assert next_page_url(_response(200, json=[])) is None
