"""Worker-specific failures.

The worker's failure surface is small because its responsibilities are small: it executes one
stage and records the result. When it does fail, the distinction that matters is whether the
failure is the *platform's* (retryable) or the *contract's* (not), and that distinction is declared
here as an exception type so ``classify_exception`` can read it without inspecting a message.
"""

from __future__ import annotations

from typing import Any

from scout_domain.enums import FailureClass
from scout_domain.errors import ScoutDomainError
from scout_domain.state.retry import register_failure_class

__all__ = ["WorkerError", "WorkerProtocolError"]


class WorkerError(ScoutDomainError):
    """Base class for worker failures. Non-retryable by default.

    An unclassified worker bug is not a licence to re-run repository code: the default has to be
    "stop", because the alternative is a retry loop that consumes sandbox capacity forever.
    """

    failure_class: FailureClass = FailureClass.NON_RETRYABLE

    def __init__(self, message: str, **context: Any) -> None:
        """Build the error, keeping structured context for the record."""
        super().__init__(message, **context)


class WorkerProtocolError(WorkerError):
    """The orchestrator and the worker disagree about what to do.

    Raised when a dispatched stage does not match the job's recorded status. The orchestrator
    advances the status *before* dispatching precisely so this check is meaningful; a mismatch
    means one of the two is running stale code or stale state. Deliberately non-retryable --
    redelivering the message cannot reconcile a contract disagreement -- and deliberately loud,
    because executing a stage the job's own status contradicts would produce a report that
    disagrees with its lifecycle.
    """

    failure_class: FailureClass = FailureClass.NON_RETRYABLE


for _error in (WorkerError, WorkerProtocolError):
    register_failure_class(_error, _error.failure_class)
del _error
