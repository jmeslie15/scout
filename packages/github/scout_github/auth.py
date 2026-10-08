"""GitHub App authentication (spec sections 9, 54, 55).

The flow is the documented GitHub App flow:

1. Sign a short-lived RS256 JWT with the App's private key.
2. Exchange it for an *installation* access token, which is what actually authorises
   repository reads and check-run writes.
3. Cache that token per installation and refresh it before it expires.

Three decisions worth stating explicitly, because each is a place where a GitHub
integration typically goes wrong:

* **The JWT is pure and time-injectable.** :func:`create_app_jwt` takes an explicit
  ``now`` and encodes only timestamps, so its claims are unit-testable without freezing
  the process clock. ``iat`` is backdated 60 seconds for clock skew, and the lifetime is
  capped so that ``exp - iat`` never exceeds GitHub's 600-second window — GitHub rejects
  a JWT that is more than ten minutes old, and a just-backdated ``iat`` is already one
  minute old.
* **The token cache is per-installation and lock-protected.** Twenty jobs for the same
  installation starting at once must produce exactly one token request, not twenty:
  GitHub rate-limits the token endpoint, and a thundering herd there fails every job at
  once.
* **Secrets are never logged.** No statement anywhere in this module interpolates the
  private key, the JWT, or an installation token into a log record — not even at debug
  level. Diagnostics name the installation id and the endpoint only.

Private keys come from a file path or from the environment; there is no default key, no
placeholder, and no fallback, because a hardcoded key in a repository is a breach.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Protocol, runtime_checkable

import httpx
import jwt

from scout_github._http import (
    API_VERSION,
    DEFAULT_API_BASE_URL,
    DEFAULT_USER_AGENT,
    build_request_headers,
    raise_for_github_status,
)
from scout_github._logging import get_logger
from scout_github.errors import GitHubAuthError, GitHubTransientError

__all__ = [
    "ENV_API_URL",
    "ENV_APP_ID",
    "ENV_PRIVATE_KEY",
    "ENV_PRIVATE_KEY_PATH",
    "INSTALLATION_TOKEN_LIFETIME_SECONDS",
    "INSTALLATION_TOKEN_REFRESH_MARGIN_SECONDS",
    "JWT_BACKDATE_SECONDS",
    "MAX_JWT_LIFETIME_SECONDS",
    "MAX_JWT_TTL_SECONDS",
    "GitHubAppAuthenticator",
    "InstallationTokenProvider",
    "create_app_jwt",
    "load_private_key",
]

logger = get_logger(__name__)

#: GitHub rejects an app JWT whose ``exp`` is more than ten minutes in the future.
MAX_JWT_LIFETIME_SECONDS: Final = 600

#: Clock-skew allowance recommended by GitHub: backdate ``iat`` by one minute so a
#: slightly fast signing host does not produce a JWT GitHub considers not-yet-valid.
JWT_BACKDATE_SECONDS: Final = 60

#: Longest JWT Scout will mint. Because ``iat`` is already one minute in the past, the
#: total span ``exp - iat`` is ``ttl + 60``; capping ``ttl`` at 540 keeps that at exactly
#: GitHub's 600-second maximum.
MAX_JWT_TTL_SECONDS: Final = MAX_JWT_LIFETIME_SECONDS - JWT_BACKDATE_SECONDS

#: Documented installation-token lifetime.
INSTALLATION_TOKEN_LIFETIME_SECONDS: Final = 3600

#: Refresh margin: a 60-minute token is refreshed from minute 50, so a token is never
#: used within ten minutes of expiry. That absorbs clock skew between Scout's host and
#: GitHub's, plus the duration of a long-running request.
INSTALLATION_TOKEN_REFRESH_MARGIN_SECONDS: Final = 600

#: Environment variables. Names match ``.env.example``; the inline PEM alternative
#: exists for container platforms that mount secrets as a variable rather than a file.
ENV_APP_ID: Final = "SCOUT_GITHUB_APP_ID"
ENV_PRIVATE_KEY_PATH: Final = "SCOUT_GITHUB_PRIVATE_KEY_PATH"
ENV_PRIVATE_KEY: Final = "SCOUT_GITHUB_PRIVATE_KEY"
ENV_API_URL: Final = "SCOUT_GITHUB_API_URL"

_ACCESS_TOKEN_PATH_TEMPLATE: Final = "/app/installations/{installation_id}/access_tokens"


def create_app_jwt(
    app_id: str,
    private_key_pem: str,
    *,
    now: datetime | None = None,
    ttl_seconds: int = MAX_JWT_TTL_SECONDS,
) -> str:
    """Mint a GitHub App JWT (RS256) for the ``/app/...`` endpoints.

    Why ``iat`` is backdated: GitHub validates ``iat``/``exp`` against its own clock. A
    host whose clock is a few seconds fast would otherwise sign a JWT that is "issued in
    the future" and be rejected with an unhelpful 401.

    Why the TTL is capped at 540 s: GitHub refuses a JWT older than ten minutes, and
    ``exp - iat`` is ``ttl + 60`` because of the skew allowance. 540 s is therefore the
    longest token that is both valid on arrival and accepted at the far end of a slow
    request.

    Args:
        app_id: The GitHub App's numeric id, as a string (as configured).
        private_key_pem: PEM-encoded RSA private key. Never logged by this function.
        now: Reference instant; injectable for deterministic tests. Defaults to now.
        ttl_seconds: Requested lifetime, at most :data:`MAX_JWT_TTL_SECONDS`.

    Returns:
        The encoded JWT.

    Raises:
        GitHubAuthError: When the app id or key is missing, or signing fails (a
            malformed key is an operator problem, not a transient one).
        ValueError: When ``ttl_seconds`` is not a usable lifetime.
    """
    if not app_id or not app_id.strip():
        raise GitHubAuthError(
            f"GitHub App id is not configured; set {ENV_APP_ID} to the App's numeric id"
        )
    if not private_key_pem or not private_key_pem.strip():
        raise GitHubAuthError(
            "GitHub App private key is not configured; set "
            f"{ENV_PRIVATE_KEY_PATH} to the downloaded .pem file or {ENV_PRIVATE_KEY} "
            "to its contents"
        )
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    if ttl_seconds > MAX_JWT_TTL_SECONDS:
        raise ValueError(
            f"ttl_seconds must be <= {MAX_JWT_TTL_SECONDS}: a longer JWT would exceed "
            f"GitHub's {MAX_JWT_LIFETIME_SECONDS}-second limit once the "
            f"{JWT_BACKDATE_SECONDS}-second clock-skew allowance is added"
        )

    reference = now or datetime.now(UTC)
    issued_at = reference - timedelta(seconds=JWT_BACKDATE_SECONDS)
    expires_at = reference + timedelta(seconds=ttl_seconds)

    claims: dict[str, Any] = {
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
        "iss": str(app_id).strip(),
    }

    try:
        encoded = jwt.encode(claims, private_key_pem, algorithm="RS256")
    except (jwt.PyJWTError, ValueError) as exc:
        # The exception message from PyJWT/cryptography never contains key material,
        # but Scout still replaces it with an actionable operator message.
        raise GitHubAuthError(
            "failed to sign the GitHub App JWT; the configured private key is not a "
            "valid PEM-encoded RSA key"
        ) from exc
    return encoded


def load_private_key(
    *,
    private_key_path: str | Path | None = None,
    private_key_pem: str | None = None,
) -> str:
    """Load the App private key from a file path or an inline environment value.

    Why no default: a fallback key would be either a committed secret or a broken
    authentication path that only fails in production. Missing configuration raises
    immediately, with a message that names the exact variable to set.

    Args:
        private_key_path: Path to the ``.pem`` file GitHub generated.
        private_key_pem: The PEM text itself, typically from a secret manager. Literal
            ``\\n`` sequences are converted to real newlines, which is how PEMs survive
            single-line environment variables.

    Returns:
        The PEM text.

    Raises:
        GitHubAuthError: When neither source is configured, the file is missing, or the
            file cannot be read.
    """
    if private_key_pem and private_key_pem.strip():
        return private_key_pem.strip().replace("\\n", "\n")

    if private_key_path and str(private_key_path).strip():
        path = Path(str(private_key_path)).expanduser()
        if not path.is_file():
            raise GitHubAuthError(
                f"GitHub App private key file not found at {path}; check "
                f"{ENV_PRIVATE_KEY_PATH}, or supply the PEM through {ENV_PRIVATE_KEY}"
            )
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise GitHubAuthError(
                f"could not read the GitHub App private key at {path}; check file "
                "permissions for the Scout process user"
            ) from exc

    raise GitHubAuthError(
        "no GitHub App private key is configured; set "
        f"{ENV_PRIVATE_KEY_PATH} to the .pem file or {ENV_PRIVATE_KEY} to its contents"
    )


@runtime_checkable
class InstallationTokenProvider(Protocol):
    """The narrow interface the REST client needs for authentication.

    Declaring it as a protocol (rather than accepting
    :class:`GitHubAppAuthenticator` directly) means the HTTP client can be constructed
    with a fixed token in tests, or with an installation-token broker in a future
    deployment, without changing the client.
    """

    async def get_installation_token(self, installation_id: int) -> str:
        """Return a currently valid installation access token."""
        ...


@dataclass(slots=True)
class _CachedToken:
    """One cached installation token and its absolute expiry."""

    token: str
    expires_at: datetime

    def __repr__(self) -> str:
        """Redact the token: a dataclass repr would otherwise leak it into logs.

        Python's default dataclass ``repr`` prints every field, so a single
        ``logger.debug("%r", cached)`` anywhere in the process would write a live
        credential to disk. Overriding ``repr`` removes that failure mode entirely.
        """
        return f"_CachedToken(expires_at={self.expires_at.isoformat()!r}, token=<redacted>)"


class GitHubAppAuthenticator:
    """Mints, caches, and refreshes GitHub App installation tokens.

    One instance is shared by every job that talks to GitHub; the cache is keyed by
    installation id because a single Scout deployment may serve several installations
    (an organisation plus a personal account, for example).

    SECURITY: this class never logs the private key, the app JWT, or an installation
    token, at any level. Diagnostics identify the installation by id only. Keep it that
    way: a token in a log aggregator is a credential with a 60-minute lifetime and an
    unknown number of readers.
    """

    def __init__(
        self,
        *,
        app_id: str,
        private_key_pem: str,
        api_base_url: str = DEFAULT_API_BASE_URL,
        http_client: httpx.AsyncClient | None = None,
        refresh_margin_seconds: int = INSTALLATION_TOKEN_REFRESH_MARGIN_SECONDS,
        clock: Callable[[], datetime] | None = None,
        request_timeout_seconds: float = 30.0,
        api_version: str = API_VERSION,
        user_agent: str = DEFAULT_USER_AGENT,
    ) -> None:
        """Configure the authenticator.

        Args:
            app_id: GitHub App numeric id as a string.
            private_key_pem: PEM-encoded RSA private key.
            api_base_url: GitHub API root; overridden for GitHub Enterprise Server.
            http_client: Optional injected client (tests, or a shared pool). When
                omitted, this instance creates and owns one.
            refresh_margin_seconds: Refresh a token this long before it expires.
            clock: Time source, injectable so cache-expiry behaviour is testable.
            request_timeout_seconds: Timeout for the token endpoint when this instance
                owns its client.
            api_version: ``X-GitHub-Api-Version`` value.
            user_agent: ``User-Agent`` value.

        Raises:
            GitHubAuthError: When the app id or private key is missing.
        """
        if not app_id or not app_id.strip():
            raise GitHubAuthError(
                f"GitHub App id is not configured; set {ENV_APP_ID}, or leave it blank "
                "to run with the in-memory fake GitHub client"
            )
        if not private_key_pem or not private_key_pem.strip():
            raise GitHubAuthError(
                "GitHub App private key is not configured; set "
                f"{ENV_PRIVATE_KEY_PATH} to the .pem file or {ENV_PRIVATE_KEY} to its "
                "contents"
            )
        if refresh_margin_seconds < 0:
            raise ValueError("refresh_margin_seconds must not be negative")

        self._app_id = app_id.strip()
        self._private_key_pem = private_key_pem
        self._api_base_url = api_base_url.strip().rstrip("/") or DEFAULT_API_BASE_URL
        self._refresh_margin = timedelta(seconds=refresh_margin_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._api_version = api_version
        self._user_agent = user_agent
        self._tokens: dict[int, _CachedToken] = {}
        self._locks: dict[int, asyncio.Lock] = {}
        self._owns_http_client = http_client is None
        self._http_client = (
            http_client
            if http_client is not None
            else httpx.AsyncClient(timeout=request_timeout_seconds)
        )

    @classmethod
    def from_environment(
        cls,
        env: Mapping[str, str] | None = None,
        **overrides: Any,
    ) -> GitHubAppAuthenticator:
        """Build an authenticator from Scout's environment variables.

        Args:
            env: Environment mapping; defaults to :data:`os.environ`. Passing a mapping
                keeps configuration handling testable and keeps ``.env`` parsing out of
                this module.
            **overrides: Forwarded to :meth:`__init__` (for example ``http_client``).

        Raises:
            GitHubAuthError: When the app id or key is missing or unreadable.
        """
        values: Mapping[str, str] = os.environ if env is None else env
        app_id = (values.get(ENV_APP_ID) or "").strip()
        key_path = (values.get(ENV_PRIVATE_KEY_PATH) or "").strip() or None
        key_pem = values.get(ENV_PRIVATE_KEY)
        api_base_url = (values.get(ENV_API_URL) or "").strip() or DEFAULT_API_BASE_URL

        private_key = load_private_key(private_key_path=key_path, private_key_pem=key_pem)
        return cls(
            app_id=app_id,
            private_key_pem=private_key,
            api_base_url=api_base_url,
            **overrides,
        )

    @property
    def app_id(self) -> str:
        """The configured App id (not a secret)."""
        return self._app_id

    def app_jwt(self, *, now: datetime | None = None) -> str:
        """Mint a fresh app JWT.

        Not cached: an app JWT is valid for ten minutes and the token endpoint needs one
        per token request, which the cache keeps to roughly one per fifty minutes.
        """
        return create_app_jwt(
            self._app_id,
            self._private_key_pem,
            now=now or self._clock(),
        )

    async def get_installation_token(self, installation_id: int) -> str:
        """Return a valid installation access token, minting one only when needed.

        Cache semantics (spec section 9): a token lives 60 minutes and is refreshed
        after ~50, so a token is never used close to expiry.

        Concurrency: an :class:`asyncio.Lock` per installation means a burst of N
        concurrent jobs triggers exactly one token request; the other N-1 wait and then
        read the cached token. Without this lock, N jobs each mint a token, GitHub's
        token endpoint rate-limits them, and every one of those jobs fails.

        Args:
            installation_id: The GitHub App installation id from the webhook payload.

        Returns:
            A bearer token. Callers must not log it.

        Raises:
            GitHubAuthError: When ``installation_id`` is not a positive integer.
            GitHubTransientError: On transport failures or 5xx from GitHub.
            GitHubRateLimitError: When GitHub rate-limits the token endpoint.
        """
        if installation_id <= 0:
            raise GitHubAuthError(
                "installation id must be a positive integer",
                installation_id=installation_id,
            )

        async with self._lock_for(installation_id):
            cached = self._tokens.get(installation_id)
            if cached is not None and self._is_fresh(cached):
                return cached.token

            fresh = await self._request_installation_token(installation_id)
            self._tokens[installation_id] = fresh
            # No token value in this record: the log states that a token was obtained,
            # never what it was.
            logger.debug(
                "minted GitHub installation token (installation_id=%s, expires_at=%s)",
                installation_id,
                fresh.expires_at.isoformat(),
            )
            return fresh.token

    def cached_token_expires_at(self, installation_id: int) -> datetime | None:
        """Return the cached token's expiry, or ``None`` when nothing is cached.

        Exposed for diagnostics and tests; the token value itself is deliberately not
        exposed (use :meth:`get_installation_token`).
        """
        cached = self._tokens.get(installation_id)
        return cached.expires_at if cached is not None else None

    async def aclose(self) -> None:
        """Close the HTTP client if this authenticator owns it."""
        if self._owns_http_client:
            await self._http_client.aclose()

    async def __aenter__(self) -> GitHubAppAuthenticator:
        """Support ``async with GitHubAppAuthenticator(...) as auth``."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Close owned resources on context-manager exit."""
        await self.aclose()

    def _lock_for(self, installation_id: int) -> asyncio.Lock:
        """Return the per-installation lock, creating it on first use.

        Created lazily because an :class:`asyncio.Lock` binds to the running event loop
        when it is first awaited; constructing thousands of locks up front for
        installations that never appear would be wasted state. The get/set pair cannot
        race: no ``await`` occurs between them, and the event loop is single-threaded.
        """
        lock = self._locks.get(installation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[installation_id] = lock
        return lock

    def _is_fresh(self, cached: _CachedToken) -> bool:
        """Return ``True`` while a cached token is more than the margin from expiry."""
        return cached.expires_at - self._refresh_margin > self._clock()

    async def _request_installation_token(self, installation_id: int) -> _CachedToken:
        """Exchange an app JWT for an installation token.

        The request is made with the JWT, not with the token being fetched; the
        returned ``expires_at`` comes from GitHub when present and otherwise falls back
        to the documented 60-minute lifetime measured from the local clock.
        """
        token_path = _ACCESS_TOKEN_PATH_TEMPLATE.format(installation_id=installation_id)
        url = f"{self._api_base_url}{token_path}"
        headers = build_request_headers(
            token=self.app_jwt(),
            api_version=self._api_version,
            user_agent=self._user_agent,
        )

        try:
            response = await self._http_client.post(url, headers=headers)
        except httpx.TransportError as exc:
            raise GitHubTransientError(
                "could not reach GitHub to mint an installation token",
                installation_id=installation_id,
                error=type(exc).__name__,
            ) from exc

        raise_for_github_status(
            response,
            resource=f"app installation {installation_id} access token",
        )

        payload = response.json()
        token = payload.get("token") if isinstance(payload, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise GitHubAuthError(
                "GitHub returned no installation token",
                installation_id=installation_id,
            )

        expires_at_raw = payload.get("expires_at") if isinstance(payload, dict) else None
        expires_at = _parse_expiry(expires_at_raw) or (
            self._clock() + timedelta(seconds=INSTALLATION_TOKEN_LIFETIME_SECONDS)
        )
        return _CachedToken(token=token.strip(), expires_at=expires_at)


def _parse_expiry(raw: object) -> datetime | None:
    """Parse GitHub's ISO-8601 ``expires_at``, normalising a naive value to UTC."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed
