"""GitHub App JWT claims, private-key loading, and installation-token caching.

The JWT is decoded against a public key derived from the generated test key, so these
tests prove the token is really signed with the configured private key rather than merely
well-formed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
import respx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from scout_github.auth import (
    ENV_API_URL,
    ENV_APP_ID,
    ENV_PRIVATE_KEY,
    ENV_PRIVATE_KEY_PATH,
    INSTALLATION_TOKEN_LIFETIME_SECONDS,
    INSTALLATION_TOKEN_REFRESH_MARGIN_SECONDS,
    JWT_BACKDATE_SECONDS,
    MAX_JWT_LIFETIME_SECONDS,
    MAX_JWT_TTL_SECONDS,
    GitHubAppAuthenticator,
    _CachedToken,
    create_app_jwt,
    load_private_key,
)
from scout_github.errors import GitHubAuthError, GitHubRateLimitError, GitHubTransientError

REFERENCE_NOW = datetime(2024, 8, 1, 12, 0, 0, tzinfo=UTC)


def _public_key_pem(private_key_pem: str) -> str:
    private_key = serialization.load_pem_private_key(private_key_pem.encode("ascii"), password=None)
    return (
        private_key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode("ascii")
    )


def _decode(token: str, private_key_pem: str) -> dict[str, Any]:
    claims: dict[str, Any] = jwt.decode(
        token,
        _public_key_pem(private_key_pem),
        algorithms=["RS256"],
        options={"verify_exp": False, "verify_iat": False},
    )
    return claims


class _Clock:
    """A mutable time source so cache expiry can be tested without sleeping."""

    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def test_jwt_claims_are_correct(rsa_private_key_pem: str) -> None:
    token = create_app_jwt("123456", rsa_private_key_pem, now=REFERENCE_NOW)

    claims = _decode(token, rsa_private_key_pem)

    assert claims["iss"] == "123456"
    assert claims["iat"] == int((REFERENCE_NOW - timedelta(seconds=60)).timestamp())
    expected_exp = int((REFERENCE_NOW + timedelta(seconds=MAX_JWT_TTL_SECONDS)).timestamp())
    assert claims["exp"] == expected_exp
    assert claims["exp"] - claims["iat"] <= MAX_JWT_LIFETIME_SECONDS


def test_default_jwt_uses_the_whole_allowed_window(rsa_private_key_pem: str) -> None:
    token = create_app_jwt("1", rsa_private_key_pem, now=REFERENCE_NOW)

    claims = _decode(token, rsa_private_key_pem)

    assert claims["exp"] - claims["iat"] == 600
    assert MAX_JWT_TTL_SECONDS == 540


def test_jwt_iat_is_backdated_for_clock_skew(rsa_private_key_pem: str) -> None:
    token = create_app_jwt("1", rsa_private_key_pem, now=REFERENCE_NOW)

    claims = _decode(token, rsa_private_key_pem)

    assert REFERENCE_NOW.timestamp() - claims["iat"] == JWT_BACKDATE_SECONDS
    assert claims["iat"] < claims["exp"]


@pytest.mark.parametrize("ttl", [1, 60, MAX_JWT_TTL_SECONDS])
def test_jwt_accepts_ttls_within_githubs_limit(rsa_private_key_pem: str, ttl: int) -> None:
    token = create_app_jwt("1", rsa_private_key_pem, now=REFERENCE_NOW, ttl_seconds=ttl)

    claims = _decode(token, rsa_private_key_pem)

    assert claims["exp"] - claims["iat"] == ttl + JWT_BACKDATE_SECONDS
    assert claims["exp"] - claims["iat"] <= MAX_JWT_LIFETIME_SECONDS


@pytest.mark.parametrize("ttl", [0, -1, MAX_JWT_TTL_SECONDS + 1, 900])
def test_jwt_rejects_ttls_that_github_would_refuse(rsa_private_key_pem: str, ttl: int) -> None:
    with pytest.raises(ValueError, match="ttl_seconds"):
        create_app_jwt("1", rsa_private_key_pem, now=REFERENCE_NOW, ttl_seconds=ttl)


def test_jwt_rejects_a_blank_app_id(rsa_private_key_pem: str) -> None:
    with pytest.raises(GitHubAuthError, match="App id"):
        create_app_jwt("  ", rsa_private_key_pem, now=REFERENCE_NOW)


def test_jwt_rejects_a_blank_private_key() -> None:
    with pytest.raises(GitHubAuthError, match="private key"):
        create_app_jwt("1", "", now=REFERENCE_NOW)


def test_jwt_signed_by_another_key_is_rejected(rsa_private_key_pem: str) -> None:
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    other_pem = other_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")

    token = create_app_jwt("1", other_pem, now=REFERENCE_NOW)

    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(
            token,
            _public_key_pem(rsa_private_key_pem),
            algorithms=["RS256"],
            options={"verify_exp": False, "verify_iat": False},
        )


def test_jwt_signing_failure_is_reported_as_an_auth_error() -> None:
    with pytest.raises(GitHubAuthError, match="not a valid PEM-encoded RSA key"):
        create_app_jwt("1", "-----BEGIN PRIVATE KEY-----\nnonsense\n-----END PRIVATE KEY-----\n")


def test_load_private_key_from_a_file(tmp_path: Path, rsa_private_key_pem: str) -> None:
    key_file = tmp_path / "app.pem"
    key_file.write_text(rsa_private_key_pem, encoding="utf-8")

    assert load_private_key(private_key_path=key_file).startswith("-----BEGIN PRIVATE KEY-----")


def test_load_private_key_from_an_inline_value_with_escaped_newlines(
    rsa_private_key_pem: str,
) -> None:
    inline = rsa_private_key_pem.replace("\n", "\\n")

    assert load_private_key(private_key_pem=inline) == rsa_private_key_pem


def test_load_private_key_prefers_the_inline_value(rsa_private_key_pem: str) -> None:
    loaded = load_private_key(
        private_key_path="/nonexistent/ignored.pem",
        private_key_pem=rsa_private_key_pem,
    )

    assert loaded == rsa_private_key_pem.strip()


def test_load_private_key_reports_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(GitHubAuthError, match="private key file not found"):
        load_private_key(private_key_path=tmp_path / "absent.pem")


def test_load_private_key_reports_missing_configuration() -> None:
    with pytest.raises(GitHubAuthError, match="no GitHub App private key is configured"):
        load_private_key()


def test_authenticator_from_environment_reads_the_inline_key(
    rsa_private_key_pem: str,
) -> None:
    env = {
        ENV_APP_ID: " 123456 ",
        ENV_PRIVATE_KEY: rsa_private_key_pem,
        ENV_API_URL: "https://ghe.example.com/api/v3/",
    }

    authenticator = GitHubAppAuthenticator.from_environment(env)

    assert authenticator.app_id == "123456"


def test_authenticator_from_environment_reads_a_key_path(
    tmp_path: Path, rsa_private_key_pem: str
) -> None:
    key_file = tmp_path / "app.pem"
    key_file.write_text(rsa_private_key_pem, encoding="utf-8")

    authenticator = GitHubAppAuthenticator.from_environment(
        {ENV_APP_ID: "1", ENV_PRIVATE_KEY_PATH: str(key_file)}
    )

    assert authenticator.app_id == "1"


def test_authenticator_from_environment_rejects_a_blank_app_id(
    rsa_private_key_pem: str,
) -> None:
    with pytest.raises(GitHubAuthError, match="SCOUT_GITHUB_APP_ID"):
        GitHubAppAuthenticator.from_environment(
            {ENV_APP_ID: "", ENV_PRIVATE_KEY: rsa_private_key_pem}
        )


def test_authenticator_requires_a_key() -> None:
    with pytest.raises(GitHubAuthError, match="private key"):
        GitHubAppAuthenticator(app_id="1", private_key_pem=" ")


def test_authenticator_rejects_a_negative_refresh_margin(rsa_private_key_pem: str) -> None:
    with pytest.raises(ValueError, match="refresh_margin_seconds"):
        GitHubAppAuthenticator(
            app_id="1",
            private_key_pem=rsa_private_key_pem,
            refresh_margin_seconds=-1,
        )


async def test_authenticator_rejects_a_non_positive_installation_id(
    rsa_private_key_pem: str,
) -> None:
    authenticator = GitHubAppAuthenticator(app_id="1", private_key_pem=rsa_private_key_pem)

    with pytest.raises(GitHubAuthError, match="positive integer"):
        await authenticator.get_installation_token(0)

    await authenticator.aclose()


@respx.mock
async def test_installation_token_is_cached_and_refreshed_after_fifty_minutes(
    rsa_private_key_pem: str,
) -> None:
    clock = _Clock(REFERENCE_NOW)
    expires_at = REFERENCE_NOW + timedelta(seconds=3600)
    route = respx.post("https://api.github.test/app/installations/42/access_tokens").mock(
        return_value=httpx.Response(
            201,
            json={"token": "ghs_installation_token", "expires_at": expires_at.isoformat()},
        )
    )

    authenticator = GitHubAppAuthenticator(
        app_id="1",
        private_key_pem=rsa_private_key_pem,
        api_base_url="https://api.github.test",
        clock=clock,
    )

    assert await authenticator.get_installation_token(42) == "ghs_installation_token"
    assert route.call_count == 1

    # Still fresh: no second request.
    assert await authenticator.get_installation_token(42) == "ghs_installation_token"
    assert route.call_count == 1

    # Past minute 50 of a 60-minute token: the token is re-minted.
    clock.advance(
        INSTALLATION_TOKEN_LIFETIME_SECONDS - INSTALLATION_TOKEN_REFRESH_MARGIN_SECONDS + 1
    )
    assert await authenticator.get_installation_token(42) == "ghs_installation_token"
    assert route.call_count == 2

    await authenticator.aclose()


@respx.mock
async def test_installation_token_caches_per_installation(rsa_private_key_pem: str) -> None:
    expires_at = REFERENCE_NOW + timedelta(seconds=3600)
    route = respx.post(
        url__regex=r"https://api\.github\.test/app/installations/\d+/access_tokens"
    ).mock(
        return_value=httpx.Response(
            201,
            json={"token": "ghs_token", "expires_at": expires_at.isoformat()},
        )
    )

    authenticator = GitHubAppAuthenticator(
        app_id="1",
        private_key_pem=rsa_private_key_pem,
        api_base_url="https://api.github.test",
        clock=_Clock(REFERENCE_NOW),
    )

    await authenticator.get_installation_token(1)
    await authenticator.get_installation_token(2)
    await authenticator.get_installation_token(1)

    assert route.call_count == 2
    await authenticator.aclose()


@respx.mock
async def test_installation_token_falls_back_to_a_local_lifetime(
    rsa_private_key_pem: str,
) -> None:
    respx.post("https://api.github.test/app/installations/7/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "ghs_no_expiry"})
    )

    authenticator = GitHubAppAuthenticator(
        app_id="1",
        private_key_pem=rsa_private_key_pem,
        api_base_url="https://api.github.test",
        clock=_Clock(REFERENCE_NOW),
    )

    assert await authenticator.get_installation_token(7) == "ghs_no_expiry"
    assert authenticator.cached_token_expires_at(7) == REFERENCE_NOW + timedelta(seconds=3600)

    await authenticator.aclose()


@respx.mock
async def test_installation_token_endpoint_failure_is_classified(
    rsa_private_key_pem: str,
) -> None:
    respx.post("https://api.github.test/app/installations/9/access_tokens").mock(
        return_value=httpx.Response(401, json={"message": "Bad credentials"})
    )
    respx.post("https://api.github.test/app/installations/10/access_tokens").mock(
        return_value=httpx.Response(
            429,
            headers={"Retry-After": "12"},
            json={"message": "API rate limit exceeded"},
        )
    )
    respx.post("https://api.github.test/app/installations/11/access_tokens").mock(
        return_value=httpx.Response(503, json={"message": "Service Unavailable"})
    )
    respx.post("https://api.github.test/app/installations/12/access_tokens").mock(
        side_effect=httpx.ConnectError("connection refused")
    )

    authenticator = GitHubAppAuthenticator(
        app_id="1",
        private_key_pem=rsa_private_key_pem,
        api_base_url="https://api.github.test",
    )

    with pytest.raises(GitHubAuthError):
        await authenticator.get_installation_token(9)
    with pytest.raises(GitHubRateLimitError) as rate_limited:
        await authenticator.get_installation_token(10)
    assert rate_limited.value.retry_after_seconds == 12.0
    with pytest.raises(GitHubTransientError):
        await authenticator.get_installation_token(11)
    with pytest.raises(GitHubTransientError):
        await authenticator.get_installation_token(12)

    await authenticator.aclose()


@respx.mock
async def test_installation_token_missing_from_the_body_is_an_auth_error(
    rsa_private_key_pem: str,
) -> None:
    respx.post("https://api.github.test/app/installations/13/access_tokens").mock(
        return_value=httpx.Response(201, json={"expires_at": "2024-08-01T13:00:00Z"})
    )

    authenticator = GitHubAppAuthenticator(
        app_id="1",
        private_key_pem=rsa_private_key_pem,
        api_base_url="https://api.github.test",
    )

    with pytest.raises(GitHubAuthError, match="no installation token"):
        await authenticator.get_installation_token(13)

    await authenticator.aclose()


def test_cached_token_repr_never_prints_the_token() -> None:
    cached = _CachedToken(token="ghs_super_secret", expires_at=REFERENCE_NOW)
    rendered = repr(cached)

    assert "ghs_super_secret" not in rendered
    assert "redacted" in rendered


def test_authenticator_app_jwt_uses_the_configured_clock(rsa_private_key_pem: str) -> None:
    authenticator = GitHubAppAuthenticator(
        app_id="123",
        private_key_pem=rsa_private_key_pem,
        clock=_Clock(REFERENCE_NOW),
    )

    claims = _decode(authenticator.app_jwt(), rsa_private_key_pem)

    assert claims["iss"] == "123"
    assert json.dumps(claims)  # claims must be JSON-serialisable integers/strings
