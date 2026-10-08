"""Globally unique, sortable, prefixed identifiers.

Scout uses ULIDs (Universally Unique Lexicographically Sortable Identifiers) for
every externally visible identifier. ULIDs are preferred over UUIDv4 because they
sort by creation time, which keeps B-tree indexes dense and makes audit trails
readable without a join.

See spec section 42: "Use UUID/ULID-style identifiers rather than database-generated
sequential IDs for externally visible identifiers."

Identifier shape::

    job_01J8ZQ4K2M7X3N9PQRSTVWXY0
    ^^^ ^----------------------^
    |   |
    |   +-- 26 character Crockford base32 ULID
    +------ lowercase resource prefix

The prefix makes an identifier self-describing in logs, URLs, and agent context,
which materially reduces debugging time.
"""

from __future__ import annotations

import os
import threading
import time
from enum import StrEnum
from typing import Final

__all__ = [
    "CROCKFORD_ALPHABET",
    "ULID_LENGTH",
    "IdKind",
    "new_id",
    "new_ulid",
    "validate_ulid",
]

#: Crockford base32: no I, L, O, or U, so the alphabet cannot spell accidental words
#: and cannot be confused with 1/0 when transcribed by a human.
CROCKFORD_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

#: 48 bits of timestamp (10 chars) + 80 bits of randomness (16 chars).
ULID_LENGTH: Final = 26

_TIMESTAMP_CHARS: Final = 10
_RANDOM_CHARS: Final = ULID_LENGTH - _TIMESTAMP_CHARS
_MAX_TIMESTAMP_MS: Final = (1 << 48) - 1
_DECODE: Final = {char: index for index, char in enumerate(CROCKFORD_ALPHABET)}

# Monotonicity state. A ULID generated in the same millisecond as the previous one
# increments the random component instead of re-randomising, which guarantees that
# identifiers created in the same millisecond still sort in creation order.
_lock = threading.Lock()
_last_timestamp_ms: int = -1
_last_random: int = 0


class IdKind(StrEnum):
    """Resource kinds that carry a prefixed public identifier."""

    JOB = "job"
    STAGE = "stage"
    REPOSITORY = "repo"
    WEBHOOK_EVENT = "whr"
    AGENT_RUN = "arun"
    TOOL_RUN = "trun"
    FINDING = "find"
    QUESTION = "qst"
    ANSWER = "ans"
    DECISION = "dec"
    KNOWLEDGE_DOCUMENT = "kdoc"
    NOTIFICATION = "ntf"
    ARTIFACT = "art"
    SANDBOX_RUN = "srun"
    MODEL_REQUEST = "mreq"
    APPROVAL = "apv"
    AUDIT_EVENT = "aud"


def _random_bits() -> int:
    """Return 80 bits of cryptographic randomness."""
    return int.from_bytes(os.urandom(_RANDOM_CHARS * 5 // 8), "big")


def _encode(value: int, *, length: int) -> str:
    """Encode ``value`` as ``length`` Crockford base32 characters (big-endian)."""
    chars = ["0"] * length
    for index in range(length - 1, -1, -1):
        value, remainder = divmod(value, 32)
        chars[index] = CROCKFORD_ALPHABET[remainder]
    if value:
        raise OverflowError(f"value does not fit in {length} base32 characters")
    return "".join(chars)


def new_ulid(*, timestamp_ms: int | None = None) -> str:
    """Generate a monotonic, sortable 26-character ULID.

    Args:
        timestamp_ms: Override the current time. Only intended for tests that need
            deterministic ordering or a fixed clock.
    """
    global _last_timestamp_ms, _last_random  # noqa: PLW0603 - guarded by _lock

    now_ms = int(time.time() * 1000) if timestamp_ms is None else timestamp_ms
    if not 0 <= now_ms <= _MAX_TIMESTAMP_MS:
        raise ValueError(f"timestamp out of ULID range: {now_ms}")

    with _lock:
        if now_ms == _last_timestamp_ms:
            # Same millisecond: advance the random component to preserve ordering.
            _last_random += 1
            if _last_random >> (_RANDOM_CHARS * 5):
                # Random space exhausted inside a single millisecond (2**80 ids).
                # Borrow from the next millisecond rather than emitting a duplicate.
                now_ms += 1
                _last_timestamp_ms = now_ms
                _last_random = _random_bits()
        elif now_ms > _last_timestamp_ms:
            _last_timestamp_ms = now_ms
            _last_random = _random_bits()
        else:
            # Clock moved backwards (NTP correction). Keep generating forward from
            # the highest timestamp already observed so ordering stays monotonic.
            _last_random += 1
            now_ms = _last_timestamp_ms
            if _last_random >> (_RANDOM_CHARS * 5):
                _last_timestamp_ms += 1
                now_ms = _last_timestamp_ms
                _last_random = _random_bits()

        timestamp_part = _encode(now_ms, length=_TIMESTAMP_CHARS)
        random_part = _encode(_last_random, length=_RANDOM_CHARS)

    return timestamp_part + random_part


def new_id(kind: IdKind) -> str:
    """Generate a prefixed identifier for ``kind``, e.g. ``job_01J8ZQ4K2M7X3N9PQRSTVWXY0``."""
    return f"{kind.value}_{new_ulid()}"


def validate_ulid(value: str) -> bool:
    """Return ``True`` if ``value`` is a structurally valid 26-character ULID."""
    if len(value) != ULID_LENGTH:
        return False
    if any(char not in _DECODE for char in value.upper()):
        return False
    # Reject timestamps that overflow the 48-bit field.
    return int_from_ulid_timestamp(value) <= _MAX_TIMESTAMP_MS


def int_from_ulid_timestamp(value: str) -> int:
    """Decode the 48-bit millisecond timestamp encoded in a ULID."""
    if len(value) != ULID_LENGTH:
        raise ValueError(f"ULID must be {ULID_LENGTH} characters, got {len(value)}")
    timestamp = 0
    for char in value[:_TIMESTAMP_CHARS].upper():
        try:
            timestamp = timestamp * 32 + _DECODE[char]
        except KeyError as exc:  # pragma: no cover - defensive
            raise ValueError(f"invalid Crockford base32 character: {char!r}") from exc
    return timestamp
