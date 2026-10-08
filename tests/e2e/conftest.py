"""Fixtures for the end-to-end vertical slice.

The slice runs the real control plane against real infrastructure. That means it needs PostgreSQL
(for the job, stage, artifact, and audit rows), Redis (to prove the queue the orchestrator consumes
is the production transport), and a real git repository -- and it must **skip cleanly** when any of
those is absent, because the fast test target runs without Docker.

The connection checks are real attempts with short timeouts. Assuming that a configured URL implies
a running server is how a suite becomes "skipped" in one environment and mysteriously red in another.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, Final

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from scout_persistence.session import create_engine_from_settings, create_session_factory

# pytest imports a directory's ``conftest.py`` before it puts that directory on ``sys.path``, so the
# sibling fixture-builder module has to be made importable explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import GitFixture, build_git_fixture

DEFAULT_DATABASE_URL: Final = "postgresql+asyncpg://scout:scout@localhost:5433/scout"
DEFAULT_REDIS_URL: Final = "redis://localhost:6380/0"
CONNECT_TIMEOUT_SECONDS: Final = 3.0

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
ALEMBIC_INI: Final = REPO_ROOT / "migrations" / "alembic.ini"


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test in this directory as ``e2e``."""
    for item in items:
        if "tests/e2e/" in str(item.fspath):
            item.add_marker(pytest.mark.e2e)


def database_url() -> str:
    """Return the database URL under test."""
    return os.environ.get("SCOUT_DATABASE_URL", DEFAULT_DATABASE_URL)


def redis_url() -> str:
    """Return the Redis URL under test."""
    return os.environ.get("SCOUT_REDIS_URL", DEFAULT_REDIS_URL)


async def _probe_postgres(url: str) -> None:
    """Connect and run one query, or raise."""
    engine = create_engine_from_settings(
        url, pool_size=None, connect_args={"timeout": CONNECT_TIMEOUT_SECONDS}
    )
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    finally:
        await engine.dispose()


async def _probe_redis(url: str) -> None:
    """Ping Redis, or raise."""
    from redis.asyncio import Redis  # noqa: PLC0415 - only needed on this path

    client = Redis.from_url(url, socket_connect_timeout=CONNECT_TIMEOUT_SECONDS)
    try:
        await client.ping()
    finally:
        await client.aclose()


@pytest.fixture(scope="session")
def postgres_url() -> str:
    """The database URL, after confirming a server answers there."""
    url = database_url()
    try:
        asyncio.run(_probe_postgres(url))
    except Exception as exc:  # noqa: BLE001 - any failure means "no database"
        pytest.skip(
            f"PostgreSQL is not reachable at {url!r} ({type(exc).__name__}: {exc}). Start it with: "
            "docker compose -f infra/compose/docker-compose.yml up -d postgres"
        )
    return url


@pytest.fixture(scope="session")
def redis_server_url() -> str:
    """The Redis URL, after confirming a server answers there."""
    url = redis_url()
    try:
        asyncio.run(_probe_redis(url))
    except Exception as exc:  # noqa: BLE001 - any failure means "no Redis"
        pytest.skip(
            f"Redis is not reachable at {url!r} ({type(exc).__name__}: {exc}). Start it with: "
            "docker compose -f infra/compose/docker-compose.yml up -d redis"
        )
    return url


def _new_schema_name() -> str:
    """Return a unique, PostgreSQL-safe schema name."""
    return f"scout_e2e_{secrets.token_hex(6)}"


async def _schema_statement(url: str, statement: str) -> None:
    """Run one DDL statement with no search path set."""
    engine = create_engine_from_settings(url, pool_size=None)
    try:
        async with engine.begin() as connection:
            await connection.execute(text(statement))
    finally:
        await engine.dispose()


def search_path_options(schema: str) -> dict[str, Any]:
    """Return engine options pinning every connection to ``schema``."""
    return {"connect_args": {"server_settings": {"search_path": schema}}}


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
    """Create a schema and apply every migration to it, so the slice runs the real schema."""
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
    """An engine pinned to the migrated test schema, with no pooled connections outliving it."""
    built = create_engine_from_settings(
        postgres_url, pool_size=None, **search_path_options(migrated_schema)
    )
    yield built


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """A session factory bound to the test schema's engine."""
    return create_session_factory(engine)


@pytest.fixture
def artifact_root(tmp_path: Path) -> Path:
    """A per-test artifact store root."""
    target = tmp_path / "artifacts"
    target.mkdir(parents=True, exist_ok=True)
    return target


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    """A per-test sandbox workspace root."""
    target = tmp_path / "workspaces"
    target.mkdir(parents=True, exist_ok=True)
    return target


@pytest.fixture
def git_root(tmp_path: Path) -> Path:
    """Where the fixture repository lives, outside the workspace root so nothing cleans it up."""
    target = tmp_path / "git"
    target.mkdir(parents=True, exist_ok=True)
    return target


@pytest.fixture
def passing_repo(git_root: Path) -> GitFixture:
    """A repository whose head commit adds a covered, passing feature."""
    return build_git_fixture(git_root / "passing", failing=False)


@pytest.fixture
def failing_repo(git_root: Path) -> GitFixture:
    """A repository whose head commit breaks the behaviour its suite asserts."""
    return build_git_fixture(git_root / "failing", failing=True)
