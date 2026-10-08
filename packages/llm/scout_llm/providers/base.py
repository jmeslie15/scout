"""The provider interface and the shared HTTP plumbing (Rule 2).

``LLMProvider`` is the explicit, substitutable interface the whole gateway is built on.
``BaseHTTPProvider`` is the shared implementation detail for the two real vendors: one
``httpx.AsyncClient`` per provider instance, one place that maps HTTP status codes to typed
errors, and one place that scrubs credentials out of anything retained.

**No retries live here.** A provider performs exactly one HTTP request per ``complete``
call. Retry, backoff, and attempt accounting belong to the gateway (spec section 51), so
that a retry is visible in ``ModelRequestRecord`` rather than hidden inside a transport
client.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Protocol, cast, runtime_checkable

import httpx

from scout_llm.errors import (
    LLMAuthenticationError,
    LLMContextTooLargeError,
    LLMGatewayError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMResponseFormatError,
    LLMTimeoutError,
    LLMTransientError,
)
from scout_llm.redaction import redact_headers, redact_payload, redact_text
from scout_llm.types import Completion, CompletionRequest

__all__ = [
    "BaseHTTPProvider",
    "HTTPJSONResult",
    "LLMProvider",
    "as_int",
    "as_str",
    "parse_retry_after",
]

#: How much of a provider error body to keep in an exception. Enough to diagnose, small
#: enough that a stray HTML error page cannot flood a log or a database column.
_MAX_DETAIL_CHARS = 500

#: Exclusive upper bound of the 5xx range. Written as a named constant rather than a bare
#: ``600`` so the intent ("every server error") is readable.
_SERVER_ERROR_RANGE_END = 600


@runtime_checkable
class LLMProvider(Protocol):
    """A model provider Scout can talk to.

    Implementations exist for OpenAI, Anthropic, and the offline ``fake`` provider. Nothing
    outside ``scout_llm`` may call a vendor API directly (Rule 2), and nothing outside
    ``scout_llm`` may instantiate one of these either — the factory owns construction.
    """

    #: Stable provider name, used as the dictionary key and recorded on every request.
    name: str

    async def complete(self, request: CompletionRequest, *, model: str) -> Completion:
        """Perform exactly one completion request.

        Implementations must not retry. They must raise one of the typed
        :mod:`scout_llm.errors` exceptions so the gateway can classify the failure.
        """
        ...

    async def aclose(self) -> None:
        """Release transport resources. Safe to call more than once."""
        ...

    def supports(self, model: str) -> bool:
        """Return whether this provider can serve ``model``."""
        ...


@dataclass(frozen=True, slots=True)
class HTTPJSONResult:
    """A successful JSON HTTP response, with what is needed to retain a safe record."""

    body: dict[str, Any]
    status_code: int
    latency_seconds: float
    request_headers: dict[str, str] = field(default_factory=dict)


class BaseHTTPProvider(ABC):
    """Shared lifecycle and error mapping for HTTP-based providers.

    Subclasses provide :meth:`default_headers` and :meth:`complete`; they inherit a
    correctly-scoped client, uniform status-code mapping, and credential scrubbing.
    """

    #: Overridden by each concrete provider.
    name: str = "http"

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_seconds: float = 120.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not api_key:
            # Fail at construction rather than on the first call: a provider with no
            # credentials can only ever produce an authentication error.
            raise LLMAuthenticationError(
                f"{self.name} provider requires an API key; set it in the environment "
                f"or pass api_key explicitly",
                provider=self.name,
            )
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        # An injected client belongs to the caller (usually a test harness with respx
        # mounted); an owned client is closed by ``aclose``. Mixing the two would either
        # close a client someone else is still using or leak sockets.
        self._owns_client = client is None
        self._client = client if client is not None else httpx.AsyncClient(timeout=timeout_seconds)

    @property
    def base_url(self) -> str:
        """The root against which request paths are resolved."""
        return self._base_url

    @property
    def client(self) -> httpx.AsyncClient:
        """The underlying HTTP client. Exposed so tests can assert on its configuration."""
        return self._client

    @abstractmethod
    def default_headers(self) -> dict[str, str]:
        """Return the authentication and version headers for this provider."""

    @abstractmethod
    async def complete(self, request: CompletionRequest, *, model: str) -> Completion:
        """Perform exactly one completion request."""

    def supports(self, model: str) -> bool:
        """Whether this provider can serve ``model``.

        Any non-empty model identifier is accepted: vendor catalogues change faster than
        Scout releases, and routing is explicit configuration (``ModelTarget``) rather
        than capability discovery. An unknown model fails at the vendor with a clear
        ``LLMInvalidRequestError`` instead of being refused locally on a stale list.
        """
        return bool(model.strip())

    async def aclose(self) -> None:
        """Close the client this provider owns; leave an injected client alone."""
        if self._owns_client:
            await self._client.aclose()

    async def _post_json(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> HTTPJSONResult:
        """POST ``payload`` as JSON and return the decoded body.

        Raises:
            LLMTimeoutError: the request exceeded the configured timeout.
            LLMTransientError: a connection/transport failure.
            LLMAuthenticationError, LLMInvalidRequestError, LLMRateLimitError,
            LLMContextTooLargeError: the provider answered with an error status.
            LLMResponseFormatError: the body was not a JSON object.
        """
        url = f"{self._base_url}{path}"
        headers = {**self.default_headers(), "content-type": "application/json"}
        if extra_headers:
            headers.update(extra_headers)

        started = time.perf_counter()
        try:
            response = await self._client.post(
                url,
                json=dict(payload),
                headers=headers,
                timeout=self._timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(
                f"{self.name} did not respond within {self._timeout_seconds:g}s",
                provider=self.name,
                url=url,
                timeout_seconds=self._timeout_seconds,
            ) from exc
        except httpx.TransportError as exc:
            raise LLMTransientError(
                f"{self.name} transport failure: {type(exc).__name__}",
                provider=self.name,
                url=url,
            ) from exc
        except httpx.HTTPError as exc:
            raise LLMTransientError(
                f"{self.name} HTTP layer failure: {type(exc).__name__}",
                provider=self.name,
                url=url,
            ) from exc
        latency = time.perf_counter() - started

        body = self._decode_body(response)
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise self._error_for_response(response, body)

        if not isinstance(body, dict):
            raise LLMResponseFormatError(
                f"{self.name} returned a successful response that is not a JSON object",
                provider=self.name,
                status_code=response.status_code,
                raw_text=_truncate(_safe_text(response)),
            )

        return HTTPJSONResult(
            body=cast("dict[str, Any]", body),
            status_code=response.status_code,
            latency_seconds=latency,
            request_headers=dict(headers),
        )

    def raw_payload(self, result: HTTPJSONResult) -> dict[str, Any]:
        """Build the scrubbed debugging payload retained on :class:`Completion`.

        The response body is kept for diagnostics, the request headers are kept with
        credentials replaced, and the API key is additionally substituted out of the body
        in case a provider echoed it back.
        """
        return {
            "response": redact_payload(result.body, secrets=(self._api_key,)),
            "request_headers": redact_headers(result.request_headers),
            "status_code": result.status_code,
        }

    def _decode_body(self, response: httpx.Response) -> Any:
        """Decode a JSON body, tolerating an empty or non-JSON error response."""
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None

    def _detects_context_overflow(self, *, status_code: int, body: Any) -> bool:
        """Whether an error response means "the prompt did not fit".

        Default is ``False``; providers override with the vendor's documented signal. This
        only chooses a *better error type* — both outcomes are non-retryable, so a wrong
        answer here cannot cause a retry storm.
        """
        del status_code, body  # the base implementation has no vendor signal to inspect
        return False

    def _error_for_response(self, response: httpx.Response, body: Any) -> LLMGatewayError:
        """Map an HTTP error status onto the typed error hierarchy."""
        status = response.status_code
        # A provider is free to echo a credential back inside an error body, and this text
        # ends up in logs and in ModelRequestRecord.error, so it is scrubbed here.
        detail = _truncate(redact_text(_safe_text(response), secrets=(self._api_key,)))
        context: dict[str, Any] = {
            "provider": self.name,
            "status_code": status,
            "detail": detail,
        }

        if self._detects_context_overflow(status_code=status, body=body):
            # The provider identified a context overflow but did not report token counts
            # we could trust, so they are recorded as unknown (zero) rather than guessed.
            return LLMContextTooLargeError(
                f"{self.name} rejected the prompt because it exceeds the model context "
                f"window (HTTP {status})",
                input_tokens=0,
                max_input_tokens=0,
                **context,
            )

        if status in {httpx.codes.UNAUTHORIZED, httpx.codes.FORBIDDEN}:
            return LLMAuthenticationError(
                f"{self.name} rejected Scout's credentials (HTTP {status})",
                **context,
            )

        if status == httpx.codes.TOO_MANY_REQUESTS:
            return LLMRateLimitError(
                f"{self.name} rate-limited Scout (HTTP {status})",
                retry_after_seconds=parse_retry_after(response.headers.get("retry-after")),
                **context,
            )

        if httpx.codes.INTERNAL_SERVER_ERROR <= status < _SERVER_ERROR_RANGE_END:
            return LLMTransientError(
                f"{self.name} server error (HTTP {status})",
                **context,
            )

        return LLMInvalidRequestError(
            f"{self.name} rejected the request (HTTP {status})",
            **context,
        )


def parse_retry_after(value: str | None) -> float | None:
    """Parse a ``retry-after`` header into seconds.

    RFC 9110 allows either a delay in seconds or an HTTP-date. Both are accepted because
    providers use both, and an unparseable value yields ``None`` (fall back to Scout's own
    exponential backoff) rather than a bogus zero.
    """
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        return None

    try:
        return max(0.0, float(candidate))
    except ValueError:
        pass

    try:
        when = parsedate_to_datetime(candidate)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


def _safe_text(response: httpx.Response) -> str:
    """Return the response body as text, never raising on a broken encoding."""
    try:
        return response.text
    except (UnicodeDecodeError, httpx.DecodingError):  # pragma: no cover - defensive
        return "<undecodable response body>"


def _truncate(text: str) -> str:
    """Clamp ``text`` to :data:`_MAX_DETAIL_CHARS`, marking the truncation."""
    if len(text) <= _MAX_DETAIL_CHARS:
        return text
    return text[:_MAX_DETAIL_CHARS] + "...[truncated]"


def as_int(value: Any) -> int:
    """Coerce a provider-supplied field to a non-negative integer.

    Providers omit usage fields, send ``null``, or send a float. None of those is a reason
    to fail a call that otherwise succeeded, so an unusable value becomes ``0`` and shows
    up in accounting as "unknown" rather than as a fabrication. ``bool`` is excluded
    explicitly because ``True`` is an ``int`` in Python and would silently become 1.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    return 0


def as_str(value: Any) -> str | None:
    """Return ``value`` when it is a string, else ``None``."""
    return value if isinstance(value, str) else None
