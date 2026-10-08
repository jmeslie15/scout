"""The initial migration applies to a clean database, and reverses.

This is the only place in the suite that exercises Alembic itself; every other
integration test starts from a schema this test proved it can build. The assertions are
deliberately about *observable database state* — relations, constraints, indexes, the
``tsvector`` — rather than about the migration file's text, because the file is generated
and the database is what Scout runs against.
"""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from scout_persistence.tables import TABLE_NAMES

pytestmark = pytest.mark.integration


async def fetch_rows(
    engine: AsyncEngine,
    statement: str,
    **parameters: Any,
) -> list[tuple[Any, ...]]:
    """Run a read-only query and return its rows as tuples."""
    async with engine.connect() as connection:
        result = await connection.execute(text(statement), parameters)
        return [tuple(row) for row in result.all()]


async def test_migration_creates_every_table(
    postgres_url: str,
    test_schema: str,
    empty_engine: AsyncEngine,
    migrate: Callable[..., Coroutine[Any, Any, None]],
) -> None:
    """A clean schema, migrated, contains exactly Scout's tables plus Alembic's own."""
    before = await fetch_rows(
        empty_engine, "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()"
    )
    assert before == [], "the fixture schema must start empty"

    await migrate(postgres_url, test_schema)

    after = {
        row[0]
        for row in await fetch_rows(
            empty_engine,
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()",
        )
    }
    assert set(TABLE_NAMES) <= after
    assert after - set(TABLE_NAMES) == {"alembic_version"}, (
        f"unexpected relations after migrating: {sorted(after - set(TABLE_NAMES))}"
    )


async def test_migration_records_the_revision(
    postgres_url: str,
    test_schema: str,
    empty_engine: AsyncEngine,
    migrate: Callable[..., Coroutine[Any, Any, None]],
) -> None:
    """``alembic_version`` holds a single, real revision id."""
    await migrate(postgres_url, test_schema)
    rows = await fetch_rows(empty_engine, "SELECT version_num FROM alembic_version")
    assert len(rows) == 1
    revision = rows[0][0]
    assert isinstance(revision, str)
    assert len(revision) >= 12, f"revision id {revision!r} is not a real identifier"


async def test_migration_creates_the_idempotency_constraints(
    postgres_url: str,
    test_schema: str,
    empty_engine: AsyncEngine,
    migrate: Callable[..., Coroutine[Any, Any, None]],
) -> None:
    """The two constraints duplicate-delivery protection depends on are in the database.

    Asserted by *name* rather than by trying an insert, because the name is what a future
    migration has to drop, and an unnamed constraint cannot be dropped deliberately.
    """
    await migrate(postgres_url, test_schema)
    rows = await fetch_rows(
        empty_engine,
        """
        SELECT conname
        FROM pg_constraint
        WHERE conrelid = 'webhook_events'::regclass AND contype = 'u'
        """,
    )
    names = {row[0] for row in rows}
    assert names == {"uq_webhook_events_delivery_id"}

    binding = await fetch_rows(
        empty_engine,
        """
        SELECT conname, pg_get_constraintdef(oid)
        FROM pg_constraint
        WHERE conrelid = 'verification_jobs'::regclass AND contype = 'u'
        """,
    )
    definitions = {row[0]: row[1] for row in binding}
    assert "pr_revision" in definitions
    for column in ("repository_full_name", "pull_request_number", "head_sha"):
        assert column in definitions["pr_revision"]


async def test_migration_creates_the_required_indexes(
    postgres_url: str,
    test_schema: str,
    empty_engine: AsyncEngine,
    migrate: Callable[..., Coroutine[Any, Any, None]],
) -> None:
    """The named indexes the query patterns need exist, including the GIN index."""
    await migrate(postgres_url, test_schema)
    rows = await fetch_rows(
        empty_engine,
        """
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = current_schema()
        """,
    )
    indexes = {row[0]: row[1] for row in rows}
    expected = {
        "ix_verification_jobs_status",
        "ix_verification_jobs_repository_full_name_pull_request_number",
        "ix_verification_jobs_lease_expires_at",
        "ix_job_stages_job_id_stage",
        "ix_findings_job_id_severity",
        "ix_questions_job_id_status",
        "ix_tool_runs_job_id_tool",
        "ix_agent_runs_job_id_role",
        "ix_audit_events_resource_resource_id",
        "ix_audit_events_job_id",
        "ix_notifications_dedup_key",
        "ix_model_requests_job_id_created_at",
        "ix_artifacts_job_id_kind",
        "ix_knowledge_documents_scope",
        "ix_approvals_job_id_commit_sha",
        "ix_approvals_job_id_invalidated_at",
    }
    assert expected <= set(indexes), f"missing indexes: {sorted(expected - set(indexes))}"
    assert "USING gin" in indexes["ix_knowledge_documents_search_vector"]


async def test_migration_creates_a_generated_tsvector_column(
    postgres_url: str,
    test_schema: str,
    empty_engine: AsyncEngine,
    migrate: Callable[..., Coroutine[Any, Any, None]],
) -> None:
    """The search vector is generated by PostgreSQL, so it cannot go stale."""
    await migrate(postgres_url, test_schema)
    rows = await fetch_rows(
        empty_engine,
        """
        SELECT is_generated, generation_expression
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'knowledge_documents'
          AND column_name = 'search_vector'
        """,
    )
    assert len(rows) == 1
    is_generated, expression = rows[0]
    assert is_generated == "ALWAYS"
    assert "to_tsvector" in expression
    assert "'A'" in expression
    assert "'B'" in expression


async def test_generated_search_vector_populates_on_insert(
    postgres_url: str,
    test_schema: str,
    empty_engine: AsyncEngine,
    migrate: Callable[..., Coroutine[Any, Any, None]],
) -> None:
    """Inserting a document without a search vector still yields a matchable one."""
    await migrate(postgres_url, test_schema)
    async with empty_engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO knowledge_documents
                    (id, title, body, source_type, authority, authority_rank, scope,
                     tags, created_at, updated_at)
                VALUES
                    ('kdoc_1', 'Retry policy', 'The exporter retries with exponential backoff.',
                     'ADR', 'APPROVED_DOCUMENT', 3, 'acme/widgets', '[]'::jsonb,
                     now(), now())
                """
            )
        )
    rows = await fetch_rows(
        empty_engine,
        """
        SELECT count(*) FROM knowledge_documents
        WHERE search_vector @@ plainto_tsquery('english', 'exponential backoff')
        """,
    )
    assert rows[0][0] == 1


async def test_every_check_constraint_exists_in_the_database(
    postgres_url: str,
    test_schema: str,
    empty_engine: AsyncEngine,
    migrate: Callable[..., Coroutine[Any, Any, None]],
) -> None:
    """The enum vocabularies are enforced by the database, not only by the models.

    Compared by count and by name against the models, so a constraint that SQLAlchemy
    compiled but the migration omitted is caught here rather than at the first bad insert.
    """
    from scout_persistence.tables import Base

    await migrate(postgres_url, test_schema)
    rows = await fetch_rows(
        empty_engine,
        """
        SELECT conrelid::regclass::text, conname
        FROM pg_constraint
        WHERE contype = 'c'
          AND connamespace = current_schema()::regnamespace
          -- PostgreSQL 11+ enforces NOT NULL with a hidden check constraint whose name
          -- starts with the column's attnum (e.g. "2200_12_1_not_null"). They are not
          -- Scout's constraints and they are not namable.
          AND conname NOT LIKE '%0_%_not_null'
        """,
    )
    in_database = {(row[0], row[1]) for row in rows}
    expected = {
        (table.name, constraint.name)
        for table in Base.metadata.sorted_tables
        for constraint in table.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    }
    missing = expected - in_database
    assert not missing, f"check constraints missing from the database: {sorted(missing)}"
    assert expected, "the models declare no check constraints, which cannot be right"


async def test_migration_round_trips_down_to_base(
    postgres_url: str,
    test_schema: str,
    empty_engine: AsyncEngine,
    migrate: Callable[..., Coroutine[Any, Any, None]],
    downgrade: Callable[..., Coroutine[Any, Any, None]],
) -> None:
    """``downgrade base`` removes every table the migration created.

    A migration whose downgrade is a stub makes the round-trip gate meaningless, so the
    test asserts the tables are actually gone rather than merely that no exception was
    raised.
    """
    await migrate(postgres_url, test_schema)
    await downgrade(postgres_url, test_schema, "base")
    remaining = {
        row[0]
        for row in await fetch_rows(
            empty_engine,
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()",
        )
    }
    # ``alembic_version`` is Alembic's own bookkeeping table and is not dropped by a
    # downgrade — it is how Alembic knows the database is at ``base``.
    assert remaining == {"alembic_version"}, (
        f"tables survived a downgrade to base: {sorted(remaining - {'alembic_version'})}"
    )


async def test_migration_can_be_reapplied_after_a_downgrade(
    postgres_url: str,
    test_schema: str,
    empty_engine: AsyncEngine,
    migrate: Callable[..., Coroutine[Any, Any, None]],
    downgrade: Callable[..., Coroutine[Any, Any, None]],
) -> None:
    """``upgrade -> downgrade -> upgrade`` works, which is the documented recovery path."""
    await migrate(postgres_url, test_schema)
    await downgrade(postgres_url, test_schema, "base")
    await migrate(postgres_url, test_schema)
    tables = {
        row[0]
        for row in await fetch_rows(
            empty_engine,
            "SELECT tablename FROM pg_tables WHERE schemaname = current_schema()",
        )
    }
    assert set(TABLE_NAMES) <= tables
