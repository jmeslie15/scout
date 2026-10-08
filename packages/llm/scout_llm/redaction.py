"""Secret redaction (spec sections 38 and 55).

Rule: **no credential ever reaches a prompt, a log line, or a retained payload.** This
module is the single implementation of that rule for ``scout_llm``, so there is exactly one
place to audit and one place to fix.

It is used in three situations:

1. provider responses retained on :attr:`scout_llm.types.Completion.raw`;
2. request headers retained alongside that raw payload, for debugging;
3. error detail scraped out of a provider's error body before it becomes an exception
   message that will certainly end up in logs and in ``ModelRequestRecord.error``.

Redaction is *belt and braces*: sensitive header names are replaced wholesale, **and** the
known secret values are substituted out of any string, because a provider is free to echo a
key back inside an error message.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Final

__all__ = [
    "REDACTED",
    "SENSITIVE_HEADERS",
    "redact_headers",
    "redact_payload",
    "redact_text",
]

#: Replacement marker. Deliberately conspicuous so an operator can tell "removed" from
#: "never present".
REDACTED: Final = "***REDACTED***"

#: Header names whose values are credentials. Lower-cased because HTTP header names are
#: case-insensitive, and compared lower-cased for the same reason.
SENSITIVE_HEADERS: Final[frozenset[str]] = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api-key",
        "openai-api-key",
        "anthropic-api-key",
        "cookie",
        "set-cookie",
    }
)

#: Attribute/key names whose values are credentials, for payload redaction.
_SENSITIVE_KEYS: Final[frozenset[str]] = SENSITIVE_HEADERS | frozenset(
    {"api_key", "apikey", "api_token", "access_token", "secret", "password"}
)

#: Short secrets are not substituted: a one- or two-character "secret" would rewrite large
#: parts of the payload and destroy the debugging value of retaining it.
_MIN_SECRET_LENGTH: Final = 8

_MAX_DEPTH: Final = 20


def redact_text(text: str, *, secrets: Iterable[str] = ()) -> str:
    """Return ``text`` with every known secret replaced by :data:`REDACTED`.

    Args:
        text: Arbitrary text that may embed a credential.
        secrets: Credential values to remove. Values shorter than
            :data:`_MIN_SECRET_LENGTH` are ignored so that redaction cannot mangle the
            payload beyond usefulness.
    """
    redacted = text
    for secret in secrets:
        if secret and len(secret) >= _MIN_SECRET_LENGTH and secret in redacted:
            redacted = redacted.replace(secret, REDACTED)
    return redacted


def redact_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of ``headers`` with credential-bearing values replaced."""
    return {
        name: (REDACTED if name.lower() in SENSITIVE_HEADERS else value)
        for name, value in headers.items()
    }


def redact_payload(
    value: Any,
    *,
    secrets: Iterable[str] = (),
    _depth: int = 0,
) -> Any:
    """Recursively redact a JSON-shaped payload.

    Mappings are rebuilt with sensitive keys replaced, sequences are walked, and strings
    are passed through :func:`redact_text`. Depth is bounded so a cyclic or absurdly deep
    payload cannot hang the gateway while it is trying to write a log line.
    """
    if _depth >= _MAX_DEPTH:
        return REDACTED

    secret_list = tuple(secrets)

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_item in value.items():
            key = raw_key if isinstance(raw_key, str) else str(raw_key)
            if key.lower() in _SENSITIVE_KEYS:
                result[key] = REDACTED
            else:
                result[key] = redact_payload(raw_item, secrets=secret_list, _depth=_depth + 1)
        return result

    if isinstance(value, str):
        return redact_text(value, secrets=secret_list)

    if isinstance(value, (list, tuple)):
        return [redact_payload(item, secrets=secret_list, _depth=_depth + 1) for item in value]

    return value
