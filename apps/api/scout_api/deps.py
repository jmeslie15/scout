"""FastAPI dependencies: the wiring between HTTP handlers and the platform's components.

Every dependency is created once per process (engine, session factory, artifact store,
GitHub client) and disposed on shutdown. Nothing here reads a global mutable, so a test can
replace any single dependency with ``app.dependency_overrides`` without touching the rest.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from scout_api.settings import Settings, get_settings
from scout_observability import get_logger
from scout_persistence.session import create_engine_from_settings, create_session_factory
from scout_persistence.unit_of_work import UnitOfWork

if TYPE_CHECKING:
    # Only the dataclass field annotations below reference these, and a dataclass never
    # evaluates its annotations at runtime. The SQLAlchemy types above ARE imported at
    # runtime because they appear inside a FastAPI dependency annotation, which FastAPI must
    # evaluate on every request.
    from scout_github.client import GitHubClient
    from scout_tools.artifacts import ArtifactStore

__all__ = [
    "AppState",
    "SettingsDep",
    "UowDep",
    "app_state",
    "get_app_state",
    "get_session_factory",
    "get_uow",
    "settings_dependency",
]

_log = get_logger(__name__)


@dataclass
class AppState:
    """Long-lived components owned by the application process.

    Held on ``app.state`` rather than in module globals so that two applications (for
    example two tests, or a test and the real app) cannot share a connection pool.
    """

    settings: Settings
    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    artifacts: ArtifactStore
    github: GitHubClient
    #: The job transport. Shared with the orchestrator via ``scout_persistence.queue``.
    queue: object | None = None
    #: Raw Redis client for readiness probing; created lazily.
    redis: object | None = None


def get_app_state(request: Request) -> AppState:
    """Return the application state, or fail loudly if the lifespan did not run.

    Handlers reach this through :func:`app_state` rather than through a ``Depends`` alias.
    ``AppState`` is a plain dataclass holding an engine and a client, so it has no OpenAPI
    schema; declaring it as a dependency annotation makes FastAPI try to build one and fail
    with an opaque Pydantic "type is not fully defined" error that names an unrelated model.
    Resolving it inside the handler keeps the type out of the schema entirely.
    """
    state: AppState | None = getattr(request.app.state, "scout", None)
    if state is None:  # pragma: no cover - a wiring bug, not a runtime condition
        raise RuntimeError("application state is not initialised; the lifespan context did not run")
    return state


def app_state(request: Request) -> AppState:
    """Resolve the application state for a handler that declared ``request: Request``."""
    return get_app_state(request)


def get_settings_dependency() -> Settings:
    return get_settings()


settings_dependency = Depends(get_settings_dependency)
app_state_dependency = Depends(get_app_state)


def get_session_factory(
    state: Annotated[AppState, app_state_dependency],
) -> async_sessionmaker[AsyncSession]:
    return state.session_factory


async def get_uow(
    session_factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> AsyncIterator[UnitOfWork]:
    """Yield a unit of work scoped to one HTTP request.

    The commit happens on clean exit of the block, so a handler that returns a response has
    already durably recorded its writes. A handler that raises rolls back and the exception
    propagates (Rule 12 — the unit of work never swallows a failure).
    """
    async with UnitOfWork(session_factory) as uow:
        yield uow


#: Convenience aliases so router signatures stay readable.
#:
#: These deliberately wrap only *runtime-resolvable* types. An alias such as
#: ``Annotated["GitHubClient", Depends(...)]`` looks harmless, but with
#: ``from __future__ import annotations`` FastAPI must evaluate the annotation at request
#: time, and a name that exists only under ``TYPE_CHECKING`` makes Pydantic fail with an
#: opaque "type is not fully defined" error -- on an unrelated model, which is what made this
#: worth a comment. Reaching the GitHub client or the artifact store goes through
#: :class:`AppState` instead.
UowDep = Annotated[UnitOfWork, Depends(get_uow)]
SettingsDep = Annotated[Settings, Depends(get_settings_dependency)]


@lru_cache(maxsize=1)
def build_engine_and_factory() -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """Build the process-wide engine and session factory.

    Kept as a function so the API and the orchestrator can share the same construction
    path, and so tests can build an isolated pool on demand.
    """
    settings = get_settings()
    engine = create_engine_from_settings(settings.database_url)
    return engine, create_session_factory(engine)
