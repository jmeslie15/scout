"""Correlation context propagated through every layer of Scout.

A verification job passes through a webhook handler, a queue, an orchestrator, a worker,
a sandbox, several tools, and several model calls. Without a shared correlation context,
a failure anywhere in that chain is nearly impossible to diagnose from logs alone.

``contextvars`` are the right mechanism here rather than thread-locals or explicit
parameter threading: they follow ``asyncio`` tasks automatically, so a background task
spawned inside a job inherits the job's identity without every function in between having
to accept and forward a context object.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token
from typing import Any, Final

__all__ = [
    "bind_context",
    "clear_context",
    "current_context",
    "get_correlation_id",
    "get_job_id",
    "job_context",
]

#: Keys that every log line should carry once they are set.
#:
#: The default is ``None`` rather than ``{}`` on purpose: a mutable default on a ContextVar is
#: shared by every task that never bound a context, so one accidental in-place mutation would
#: leak correlation identity between unrelated jobs. Callers always go through
#: :func:`current_context`, which materialises a fresh dict.
_CONTEXT: Final[ContextVar[dict[str, Any] | None]] = ContextVar("scout_context", default=None)


def current_context() -> dict[str, Any]:
    """Return a copy of the currently bound correlation context."""
    return dict(_CONTEXT.get() or {})


def bind_context(**values: Any) -> Token[dict[str, Any] | None]:
    """Merge ``values`` into the current context and return a reset token.

    Prefer :func:`job_context` in application code; this lower-level function exists for
    cases that need to merge a single field (for example tagging a stage).
    """
    merged = {
        **(_CONTEXT.get() or {}),
        **{key: value for key, value in values.items() if value is not None},
    }
    return _CONTEXT.set(merged)


def clear_context() -> None:
    """Reset the context entirely.

    Called at the start of each unit of work (an HTTP request, a queue message, a test)
    so that identity never leaks between unrelated jobs — leaking a ``job_id`` into an
    unrelated log line is worse than having no ``job_id`` at all.
    """
    _CONTEXT.set(None)


@contextmanager
def job_context(**values: Any) -> Iterator[dict[str, Any]]:
    """Bind ``values`` for the duration of the ``with`` block.

    Example::

        with job_context(job_id=job.id, correlation_id=job.correlation_id):
            ...
    """
    token = bind_context(**values)
    try:
        yield current_context()
    finally:
        _CONTEXT.reset(token)


def get_job_id() -> str | None:
    """Return the bound job id, if any."""
    value = (_CONTEXT.get() or {}).get("job_id")
    return str(value) if value is not None else None


def get_correlation_id() -> str | None:
    """Return the bound correlation id, if any.

    Falls back to the job id, because a job id is itself a stable correlation handle.
    """
    context: Mapping[str, Any] = _CONTEXT.get() or {}
    value = context.get("correlation_id") or context.get("job_id")
    return str(value) if value is not None else None
