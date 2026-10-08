"""Scout's execution plane: checkout, sandboxed tools, and stage results.

The worker is deliberately the least privileged long-lived process in Scout. It holds Scout's own
datastore and GitHub credentials because it persists the results it produces, and repository code
running under it holds neither -- the sandbox environment allowlist enforces that, not this package.

The public surface is :class:`~scout_worker.worker.Worker`, which consumes one stage message at a
time and never decides a job status.
"""

from __future__ import annotations

from scout_worker.errors import WorkerError, WorkerProtocolError
from scout_worker.executor import StageExecutor
from scout_worker.settings import Settings, get_settings
from scout_worker.worker import Worker

__version__ = "0.1.0"

__all__ = [
    "Settings",
    "StageExecutor",
    "Worker",
    "WorkerError",
    "WorkerProtocolError",
    "__version__",
    "get_settings",
]
