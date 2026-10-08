"""Orchestrator-specific failures.

Rule 12 says a failure must never be hidden, and spec section 51 says retryability is
decided by *exception type* rather than by message text. Both rules need a typed hierarchy
that the orchestrator's own code paths can raise, because "the queue is down" and "the
repository moved under us" must produce different behaviour and the difference has to be
readable by ``classify_exception``.

Every class here declares ``failure_class`` so that
:func:`scout_domain.state.retry.classify_exception` answers without a lookup table
elsewhere. The registration loop at the bottom is what makes the classification work for
callers that only import the domain layer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from scout_domain.enums import FailureClass, JobStatus
from scout_domain.errors import ScoutDomainError
from scout_domain.state.retry import register_failure_class

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "CheckRunPublicationError",
    "OrchestratorError",
    "PersistenceUnavailableError",
    "ReportDerivationError",
    "ResumeInvariantViolation",
    "StageExecutionError",
    "StageInfrastructureError",
    "WorkerProtocolError",
]


class OrchestratorError(ScoutDomainError):
    """Base class for orchestrator failures.

    Defaults to non-retryable: an unclassified orchestrator bug is not a licence to
    re-run a verification and consume another sandbox.
    """

    failure_class: FailureClass = FailureClass.NON_RETRYABLE

    def __init__(self, message: str, **context: Any) -> None:
        """Build the error, keeping structured context for the audit trail."""
        super().__init__(message, **context)


class ResumeInvariantViolation(OrchestratorError):
    """The job's status does not match the stage the orchestrator is about to run.

    This is the invariant :mod:`docs/ARCHITECTURE.md` section 4.1 calls out as missing
    from the domain layer: the state machine permits forward progress that *skips*
    resolved stages, so a job could legally sit in ``PREPARING`` while ``PREPARING`` and
    ``ANALYZING`` are both resolved. That is legal but not *derived*, and running the
    wrong stage is how analysis silently diverges from the recorded status.

    The orchestrator therefore asserts the equality itself and raises this error rather
    than guessing. It is deliberately non-retryable: a retry would reproduce the same
    divergence.
    """

    failure_class: FailureClass = FailureClass.NON_RETRYABLE


class StageExecutionError(OrchestratorError):
    """A pipeline stage could not be executed.

    Raised when the *platform* could not run a stage, as opposed to a stage that ran and
    reported a problem in the code under review. The distinction is what stops Scout from
    telling a developer their tests failed when the real answer is that the sandbox never
    started.
    """

    failure_class: FailureClass = FailureClass.NON_RETRYABLE

    def __init__(
        self,
        message: str,
        *,
        stage: JobStatus | None = None,
        **context: Any,
    ) -> None:
        """Record the stage the failure belongs to, when it is known."""
        super().__init__(message, stage=None if stage is None else stage.value, **context)
        self.stage = stage


class StageInfrastructureError(StageExecutionError):
    """A stage failed for a reason that is worth retrying.

    A sandbox runtime that is temporarily unavailable, a git fetch that hit a network
    blip: retrying is the correct response, and the retry policy is only allowed to make
    that decision because the type says so.
    """

    failure_class: FailureClass = FailureClass.RETRYABLE


class PersistenceUnavailableError(OrchestratorError):
    """The datastore could not be reached for a short, retryable operation.

    Raised while reserving messages or sweeping leases, where retrying is safe and
    desirable: the work has not started, so no partial state exists to reconcile.
    """

    failure_class: FailureClass = FailureClass.RETRYABLE


class ReportDerivationError(OrchestratorError):
    """The derived recommendation was rejected by the report's own gates.

    This is the failure mode that protects spec section 2: if the derivation produces an
    inconsistent recommendation, the fix belongs in the derivation, never in a looser
    model. Raising here makes that a loud development-time error instead of a report that
    overstates its own confidence.
    """

    failure_class: FailureClass = FailureClass.NON_RETRYABLE


class CheckRunPublicationError(OrchestratorError):
    """Publishing the GitHub check run failed.

    Classified non-retryable by default because the underlying GitHub error types carry
    their own classification (transport errors are retryable, authorization errors are
    not) and those are what ``classify_exception`` will see; this type exists for the
    cases where the *rendering* failed and no request was made at all.
    """

    failure_class: FailureClass = FailureClass.NON_RETRYABLE


class WorkerProtocolError(OrchestratorError):
    """A message or result crossed the control/execution boundary malformed.

    Deliberately non-retryable: a protocol violation means two components disagree about
    the contract, and redelivering the message cannot reconcile that.
    """

    failure_class: FailureClass = FailureClass.NON_RETRYABLE


def _register(types: Iterable[type[BaseException]]) -> None:
    """Register each error's declared failure class with the retry policy."""
    for error_type in types:
        register_failure_class(error_type, error_type.failure_class)  # type: ignore[attr-defined]


_register(
    (
        OrchestratorError,
        ResumeInvariantViolation,
        StageExecutionError,
        StageInfrastructureError,
        PersistenceUnavailableError,
        ReportDerivationError,
        CheckRunPublicationError,
        WorkerProtocolError,
    )
)
