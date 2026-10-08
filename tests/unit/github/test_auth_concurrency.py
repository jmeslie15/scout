"""The installation-token cache under concurrency (spec section 9).

A burst of jobs for one installation must produce exactly one token request. Without the
per-installation lock, N jobs each mint a token, GitHub's token endpoint rate-limits
them, and every job in the burst fails at once.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from scout_github.auth import GitHubAppAuthenticator
from scout_github.errors import GitHubTransientError

REFERENCE_NOW = datetime(2024, 8, 1, 12, 0, 0, tzinfo=UTC)

CONCURRENT_REQUESTS = 20


@respx.mock
async def test_twenty_concurrent_requests_mint_exactly_one_token(
    rsa_private_key_pem: str,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            201,
            json={
                "token": "ghs_single_token",
                "expires_at": (REFERENCE_NOW + timedelta(seconds=3600)).isoformat(),
            },
        )

    route = respx.post("https://api.github.test/app/installations/42/access_tokens").mock(
        side_effect=handler
    )

    authenticator = GitHubAppAuthenticator(
        app_id="1",
        private_key_pem=rsa_private_key_pem,
        api_base_url="https://api.github.test",
        clock=lambda: REFERENCE_NOW,
    )

    tokens = await asyncio.gather(
        *(authenticator.get_installation_token(42) for _ in range(CONCURRENT_REQUESTS))
    )

    assert route.call_count == 1
    assert calls == 1
    assert tokens == ["ghs_single_token"] * CONCURRENT_REQUESTS

    await authenticator.aclose()


@respx.mock
async def test_concurrent_requests_for_different_installations_do_not_serialise_incorrectly(
    rsa_private_key_pem: str,
) -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        installation_id = int(request.url.path.split("/")[3])
        seen.append(installation_id)
        return httpx.Response(
            201,
            json={
                "token": f"ghs_token_{installation_id}",
                "expires_at": (REFERENCE_NOW + timedelta(seconds=3600)).isoformat(),
            },
        )

    route = respx.post(
        url__regex=r"https://api\.github\.test/app/installations/\d+/access_tokens"
    ).mock(side_effect=handler)

    authenticator = GitHubAppAuthenticator(
        app_id="1",
        private_key_pem=rsa_private_key_pem,
        api_base_url="https://api.github.test",
        clock=lambda: REFERENCE_NOW,
    )

    tokens = await asyncio.gather(
        authenticator.get_installation_token(1),
        authenticator.get_installation_token(2),
        authenticator.get_installation_token(1),
        authenticator.get_installation_token(2),
    )

    assert route.call_count == 2
    assert sorted(seen) == [1, 2]
    assert tokens == ["ghs_token_1", "ghs_token_2", "ghs_token_1", "ghs_token_2"]

    await authenticator.aclose()


@respx.mock
async def test_a_failed_token_request_is_not_cached_and_is_retried_by_the_next_caller(
    rsa_private_key_pem: str,
) -> None:
    responses = [
        httpx.Response(503, json={"message": "Service Unavailable"}),
        httpx.Response(
            201,
            json={
                "token": "ghs_after_failure",
                "expires_at": (REFERENCE_NOW + timedelta(seconds=3600)).isoformat(),
            },
        ),
    ]
    route = respx.post("https://api.github.test/app/installations/5/access_tokens").mock(
        side_effect=responses
    )

    authenticator = GitHubAppAuthenticator(
        app_id="1",
        private_key_pem=rsa_private_key_pem,
        api_base_url="https://api.github.test",
        clock=lambda: REFERENCE_NOW,
    )

    with pytest.raises(GitHubTransientError):
        await authenticator.get_installation_token(5)

    # A failure must not poison the cache: the next caller tries again.
    assert await authenticator.get_installation_token(5) == "ghs_after_failure"
    assert route.call_count == 2

    await authenticator.aclose()
