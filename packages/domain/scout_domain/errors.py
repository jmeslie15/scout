"""Domain exception hierarchy.

Rule 12 of the engineering policies: *do not hide failures behind generic
exception handling*. Every failure mode that the orchestrator must reason about
has a named exception type here, so that retry classification and operator
diagnosis never depend on string matching against an error message.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from scout_domain.enums import FailureClass

__all__ = [
    "ConfigurationError",
    "InvalidJobTransition",
    "PolicyViolation",
    "RepositoryConfigurationError",
    "ScoutDomainError",
    "ToolContractError",
    "TransitionGuardFailed",
]


class ScoutDomainError(Exception):
    """Base class for all Scout domain errors."""

    #: Retry classification for this error class. Orchestration reads this instead
    #: of guessing from the message (spec section 51).
    failure_class: FailureClass

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = context

    def __str__(self) -> str:
        if not self.context:
            return self.message
        rendered = ", ".join(f"{key}={value!r}" for key, value in sorted(self.context.items()))
        return f"{self.message} ({rendered})"


class InvalidJobTransition(ScoutDomainError):
    """Raised when a job state transition is not permitted.

    This covers both a structurally impossible edge (``QUEUED -> TESTING``) and a
    permitted edge whose guard is not satisfied (``INVESTIGATING -> READY_FOR_REVIEW``
    while questions remain open).
    """


class TransitionGuardFailed(InvalidJobTransition):
    """Raised when an edge exists but its precondition is not met."""


class RepositoryConfigurationError(ScoutDomainError):
    """Raised when ``.scout/engineering.yaml`` is invalid.

    Per spec section 14, invalid configuration must produce a clear finding rather
    than silently falling back to defaults, so this error carries structured
    validation detail for report generation.
    """


class ConfigurationError(ScoutDomainError):
    """Raised when Scout's own process configuration is invalid or incomplete."""


class PolicyViolation(ScoutDomainError):
    """Raised when an action would breach an engineering policy.

    Examples: an agent requesting a capability it was not granted, or a component
    attempting to record an AI claim as deterministic evidence.
    """


class ToolContractError(ScoutDomainError):
    """Raised when a tool returns a result that violates the tool result contract."""
