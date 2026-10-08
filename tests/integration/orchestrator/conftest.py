"""Fixtures for the orchestrator integration tests.

These tests need a real PostgreSQL, because the behaviour they prove *is* database behaviour: a
conditional ``UPDATE`` deciding who owns a lease, a ``RUNNING`` stage row being marked failed by the
reclaim sweep, and a status transition whose guard reads persisted rows.

**A clean skip, not an error, when PostgreSQL is absent.** The fast target runs without Docker, so
this directory must skip rather than fail; the check is a real connection attempt with a short
timeout, never an assumption that a configured URL implies a running server.

**Every test runs against a schema it creates and drops.** Isolation uses ``search_path`` rather than
a separate database, so the suite works against a managed instance where ``CREATE DATABASE`` is
unavailable.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, Final

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from scout_persistence.session import create_engine_from_settings, create_session_factory

DEFAULT_DATABASE_URL: Final = "postgresql+asyncpg://scout:scout@localhost:5433/scout"
CONNECT_TIMEOUT_SECONDS: Final = 3.0

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
ALEMBIC_INI: Final = REPO_ROOT / "migrations" / "alembic.ini"


def database_url() -> str:
    """Return the database URL under test, honouring ``SCOUT_DATABASE_URL``."""
    return os.environ.get("SCOUT_DATABASE_URL", DEFAULT_DATABASE_URL)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test in this directory as ``integration``.

    Applied by location so a new test cannot silently escape the marker and end up in the fast,
    Docker-free suite.
    """
    for item in items:
        if "tests/integration/orchestrator/" in str(item.fspath):
            item.add_marker(pytest.mark.integration)


def search_path_options(schema: str) -> dict[str, Any]:
    """Return engine options pinning every connection to ``schema``."""
    return {"connect_args": {"server_settings": {"search_path": schema}}}


def _new_schema_name() -> str:
    """Return a unique, PostgreSQL-safe schema name."""
    return f"scout_orch_{secrets.token_hex(6)}"


async def _schema_statement(url: str, statement: str) -> None:
    """Run one DDL statement with no search path set, then dispose the engine."""
    engine = create_engine_from_settings(url, pool_size=None)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(statement))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def postgres_url() -> str:
    """The database URL, after confirming a server actually answers there."""
    url = database_url()

    async def probe() -> None:
        engine = create_engine_from_settings(
            url, pool_size=None, connect_args={"timeout": CONNECT_TIMEOUT_SECONDS}
        )
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        finally:
            await engine.dispose()

    try:
        asyncio.run(probe())
    except Exception as exc:  # noqa: BLE001 - any failure means "no database"
        pytest.skip(
            f"PostgreSQL is not reachable at {url!r} ({type(exc).__name__}: {exc}). "
            "Start it with: docker compose -f infra/compose/docker-compose.yml up -d postgres"
        )
    return url


@pytest.fixture
async def test_schema(postgres_url: str) -> AsyncIterator[str]:
    """Yield the name of a freshly created, empty schema; drop it afterwards."""
    schema = _new_schema_name()
    await _schema_statement(postgres_url, f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    await _schema_statement(postgres_url, f'CREATE SCHEMA "{schema}"')
    try:
        yield schema
    finally:
        await _schema_statement(postgres_url, f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


@pytest.fixture
def migrated_schema(test_schema: str, postgres_url: str) -> Iterator[str]:
    """Create a schema and apply every migration to it."""
    previous = os.environ.get("SCOUT_DATABASE_URL")
    os.environ["SCOUT_DATABASE_URL"] = postgres_url
    try:
        config = Config(str(ALEMBIC_INI), ini_section="alembic")
        config.attributes["engine_options"] = search_path_options(test_schema)
        command.upgrade(config, "head")
    finally:
        if previous is None:
            os.environ.pop("SCOUT_DATABASE_URL", None)
        else:
            os.environ["SCOUT_DATABASE_URL"] = previous
    yield test_schema


@pytest.fixture
def engine(migrated_schema: str, postgres_url: str) -> Iterator[AsyncEngine]:
    """An engine whose every connection defaults to the migrated test schema.

    A ``NullPool`` is required rather than merely tidy: the schema is dropped at the end of the test,
    and a pooled connection pinned to it would either keep the schema alive or make the drop fail.
    """
    built = create_engine_from_settings(
        postgres_url, pool_size=None, **search_path_options(migrated_schema)
    )
    yield built


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """A session factory bound to the test schema's engine."""
    return create_session_factory(engine)
