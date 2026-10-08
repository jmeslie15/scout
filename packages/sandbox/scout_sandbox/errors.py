"""Sandbox error types and retry classification.

An infrastructure failure in the sandbox — the container runtime is down, the image is
missing — is *not* a defect in the code under review. Keeping that distinction in the
type system is what stops Scout from reporting "your tests failed" when the real answer
is "Scout could not start Docker" (spec section 51, and Rule 12).
"""

from __future__ import annotations

from scout_domain.enums import FailureClass
from scout_domain.errors import ScoutDomainError
from scout_domain.state.retry import register_failure_class

__all__ = [
    "SandboxBackendUnavailable",
    "SandboxCommandError",
    "SandboxError",
    "SandboxLimitExceeded",
    "SandboxTimeout",
]


class SandboxError(ScoutDomainError):
    """Base class for sandbox failures.

    ``infrastructure_failure`` distinguishes "the platform broke" from "the code is
    broken". Callers must not turn the former into a finding about the pull request.
    """

    failure_class: FailureClass = FailureClass.NON_RETRYABLE
    infrastructure_failure: bool = True


class SandboxBackendUnavailable(SandboxError):
    """The configured sandbox backend cannot be used (runtime missing, image absent)."""

    failure_class = FailureClass.RETRYABLE


class SandboxTimeout(SandboxError):
    """A command exceeded its wall-clock budget and was terminated."""

    failure_class = FailureClass.RETRYABLE


class SandboxLimitExceeded(SandboxError):
    """The sandbox was killed for exceeding a CPU, memory, or process ceiling."""

    failure_class = FailureClass.RETRYABLE


class SandboxCommandError(SandboxError):
    """The command could not be executed at all (binary missing, permission denied).

    Note this is distinct from a command that ran and returned a non-zero exit code. A
    non-zero exit from ``pytest`` is a result, not an error; a missing ``pytest`` binary
    is an error.
    """

    infrastructure_failure = True


for _error in (
    SandboxError,
    SandboxBackendUnavailable,
    SandboxTimeout,
    SandboxLimitExceeded,
    SandboxCommandError,
):
    register_failure_class(_error, _error.failure_class)
del _error
