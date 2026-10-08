"""Async engine, session factory, and readiness probe.

One rule shapes this module: **the caller decides where a transaction ends.** A
repository method must never commit, and an HTTP handler must never see a session it
cannot roll back. That is why the engine factory is separate from the scope helper,
why the session factory disables ``expire_on_commit``, and why nothing here catches
an exception except to roll back and re-raise it (Rule 12: never hide a failure).

``expire_on_commit=False`` is not a micro-optimisation. With the default, every
attribute access after a commit triggers a lazy refresh, which in async SQLAlchemy
either raises ``MissingGreenlet`` or emits an implicit round trip after the caller
believes the unit of work has finished. Keeping loaded state usable after commit
removes an entire class of "it worked in the test, it hung in the API" bugs.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

__all__ = [
    "DEFAULT_MAX_OVERFLOW",
    "DEFAULT_POOL_RECYCLE_SECONDS",
    "DEFAULT_POOL_SIZE",
    "EngineCleanup",
    "async_session_scope",
    "check_database",
    "create_engine_from_settings",
    "create_session_factory",
    "dispose_engine",
]

#: Pool sizing for the single-host deployment described in ADR 0001: a handful of
#: long-lived processes, each with a bounded worker count. ``max_overflow`` is
#: deliberately larger than ``pool_size`` because a request that waits for a
#: connection is a latency spike, whereas an extra connection is cheap.
DEFAULT_POOL_SIZE: Final = 5
DEFAULT_MAX_OVERFLOW: Final = 10
#: Recycle below the usual 1-hour server-side idle timeout so Scout never hands out
#: a connection the server has already closed. ``pool_pre_ping`` catches the rest.
DEFAULT_POOL_RECYCLE_SECONDS: Final = 1_800


def _asyncpg_connect_args(statement_cache_size: int) -> dict[str, Any]:
    """Return asyncpg connection arguments applied to every new connection.

    Prepared-statement caching is disabled because Scout runs behind PgBouncer-style
    poolers in some deployments, where asyncpg's statement cache produces
    ``prepared statement already exists`` failures. The cost is a re-plan per query,
    which is negligible next to the network round trip it protects.
    """
    return {"statement_cache_size": statement_cache_size}


def create_engine_from_settings(
    database_url: str,
    *,
    pool_size: int | None = DEFAULT_POOL_SIZE,
    max_overflow: int = DEFAULT_MAX_OVERFLOW,
    pool_recycle: int = DEFAULT_POOL_RECYCLE_SECONDS,
    pool_pre_ping: bool = True,
    echo: bool = False,
    statement_cache_size: int = 0,
    connect_args: dict[str, Any] | None = None,
) -> AsyncEngine:
    """Build the process-wide async engine for ``database_url``.

    The URL is passed in rather than read from the environment here so that the API,
    the orchestrator, the worker, and every test all construct their engine through
    one code path while still owning their own configuration. Nothing in this module
    reads ``SCOUT_DATABASE_URL``; the caller does, once, at start-up.

    ``pool_size=None`` selects a ``NullPool``, which is what a short-lived script or a
    test that creates and drops schemas wants: no connection survives the event loop.
    """
    args: dict[str, Any] = _asyncpg_connect_args(statement_cache_size)
    if connect_args:
        args.update(connect_args)

    options: dict[str, Any] = {
        "echo": echo,
        "pool_pre_ping": pool_pre_ping,
        "connect_args": args,
        "future": True,
    }
    if pool_size is None:
        # NullPool: every checkout opens a fresh connection and closes it on release.
        # Short-lived scripts and schema-per-test fixtures want exactly this, because a
        # pooled connection would outlive the schema it was pinned to.
        options["poolclass"] = NullPool
    else:
        options["pool_size"] = pool_size
        options["max_overflow"] = max_overflow
        options["pool_recycle"] = pool_recycle

    return create_async_engine(database_url, **options)


def create_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Return the session factory bound to ``engine``.

    ``autoflush=False`` keeps writes explicit: a repository that flushes gets a
    constraint violation at a line it can point at, rather than at whatever
    incidental query the ORM happened to run first.
    """
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


@asynccontextmanager
async def async_session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """Yield a session inside one transaction, committing on success.

    On exception the transaction is rolled back and **the exception is re-raised**.
    Swallowing it here would turn a failed verification write into a job that appears
    to have succeeded (Rule 12).
    """
    session = session_factory()
    try:
        yield session
        await session.commit()
    except BaseException:
        await session.rollback()
        raise
    finally:
        await session.close()


async def check_database(engine: AsyncEngine) -> None:
    """Run ``SELECT 1`` and raise if the database is not usable.

    Used by ``GET /ready``. A readiness probe must distinguish "the process is up" from
    "the process can serve traffic", and the only honest test of the latter is a query
    that reaches PostgreSQL and comes back — which also exercises the connection pool,
    so a pool that has gone stale is reported as unready rather than failing later on
    a real request.
    """
    async with engine.connect() as connection:
        result = await connection.execute(text("SELECT 1"))
        value = result.scalar_one()
    if value != 1:
        raise RuntimeError(f"database readiness probe returned {value!r}, expected 1")


async def dispose_engine(engine: AsyncEngine) -> None:
    """Close every pooled connection. Call once on application shutdown."""
    await engine.dispose()


class EngineCleanup:
    """Owns an engine plus its session factory and closes both exactly once.

    Application start-up needs the engine and the factory to travel together; making
    the pairing a type rather than a tuple means shutdown cannot dispose an engine
    while a factory that outlives it is still handed around.
    """

    __slots__ = ("engine", "session_factory")

    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self.session_factory = create_session_factory(engine)

    @classmethod
    def from_url(cls, database_url: str, **kwargs: Any) -> EngineCleanup:
        """Build an :class:`EngineCleanup` directly from a database URL."""
        return cls(create_engine_from_settings(database_url, **kwargs))

    async def aclose(self) -> None:
        """Dispose the engine. Idempotent for SQLAlchemy's own accounting."""
        await dispose_engine(self.engine)
