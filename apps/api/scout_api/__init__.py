"""Scout's control-plane HTTP API (spec section 8.1).

The API receives GitHub webhooks, owns job creation and status queries, records human
approvals, and serves the dashboard. It is deliberately **not** able to execute repository
commands: the only path from the API to untrusted code is by enqueueing a job for the
orchestrator, which schedules a sandboxed worker. That separation is what keeps a
compromised or buggy HTTP handler from becoming remote code execution.
"""

from __future__ import annotations

__version__ = "0.1.0"

__all__ = ["__version__", "create_app"]


def create_app() -> object:
    """Build the API application.

    Imported lazily so that ``import scout_api`` does not construct a FastAPI app, read
    settings, or open a database pool. A module import must stay side-effect free.
    """
    from scout_api.main import create_app as _create_app

    return _create_app()
