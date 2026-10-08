"""DDL compiles against the PostgreSQL dialect, with no database required.

The persistence layer targets PostgreSQL specifically: ``JSONB``, a generated
``tsvector``, ``BigInteger``, timezone-aware timestamps, and ``CHECK`` constraints whose
expressions use PostgreSQL functions (``char_length``, ``~``, ``jsonb_typeof``,
``?``). A model that only ever compiles for SQLite would be discovered as broken at
migration time, on a machine that may not have a database.

Compiling every ``CREATE TABLE``, ``CREATE INDEX``, and constraint for
``postgresql.dialect()`` is a cheap, hermetic check that catches: a type with no
PostgreSQL rendering, an index naming collision, an unresolvable column reference in a
constraint, and a table that cannot be sorted because a foreign key points at a table
declared later.
"""

from __future__ import annotations

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from scout_persistence.tables import TABLE_NAMES, Base

DIALECT = postgresql.dialect()


def test_ddl_compiles_for_every_table() -> None:
    """Every table renders as PostgreSQL DDL."""
    for table in Base.metadata.sorted_tables:
        ddl = str(CreateTable(table).compile(dialect=DIALECT))
        assert f"CREATE TABLE {table.name}" in ddl


def test_ddl_compiles_for_every_index() -> None:
    """Every index renders, including the GIN index on the search vector."""
    for table in Base.metadata.sorted_tables:
        for index in table.indexes:
            CreateIndex(index).compile(dialect=DIALECT)


def test_ddl_compiles_for_every_table_in_declaration_order() -> None:
    """``sorted_tables`` resolves foreign keys, so it cannot hit a forward reference."""
    ordered = [table.name for table in Base.metadata.sorted_tables]
    assert set(ordered) == set(TABLE_NAMES)
    for table in Base.metadata.sorted_tables:
        for foreign_key in table.foreign_keys:
            target = foreign_key.column.table.name
            assert ordered.index(target) < ordered.index(table.name), (
                f"{table.name} references {target}, which sorts after it"
            )


def test_every_table_declares_id_created_at_and_updated_at() -> None:
    """Every table carries a prefixed-ULID primary key and both audit timestamps."""
    for name in TABLE_NAMES:
        table = Base.metadata.tables[name]
        assert "id" in table.c, f"{name} has no id column"
        assert table.c["id"].primary_key, f"{name}.id is not the primary key"
        assert table.c["id"].type.length == 64, f"{name}.id must be String(64)"
        for stamp in ("created_at", "updated_at"):
            column = table.c[stamp]
            assert column.nullable is False, f"{name}.{stamp} must not be nullable"
            assert column.server_default is not None, (
                f"{name}.{stamp} must have a server default so a row cannot arrive unstamped"
            )


def test_timestamp_columns_are_timezone_aware() -> None:
    """No naive ``DateTime`` anywhere.

    A naive timestamp in an audit trail is a correctness bug: it cannot say which
    instant it recorded once two workers are in different zones.
    """
    naive: list[str] = []
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if column.type.__class__.__name__ == "DateTime" and not column.type.timezone:  # type: ignore[attr-defined]
                naive.append(f"{table.name}.{column.name}")
    assert naive == [], f"timezone-naive datetime columns: {naive}"


def test_identifiers_are_never_database_generated() -> None:
    """No primary key has a sequence, identity, or client-side default.

    An identifier has to be known before the row is written so it can appear in the queue
    message, the log line, and the GitHub check run written around the insert.
    """
    offenders: list[str] = []
    for table in Base.metadata.sorted_tables:
        column = table.c["id"]
        if column.default is not None or column.server_default is not None:
            offenders.append(table.name)
        assert not column.autoincrement or column.autoincrement == "auto"
    assert offenders == []


def test_no_column_stores_unbounded_tool_output() -> None:
    """Rule 15 is a schema constraint, not a convention.

    Raw output is unbounded text; the only columns allowed to hold large free text are
    those whose domain models cap them (findings, questions, knowledge bodies), and each
    of those is enumerated here so a new one has to be a deliberate decision.
    """
    permitted_text_columns = {
        ("findings", "description"),
        ("questions", "description"),
        ("questions", "context"),
        ("questions", "investigation_summary"),
        ("question_answers", "body"),
        ("approvals", "comment"),
        ("engineering_decisions", "statement"),
        ("engineering_decisions", "rationale"),
        ("knowledge_documents", "body"),
        ("notifications", "body"),
    }
    actual = {
        (table.name, column.name)
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if column.type.__class__.__name__ == "Text"
    }
    assert actual == permitted_text_columns


def test_webhook_delivery_id_is_unique_and_not_null() -> None:
    """The idempotency guarantee for duplicate GitHub deliveries (spec section 10)."""
    column = Base.metadata.tables["webhook_events"].c["delivery_id"]
    assert column.nullable is False
    assert column.unique is True


def test_job_pr_revision_uniqueness_is_declared() -> None:
    """One verification job per ``(repository, pull request, head sha)``."""
    table = Base.metadata.tables["verification_jobs"]
    unique_column_sets = {
        frozenset(constraint.columns.keys())
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert frozenset({"repository_full_name", "pull_request_number", "head_sha"}) in (
        unique_column_sets
    )


def test_notification_dedup_is_unique_per_channel() -> None:
    """``(dedup_key, channel)`` is unique: Scout must not spam (spec section 47)."""
    table = Base.metadata.tables["notifications"]
    unique_column_sets = {
        frozenset(constraint.columns.keys())
        for constraint in table.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }
    assert frozenset({"dedup_key", "channel"}) in unique_column_sets


def test_required_query_indexes_exist() -> None:
    """Every index the documented query patterns need is present.

    Named explicitly rather than counted, because the failure this guards against is a
    *specific* index being dropped during a refactor while the total stays the same.
    """
    required: dict[str, set[frozenset[str]]] = {
        "verification_jobs": {
            frozenset({"status"}),
            frozenset({"repository_full_name", "pull_request_number"}),
            frozenset({"lease_expires_at"}),
        },
        "job_stages": {frozenset({"job_id", "stage"})},
        "findings": {frozenset({"job_id", "severity"})},
        "questions": {frozenset({"job_id", "status"})},
        "tool_runs": {frozenset({"job_id", "tool"})},
        "agent_runs": {frozenset({"job_id", "role"})},
        "audit_events": {
            frozenset({"resource", "resource_id"}),
            frozenset({"job_id"}),
        },
        "notifications": {frozenset({"dedup_key"})},
        "model_requests": {frozenset({"job_id", "created_at"})},
        "artifacts": {frozenset({"job_id", "kind"})},
        "knowledge_documents": {frozenset({"scope"})},
        "approvals": {
            frozenset({"job_id", "commit_sha"}),
            frozenset({"job_id", "invalidated_at"}),
        },
    }
    for table_name, expected in required.items():
        actual = {
            frozenset(index.columns.keys()) for index in Base.metadata.tables[table_name].indexes
        }
        missing = expected - actual
        assert not missing, f"{table_name} is missing index(es) on {sorted(map(sorted, missing))}"


def test_composite_indexes_are_named_with_column_order() -> None:
    """Index names include both columns and their order.

    An autogenerated name that only mentioned the first column would make two indexes on
    ``(a, b)`` and ``(a, c)`` collide, and would make an ``ORDER BY a, b`` index
    indistinguishable from an ``ORDER BY b, a`` one in a migration review.
    """
    for table in Base.metadata.sorted_tables:
        for index in table.indexes:
            columns = list(index.columns.keys())
            if len(columns) > 1:
                assert "_".join(columns) in index.name, (
                    f"{table.name} index {index.name!r} does not name its column order"
                )


def test_enum_columns_are_varchar_not_native_pg_enum() -> None:
    """No native PostgreSQL ``ENUM`` type anywhere.

    ``ALTER TYPE ... ADD VALUE`` cannot run inside a transaction on older servers and
    cannot be rolled back, which makes it a poor fit for a state machine that is expected
    to gain states (spec section 11).
    """
    offenders: list[str] = []
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if column.type.__class__.__name__ in {"ENUM", "Enum"}:
                offenders.append(f"{table.name}.{column.name}")
    assert offenders == []


#: JSONB columns that must be supplied explicitly because an empty value would be
#: meaningless or wrong. ``verification_jobs.repository`` and ``.commits`` describe the
#: revision under verification — there is no sensible empty repository — and an
#: embedding vector of ``[]`` would be indistinguishable from "not yet computed".
JSON_COLUMNS_REQUIRING_A_VALUE: frozenset[tuple[str, str]] = frozenset(
    {
        ("verification_jobs", "repository"),
        ("verification_jobs", "commits"),
        ("sandbox_runs", "limits"),
        ("sandbox_runs", "policy"),
        ("knowledge_embeddings", "vector"),
    }
)


def test_collection_valued_json_columns_default_to_empty_not_null() -> None:
    """Every other JSONB column defaults to ``{}`` or ``[]``, never to NULL.

    A reader should not have to special-case "column exists but is null", and a JSON path
    query is then total over the column. The exceptions above are the columns where an
    empty value would be a *lie* rather than a convenience.
    """
    offenders: list[str] = []
    for table in Base.metadata.sorted_tables:
        for column in table.columns:
            if column.type.__class__.__name__ not in {"JSONB", "JSON"}:
                continue
            if (table.name, column.name) in JSON_COLUMNS_REQUIRING_A_VALUE:
                assert column.server_default is None
                continue
            if column.server_default is None:
                offenders.append(f"{table.name}.{column.name} (no default)")
                continue
            rendered = str(column.server_default.arg)
            if rendered not in {"'{}'::jsonb", "'[]'::jsonb"}:
                offenders.append(f"{table.name}.{column.name} (default {rendered})")
    assert offenders == []


def test_ddl_renders_the_generated_search_vector() -> None:
    """The weighted ``tsvector`` is generated and stored by PostgreSQL.

    ``STORED`` (not virtual) is required for the GIN index to be usable, and the weights
    are what make a title match outrank a body match.
    """
    ddl = str(CreateTable(Base.metadata.tables["knowledge_documents"]).compile(dialect=DIALECT))
    assert "TSVECTOR" in ddl
    assert "GENERATED ALWAYS AS" in ddl
    assert "STORED" in ddl
    assert "setweight" in ddl
    assert "'A'" in ddl
    assert "'B'" in ddl


def test_ddl_renders_the_gin_index_using_gin() -> None:
    """The search index is GIN, not the default btree.

    A btree index over a ``tsvector`` cannot answer an ``@@`` query, so getting this
    wrong would leave search correct but permanently sequential.
    """
    index = next(
        index
        for index in Base.metadata.tables["knowledge_documents"].indexes
        if "search_vector" in index.columns
    )
    ddl = str(CreateIndex(index).compile(dialect=DIALECT))
    assert "USING gin" in ddl


def test_ddl_compiles_for_the_offline_sql_dialect() -> None:
    """Alembic's offline mode uses a bare dialect, so DDL must not need a connection."""
    for table in Base.metadata.sorted_tables:
        CreateTable(table).compile(dialect=postgresql.dialect())
