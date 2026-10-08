"""Alembic environment for Scout's async SQLAlchemy engine.

Design notes
------------

**The URL comes from the environment, never from the file.** ``alembic.ini`` leaves
``sqlalchemy.url`` empty and this module reads ``SCOUT_DATABASE_URL``. That keeps
credentials out of version control and means the same configuration works for a
developer's localhost, CI, and a deployment.

**Async all the way down.** Scout reaches PostgreSQL through ``asyncpg``, so the
migration runner uses ``async_engine_from_config`` and hands the migrations to
``connection.run_sync(context.run_migrations)``. Alembic's migration functions are
synchronous; running them inside the sync facade is what lets them share one
transaction and one connection with the async driver.

**Offline mode still works.** ``--sql`` output is generated against a synchronous
dialect instance, which is what lets ``alembic upgrade head --sql`` produce a script a
DBA can review without a database connection.

**Autogenerate is configured to be trustworthy.** ``compare_type=True`` makes a changed
column type show up as a diff rather than being silently ignored, and
``render_as_batch=False`` is explicit: batch mode exists for SQLite's inability to
``ALTER``, and Scout targets PostgreSQL only. Batch mode on PostgreSQL would emit
table-rewriting copies where an ordinary ``ALTER`` suffices.
"""

from __future__ import annotations

import asyncio
import os
import sys
from logging.config import fileConfig
from typing import Any, Final

from alembic import context
from sqlalchemy import Connection, pool
from sqlalchemy.ext.asyncio import async_engine_from_config

from scout_persistence.tables import Base

# Alembic's `Config` object, which gives access to the values in alembic.ini.
config = context.config

# The metadata autogenerate compares the database against. Imported from the schema
# module rather than re-declared, so a table added to the models is visible to
# `alembic check` immediately.
target_metadata: Final = Base.metadata

#: The environment variable that holds the database URL. Named once so the error
#: message below and the reader agree about what to set.
DATABASE_URL_ENV: Final = "SCOUT_DATABASE_URL"

#: Escape character for ConfigParser interpolation. A database password containing a
#: literal ``%`` is legal in a URL and would otherwise be read as the start of an
#: interpolation token, producing a confusing error from deep inside configparser.
_CONFIG_ESCAPE: Final = "%"


def _configure_logging() -> None:
    """Apply the logging configuration from ``alembic.ini``, when present.

    Guarded because a programmatic invocation (a test, a maintenance script) may build
    its own ``Config`` without ini file sections, and ``fileConfig`` raises on a config
    that has no ``[loggers]`` section.
    """
    if config.config_file_name is not None and config.get_section("loggers") is not None:
        fileConfig(config.config_file_name)


def _database_url() -> str:
    """Return the database URL, or exit with an actionable message.

    Exiting with a message rather than defaulting to a local URL is deliberate: a
    migration silently applied to the wrong database is worse than a migration that
    refuses to start.
    """
    url = os.environ.get(DATABASE_URL_ENV)
    if not url:
        print(
            f"error: {DATABASE_URL_ENV} is not set; Alembic needs a database URL.\n"
            f"       example: export {DATABASE_URL_ENV}="
            "'postgresql+asyncpg://scout:scout@localhost:5433/scout'",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return url


def _sync_url(url: str) -> str:
    """Return a synchronous driver URL for offline ``--sql`` generation.

    ``asyncpg`` cannot drive Alembic's offline mode, which needs a dialect object and no
    connection. ``postgresql+psycopg://`` is used when available and
    ``postgresql://`` otherwise; either way the URL is never connected to, so the
    fallback is harmless when the sync driver is not installed.
    """
    return url.replace("postgresql+asyncpg://", "postgresql://")


def run_migrations_offline() -> None:
    """Emit SQL to stdout without connecting to a database (``alembic upgrade --sql``)."""
    context.configure(
        url=_sync_url(_database_url()),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        render_as_batch=False,
        include_schemas=False,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run the migrations against an established synchronous connection facade.

    ``compare_type`` and ``compare_server_default`` are set here, in the function that
    actually configures the context Alembic's autogenerate and ``check`` commands use,
    rather than only on the ``EnvironmentContext``. Setting them in both places is
    intentional: the context configuration here is the one that decides the comparison,
    and the module-level one would otherwise look authoritative while being ignored.
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        render_as_batch=False,
        include_schemas=False,
    )
    with context.begin_transaction():
        context.run_migrations()


#: The ``Config.attributes`` key through which a programmatic caller passes extra
#: keyword arguments to ``create_async_engine``. Its reason to exist is CI and the test
#: suite: both need the migration to run against a specific PostgreSQL schema, which is
#: expressed as ``connect_args={"server_settings": {"search_path": ...}}`` and cannot be
#: put in a URL without losing its type.
ENGINE_OPTIONS_ATTRIBUTE: Final = "engine_options"


def _engine_options() -> dict[str, Any]:
    """Return caller-supplied engine options, defaulting to none.

    Read from ``Config.attributes`` rather than the ini file because the values are
    typed (a nested dict, in the search-path case) and the ini file holds strings only.
    """
    supplied = config.attributes.get(ENGINE_OPTIONS_ATTRIBUTE)
    if supplied is None:
        return {}
    if not isinstance(supplied, dict):
        raise TypeError(
            f"Config.attributes[{ENGINE_OPTIONS_ATTRIBUTE!r}] must be a dict of "
            f"create_async_engine keyword arguments, got {type(supplied).__name__}"
        )
    return dict(supplied)


async def run_async_migrations() -> None:
    """Open one async connection and run every migration inside it."""
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url().replace(_CONFIG_ESCAPE, _CONFIG_ESCAPE * 2)

    connectable = async_engine_from_config(
        section,
        prefix="sqlalchemy.",
        # A migration run is single-use and must not inherit a service's pool settings.
        poolclass=pool.NullPool,
        future=True,
        **_engine_options(),
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run the migrations against a live database."""
    asyncio.run(run_async_migrations())


_configure_logging()

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
