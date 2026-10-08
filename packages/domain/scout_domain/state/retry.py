"""Retry classification and backoff (spec section 51).

Rules encoded here:

* Retryable: network timeouts, temporary GitHub failures, temporary model-provider
  failures, worker crashes.
* Non-retryable: invalid repository, invalid configuration, authentication failure,
  compilation failure, test failure.
* Every retry uses exponential backoff and is recorded.

Classification is attached to the *exception type*, not inferred from the message,
so a wording change can never silently turn a permanent failure into a retry storm.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from scout_domain.enums import FailureClass

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "DEFAULT_RETRY_POLICY",
    "RetryPolicy",
    "classify_exception",
    "register_failure_class",
]


class _TransientScoutError(Exception):
    """Marker for Scout errors that are safe to retry.

    Concrete error types register themselves in :data:`_FAILURE_CLASSES`; this class
    only exists so that third-party exceptions can opt in without Scout importing them.
    """


#: Explicit exception-type to failure-class registry. Populated by the packages that
#: own the relevant error types (GitHub transport, LLM gateway, sandbox) so that the
#: domain layer never depends on those packages.
_FAILURE_CLASSES: dict[type[BaseException], FailureClass] = {
    # Built-in transient conditions. Anything not listed defaults to UNKNOWN, which
    # the policy treats as non-retryable: an unknown failure is not a licence to retry.
    TimeoutError: FailureClass.RETRYABLE,
    ConnectionError: FailureClass.RETRYABLE,
    ConnectionResetError: FailureClass.RETRYABLE,
    BrokenPipeError: FailureClass.RETRYABLE,
    OSError: FailureClass.RETRYABLE,
    _TransientScoutError: FailureClass.RETRYABLE,
}


def register_failure_class(
    exception_type: type[BaseException],
    failure_class: FailureClass,
) -> None:
    """Declare the retry classification of an exception type.

    Called at import time by the owning package, e.g. ``scout_github`` marking its
    transport errors retryable and its authorization errors non-retryable.
    """
    _FAILURE_CLASSES[exception_type] = failure_class


def failure_class_map() -> Mapping[type[BaseException], FailureClass]:
    """Expose the registry read-only, primarily for diagnostics and tests."""
    return dict(_FAILURE_CLASSES)


def classify_exception(exc: BaseException) -> FailureClass:
    """Return the retry classification for ``exc``.

    The most specific registered base class wins; unregistered types are ``UNKNOWN``,
    which the retry policy treats as non-retryable (fail closed).
    """
    from scout_domain.errors import ScoutDomainError  # local import avoids a cycle

    for klass in type(exc).__mro__:
        registered = _FAILURE_CLASSES.get(klass)
        if registered is not None:
            return registered

    # Domain errors declare their own classification as a class attribute.
    if isinstance(exc, ScoutDomainError):
        declared = getattr(type(exc), "failure_class", None)
        if isinstance(declared, FailureClass):
            return declared

    # Third-party exceptions opt in by setting a class attribute, so Scout can classify an
    # httpx or redis error without importing those libraries into the domain layer.
    declared = getattr(exc, "__scout_failure_class__", None)
    if isinstance(declared, FailureClass):
        return declared

    return FailureClass.UNKNOWN


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Exponential backoff with bounded jitter (spec section 51).

    Jitter matters because Scout fans several jobs out at once against the same
    upstream (GitHub, a model provider). Without jitter, backoff synchronises and
    produces a thundering herd on recovery.
    """

    max_attempts: int = 3
    base_delay_seconds: float = 2.0
    multiplier: float = 2.0
    max_delay_seconds: float = 300.0
    jitter_ratio: float = 0.15

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay_seconds <= 0:
            raise ValueError("base_delay_seconds must be positive")
        if self.multiplier < 1:
            raise ValueError("multiplier must be at least 1")
        if self.max_delay_seconds < self.base_delay_seconds:
            raise ValueError("max_delay_seconds must be >= base_delay_seconds")
        if not 0.0 <= self.jitter_ratio < 1.0:
            raise ValueError("jitter_ratio must be in [0, 1)")

    def should_retry(self, *, attempt: int, failure_class: FailureClass) -> bool:
        """Decide whether to retry after ``attempt`` (1-based) failed."""
        if failure_class is not FailureClass.RETRYABLE:
            return False
        return attempt < self.max_attempts

    def delay_for_attempt(self, attempt: int, *, jitter: float | None = None) -> float:
        """Return the backoff delay in seconds before retry number ``attempt + 1``.

        Args:
            attempt: The 1-based attempt that just failed.
            jitter: Deterministic jitter in ``[-1, 1]``; drawn randomly when omitted.
                Supplying it makes tests reproducible.
        """
        if attempt < 1:
            raise ValueError("attempt must be >= 1")

        raw = self.base_delay_seconds * (self.multiplier ** (attempt - 1))
        capped = min(raw, self.max_delay_seconds)

        if self.jitter_ratio == 0.0:
            return capped

        draw = random.uniform(-1.0, 1.0) if jitter is None else max(-1.0, min(1.0, jitter))
        return max(0.0, capped * (1.0 + self.jitter_ratio * draw))

    def retries_remaining(self, *, attempt: int) -> int:
        """Number of retries still available after ``attempt`` attempts."""
        return max(0, self.max_attempts - attempt)


#: Scout's default: three attempts, two-second base, exponential to five minutes.
DEFAULT_RETRY_POLICY: Final[RetryPolicy] = RetryPolicy()
