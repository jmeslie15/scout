"""Fixtures shared by the persistence integration tests.

These tests need a real PostgreSQL, because the behaviour they prove *is* PostgreSQL
behaviour: a unique-constraint violation surfacing as an ``IntegrityError``, a
conditional ``UPDATE`` deciding a lease race, and full-text ranking. None of that can be
faked with SQLite, and faking it would only test the fake.

Two properties the fixtures guarantee:

**A clean skip, not an error, when PostgreSQL is absent.** The fast test target runs
without Docker, so this whole directory must skip rather than fail. The check is a real
connection attempt with a short timeout — never an assumption that a configured URL
implies a running server.

**Every test runs against a schema it creates and drops.** Isolation uses ``search_path``
rather than a separate database so the suite works against a managed instance where
``CREATE DATABASE`` is unavailable. Each test gets a uniquely named schema, created
before it and dropped after it, so no test depends on another's leftovers and a failure
cannot poison the next test.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from scout_domain.enums import (
    ActorType,
    ApprovalDecision,
    FindingCategory,
    JobStatus,
    SandboxBackendKind,
    Severity,
    ToolStatus,
)
from scout_domain.ids import IdKind, new_id
from scout_domain.models.approval import Approval
from scout_domain.models.artifact import Artifact, ArtifactKind
from scout_domain.models.audit import AuditEvent
from scout_domain.models.finding import Finding, FindingOrigin
from scout_domain.models.job import CommitRange, VerificationJob
from scout_domain.models.knowledge import (
    KnowledgeAuthority,
    KnowledgeDocument,
    KnowledgeSourceType,
)
from scout_domain.models.question import Question
from scout_domain.models.repository import RepositoryRef
from scout_domain.models.sandbox import (
    NetworkMode,
    ResourceLimits,
    SandboxPolicy,
    SandboxRun,
    SandboxStatus,
)
from scout_domain.models.tool import ToolResult, ToolSummary
from scout_persistence.session import create_engine_from_settings, create_session_factory

#: The default matches ``infra/compose/docker-compose.yml`` — host port 5433 because 5432
#: is commonly already bound on macOS. Overridable so the suite can run in CI.
DEFAULT_DATABASE_URL: Final = "postgresql+asyncpg://scout:scout@localhost:5433/scout"

#: Seconds to wait for a connection before declaring PostgreSQL unreachable. Short on
#: purpose: a skip that takes ten seconds is a skip nobody runs.
CONNECT_TIMEOUT_SECONDS: Final = 3.0

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
ALEMBIC_INI: Final = REPO_ROOT / "migrations" / "alembic.ini"


def database_url() -> str:
    """Return the database URL under test, honouring ``SCOUT_DATABASE_URL``."""
    return os.environ.get("SCOUT_DATABASE_URL", DEFAULT_DATABASE_URL)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark every test in this directory as ``integration``.

    Applied by location rather than by hand, so a new test cannot silently escape the
    marker and end up running in the fast, Docker-free suite.
    """
    marker = pytest.mark.integration
    for item in items:
        if "tests/integration/persistence/" in str(item.fspath):
            item.add_marker(marker)


def search_path_options(schema: str) -> dict[str, Any]:
    """Return ``create_async_engine`` options pinning a connection to ``schema``.

    Returning the nested keyword structure rather than a URL suffix is deliberate:
    asyncpg requires ``server_settings`` to be a real dict, and passing it through a URL
    query string would arrive as text and be rejected.
    """
    return {"connect_args": {"server_settings": {"search_path": schema}}}


def _new_schema_name() -> str:
    """Return a unique, PostgreSQL-safe schema name.

    A random token rather than a counter or the test name: two workers running the suite
    concurrently must not collide, and a name derived from a test id would make a
    leftover schema from a killed run look like an active one.
    """
    return f"scout_test_{secrets.token_hex(6)}"


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
    """The database URL, after confirming a server actually answers there.

    Skipping at session scope (rather than per test) keeps the reason in one place and
    makes the reachability check happen once.
    """
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
    except Exception as exc:
        # exception text is the only useful diagnostic for whoever runs this next.
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


def run_migrations(url: str, schema: str, *revisions: str) -> None:
    """Apply migrations to ``schema``, defaulting to ``head``.

    ``env.py`` builds its own engine from ``SCOUT_DATABASE_URL``, so the URL is set in
    the environment for the duration of the call, and the schema is handed over as a
    typed engine option through ``Config.attributes`` — the same mechanism a CI job
    behind a connection pooler would use.
    """
    previous = os.environ.get("SCOUT_DATABASE_URL")
    os.environ["SCOUT_DATABASE_URL"] = url
    try:
        config = Config(str(ALEMBIC_INI), ini_section="alembic")
        config.attributes["engine_options"] = search_path_options(schema)
        command.upgrade(config, *(revisions or ("head",)))
    finally:
        if previous is None:
            os.environ.pop("SCOUT_DATABASE_URL", None)
        else:
            os.environ["SCOUT_DATABASE_URL"] = previous


def downgrade_migrations(url: str, schema: str, revision: str) -> None:
    """Downgrade ``schema`` to ``revision`` (use ``"base"`` to drop every table)."""
    previous = os.environ.get("SCOUT_DATABASE_URL")
    os.environ["SCOUT_DATABASE_URL"] = url
    try:
        config = Config(str(ALEMBIC_INI), ini_section="alembic")
        config.attributes["engine_options"] = search_path_options(schema)
        command.downgrade(config, revision)
    finally:
        if previous is None:
            os.environ.pop("SCOUT_DATABASE_URL", None)
        else:
            os.environ["SCOUT_DATABASE_URL"] = previous


@pytest.fixture
def migrated_schema(test_schema: str, postgres_url: str) -> str:
    """Create a schema, apply every migration to it, and return its name.

    Most integration tests start here rather than exercising the migration itself: the
    migration is verified once, deliberately, and every other test is about repository
    behaviour rather than about Alembic. The ``test_schema`` fixture owns the teardown.
    """
    run_migrations(postgres_url, test_schema)
    return test_schema


# ---------------------------------------------------------------------------
# Alembic, driven from inside an async test
#
# ``command.upgrade`` reaches ``env.py``, which calls ``asyncio.run`` to build its own
# engine. Calling that from a running event loop is an error, so every migration is run in
# a worker thread via ``asyncio.to_thread``: the thread has no loop of its own, so
# ``asyncio.run`` succeeds there, and the caller stays ``await``-able. Running the
# migration in-process (rather than shelling out to the ``alembic`` CLI) keeps failures
# as ordinary exceptions with real tracebacks.
# ---------------------------------------------------------------------------


async def apply_migrations(url: str, schema: str, *revisions: str) -> None:
    """Apply ``revisions`` (default ``head``) to ``schema`` without blocking the loop."""
    await asyncio.to_thread(run_migrations, url, schema, *revisions)


async def revert_migrations(url: str, schema: str, revision: str) -> None:
    """Downgrade ``schema`` to ``revision`` without blocking the loop."""
    await asyncio.to_thread(downgrade_migrations, url, schema, revision)


@pytest.fixture(scope="session")
def migrate() -> Callable[..., Coroutine[Any, Any, None]]:
    """Return :func:`apply_migrations` as a fixture.

    Exposed rather than imported so the suite gains no import-time coupling between test
    directories: the repository root is deliberately not a package, so ``tests.*``
    imports do not resolve.
    """
    return apply_migrations


@pytest.fixture(scope="session")
def downgrade() -> Callable[..., Coroutine[Any, Any, None]]:
    """Return :func:`revert_migrations` as a fixture."""
    return revert_migrations


@pytest.fixture
def empty_engine(postgres_url: str, test_schema: str) -> AsyncEngine:
    """An engine over an empty schema, before any migration has run.

    Separate from :func:`engine`, which depends on ``migrated_schema``, so that the
    migration itself can be tested starting from nothing.
    """
    return create_engine_from_settings(
        postgres_url, pool_size=None, **search_path_options(test_schema)
    )


@pytest.fixture
def engine(migrated_schema: str, postgres_url: str) -> AsyncEngine:
    """An engine whose every connection defaults to the migrated test schema.

    A ``NullPool`` is required rather than merely tidy: the schema is dropped at the end
    of the test, and a pooled connection pinned to it would keep the schema alive and
    make the drop fail.
    """
    return create_engine_from_settings(
        postgres_url, pool_size=None, **search_path_options(migrated_schema)
    )


@pytest.fixture
def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """A session factory bound to the test schema's engine."""
    return create_session_factory(engine)


@pytest.fixture
async def session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """A session committed at the end of a passing test, rolled back on failure.

    Close is in a ``finally`` so a session whose transaction was aborted (a test that
    deliberately triggered a constraint violation) still releases its connection. Without
    that, one failing write leaks a connection into the next test — the kind of coupling
    that makes an intermittent failure impossible to reproduce.
    """
    active = session_factory()
    try:
        yield active
        # A test that deliberately provoked a constraint violation leaves the transaction
        # aborted, so the teardown commit fails. That is the test succeeding at what it
        # set out to do, not an error: roll the aborted transaction back and move on.
        try:
            await active.commit()
        except SQLAlchemyError:
            await active.rollback()
    except BaseException:
        await active.rollback()
        raise
    finally:
        await active.close()


@pytest.fixture
async def second_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """A second, independent session over the same schema.

    Concurrency tests need two sessions that do not share a transaction, because the whole
    question is what PostgreSQL does when two connections race for one unique key. This
    fixture never commits: everything it writes is discarded, so a test can use it to
    compete for a constraint without leaving state behind.
    """
    active = session_factory()
    try:
        yield active
    finally:
        await active.rollback()
        await active.close()


@pytest.fixture
async def verification_session(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """A third, read-only session for asserting what *committed* state looks like.

    Competing-claim tests need to check the database after another session's commit. A
    session that has already read the row returns its identity-mapped copy, so the
    assertion would compare against a stale object rather than against PostgreSQL. This
    fixture never writes and always rolls back.
    """
    active = session_factory()
    try:
        yield active
    finally:
        await active.rollback()
        await active.close()


# ---------------------------------------------------------------------------
# Domain-model factories
#
# Every value is explicit rather than defaulted, so a test that asserts on a field
# cannot pass because a factory happened to supply the value it expected. The
# factories live here rather than in each test module so that "a valid VerificationJob"
# has exactly one definition and stays valid as the domain model evolves.
# ---------------------------------------------------------------------------


def make_repository_ref(full_name: str = "acme/widgets") -> RepositoryRef:
    """Return a domain repository identity for tests."""
    return RepositoryRef(full_name=full_name)


def make_job(
    *,
    job_id: str | None = None,
    full_name: str = "acme/widgets",
    pull_request_number: int = 42,
    head_sha: str = "b" * 40,
    base_sha: str = "a" * 40,
    status: JobStatus = JobStatus.QUEUED,
    github_delivery_id: str | None = None,
    **overrides: Any,
) -> VerificationJob:
    """Return a valid :class:`VerificationJob` with explicit defaults.

    ``**overrides`` is passed through to the model so a test can set any field without
    this helper growing a parameter per field — while still going through Pydantic's
    validation rather than constructing an invalid object.
    """
    fields: dict[str, Any] = {
        "id": job_id or new_id(IdKind.JOB),
        "repository": make_repository_ref(full_name),
        "repository_full_name": full_name,
        "pull_request_number": pull_request_number,
        "pull_request_title": "Add widget export",
        "pull_request_author": "octocat",
        "commits": CommitRange(base_sha=base_sha, head_sha=head_sha),
        "github_delivery_id": github_delivery_id,
        "status": status,
        "correlation_id": "corr-test",
    }
    fields.update(overrides)
    return VerificationJob.model_validate(fields)


#: The signature of the job factory fixture. A ``Protocol`` would be more precise, but the
#: factory is a plain callable with keyword-only defaults, and this alias keeps the test
#: signatures readable.
JobFactory = Callable[..., VerificationJob]


#: Signatures of the remaining factory fixtures.
FindingFactory = Callable[..., Finding]
ArtifactFactory = Callable[..., Artifact]
ToolResultFactory = Callable[..., ToolResult]
SandboxFactory = Callable[..., SandboxRun]


@pytest.fixture
def finding_factory() -> FindingFactory:
    """Return :func:`make_finding` as a fixture."""
    return make_finding


@pytest.fixture
def artifact_factory() -> ArtifactFactory:
    """Return :func:`make_artifact` as a fixture."""
    return make_artifact


@pytest.fixture
def tool_result_factory() -> ToolResultFactory:
    """Return :func:`make_tool_result` as a fixture."""
    return make_tool_result


@pytest.fixture
def sandbox_factory() -> SandboxFactory:
    """Return :func:`make_sandbox_run` as a fixture."""
    return make_sandbox_run


@pytest.fixture
def question_factory() -> QuestionFactory:
    """Return :func:`make_question` as a fixture."""
    return make_question


#: Signature of the knowledge-document factory fixture.
DocumentFactory = Callable[..., KnowledgeDocument]


@pytest.fixture
def document_factory() -> DocumentFactory:
    """Return :func:`make_knowledge_document` as a fixture."""
    return make_knowledge_document


#: Signature of the audit-event factory fixture.
AuditEventFactory = Callable[..., AuditEvent]


@pytest.fixture
def event_factory() -> AuditEventFactory:
    """Return :func:`make_audit_event` as a fixture."""
    return make_audit_event


#: Signature of the question factory fixture.
QuestionFactory = Callable[..., Question]


#: Signature of the approval factory fixture.
ApprovalFactory = Callable[..., Approval]


@pytest.fixture
def approval_factory() -> ApprovalFactory:
    """Return :func:`make_approval` as a fixture."""
    return make_approval


@pytest.fixture
def job_factory() -> JobFactory:
    """Return :func:`make_job` as a fixture.

    Fixtures rather than cross-module imports: the repository root is not a package, so
    ``from tests.integration... import make_job`` does not resolve.
    """
    return make_job


def make_finding(
    *,
    job_id: str,
    finding_id: str | None = None,
    severity: Severity = Severity.HIGH,
    **overrides: Any,
) -> Finding:
    """Return a valid :class:`Finding` for tests."""
    fields: dict[str, Any] = {
        "id": finding_id or new_id(IdKind.FINDING),
        "job_id": job_id,
        "severity": severity,
        "category": FindingCategory.CORRECTNESS,
        "title": "Unbounded retry loop",
        "description": "The export retries forever when the upstream returns 429.",
        "repository": "acme/widgets",
        "origin": FindingOrigin.AGENT,
        "source": "code_reviewer",
    }
    fields.update(overrides)
    return Finding.model_validate(fields)


def make_question(
    *,
    job_id: str,
    question_id: str | None = None,
    **overrides: Any,
) -> Question:
    """Return a valid, open :class:`Question` for tests."""
    fields: dict[str, Any] = {
        "id": question_id or new_id(IdKind.QUESTION),
        "job_id": job_id,
        "title": "Should export metadata be visible to managers?",
        "description": "The PR adds manager-visible export metadata.",
        "created_by": "security",
    }
    fields.update(overrides)
    return Question.model_validate(fields)


def make_approval(
    *,
    job_id: str,
    commit_sha: str = "b" * 40,
    approval_id: str | None = None,
    decision: ApprovalDecision = ApprovalDecision.APPROVE,
    **overrides: Any,
) -> Approval:
    """Return a valid human :class:`Approval` for tests."""
    fields: dict[str, Any] = {
        "id": approval_id or new_id(IdKind.APPROVAL),
        "job_id": job_id,
        "commit_sha": commit_sha,
        "decision": decision,
        "decided_by": "dana",
        "comment": "Reviewed the export path by hand.",
    }
    fields.update(overrides)
    return Approval.model_validate(fields)


def make_tool_result(
    *,
    job_id: str,
    tool_run_id: str | None = None,
    status: ToolStatus = ToolStatus.PASSED,
    **overrides: Any,
) -> ToolResult:
    """Return a valid :class:`ToolResult` for tests."""
    started = datetime.now(UTC)
    fields: dict[str, Any] = {
        "id": tool_run_id or new_id(IdKind.TOOL_RUN),
        "job_id": job_id,
        "stage": JobStatus.TESTING.value,
        "tool": "pytest",
        "command": "pytest -q",
        "argv": ["pytest", "-q"],
        "exit_code": 0,
        "status": status,
        "started_at": started,
        "completed_at": started + timedelta(seconds=12.5),
        "duration_seconds": 12.5,
        "summary": ToolSummary(values={"passed": 183, "failed": 0}),
        "narrative": "183 passed.",
    }
    fields.update(overrides)
    return ToolResult.model_validate(fields)


def make_artifact(
    *,
    job_id: str | None,
    artifact_id: str | None = None,
    **overrides: Any,
) -> Artifact:
    """Return a valid :class:`Artifact` locator for tests.

    The digest is a fixed, well-formed SHA-256: these tests are about storing a locator,
    not about hashing bytes.
    """
    fields: dict[str, Any] = {
        "id": artifact_id or new_id(IdKind.ARTIFACT),
        "job_id": job_id,
        "kind": ArtifactKind.TOOL_STDOUT,
        "content_type": "text/plain",
        "size_bytes": 4096,
        "sha256": "c" * 64,
        "storage_key": f"sha256/cc/cc/{'c' * 64}",
        "filename": "pytest-stdout.txt",
    }
    fields.update(overrides)
    return Artifact.model_validate(fields)


def make_knowledge_document(
    *,
    document_id: str | None = None,
    title: str = "Export metadata authorization",
    body: str = "The reporting service bypasses user authorization; the gateway enforces it.",
    scope: str = "acme/widgets",
    **overrides: Any,
) -> KnowledgeDocument:
    """Return a valid :class:`KnowledgeDocument` for tests."""
    fields: dict[str, Any] = {
        "id": document_id or new_id(IdKind.KNOWLEDGE_DOCUMENT),
        "title": title,
        "body": body,
        "source_type": KnowledgeSourceType.HUMAN_ANSWER,
        "authority": KnowledgeAuthority.HUMAN_DECISION,
        "scope": scope,
    }
    fields.update(overrides)
    # ``expires_at`` must be after ``created_at``. A test that supplies a past expiry is
    # describing a document that expired, so the creation time is derived rather than
    # forcing every caller to remember to backdate it too.
    expires_at = fields.get("expires_at")
    if expires_at is not None and "created_at" not in fields:
        fields["created_at"] = expires_at - timedelta(days=1)
        fields["updated_at"] = fields["created_at"]
    return KnowledgeDocument.model_validate(fields)


def make_audit_event(
    *,
    event_id: str | None = None,
    actor: str = "dana",
    actor_type: str = ActorType.HUMAN.value,
    action: str = "approval.record",
    resource: str = "verification_job",
    resource_id: str = "job_01J8ZQ4K2M7X3N9PQRSTVWXY0",
    job_id: str | None = "job_01J8ZQ4K2M7X3N9PQRSTVWXY0",
    timestamp: datetime | None = None,
    **overrides: Any,
) -> AuditEvent:
    """Return a valid :class:`AuditEvent` for tests.

    ``actor_type`` is taken as the stored string so a test can also exercise the rejection
    path by passing something that is not a member.
    """
    fields: dict[str, Any] = {
        "id": event_id or new_id(IdKind.AUDIT_EVENT),
        "actor": actor,
        "actor_type": actor_type,
        "action": action,
        "resource": resource,
        "resource_id": resource_id,
        "job_id": job_id,
        "correlation_id": "corr-test",
    }
    if timestamp is not None:
        fields["timestamp"] = timestamp
    fields.update(overrides)
    return AuditEvent.model_validate(fields)


def make_sandbox_run(
    *,
    job_id: str,
    sandbox_run_id: str | None = None,
    status: SandboxStatus = SandboxStatus.PENDING,
    **overrides: Any,
) -> SandboxRun:
    """Return a valid :class:`SandboxRun` with a policy snapshot.

    ``limits`` and ``policy`` are supplied explicitly because the table has no default for
    them: an empty snapshot would claim the run had no isolation.
    """
    fields: dict[str, Any] = {
        "id": sandbox_run_id or new_id(IdKind.SANDBOX_RUN),
        "job_id": job_id,
        "backend": SandboxBackendKind.LOCAL,
        "limits": ResourceLimits(memory_mb=2048, cpus=1.0, pids_limit=256),
        "policy": SandboxPolicy(network_mode=NetworkMode.NONE, read_only_rootfs=True),
        "status": status,
        "workspace_path": "",
        "started_at": datetime.now(UTC),
    }
    fields.update(overrides)
    return SandboxRun.model_validate(fields)
