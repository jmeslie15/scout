"""SQLAlchemy 2.0 schema for Scout's system of record (spec section 42).

The split this module encodes
---------------------------

**The database holds locators; the artifact store holds bytes.**

Every column here is either a scalar fact, a status from Scout's closed vocabulary,
a small JSON document of structured detail, or a *pointer* at something larger.
Raw tool stdout, diffs, prompt text, and generated tests never appear as columns:
they are written to the artifact store and referenced by ``artifacts.id`` (see
``scout_domain.models.artifact`` and Rule 15). That is what makes it affordable to
preserve raw evidence forever — a 40 MB failing test log costs one ``artifacts``
row and one content-addressed blob, not 40 MB of PostgreSQL.

Three schema decisions are worth reading before changing anything:

* **Enums are ``VARCHAR`` + ``CHECK``, not PostgreSQL ``ENUM``.** The job state
  machine is expected to gain states (spec section 11), and ``ALTER TYPE ... ADD
  VALUE`` cannot run inside a transaction or be rolled back on older servers. A
  named check constraint is a plain, reversible migration.
* **Identifiers are caller-supplied prefixed ULIDs** (``job_01J8...``), never
  database-generated sequences. An identifier is therefore known before the row is
  written, which is what lets a job id appear in a log line, a queue message, and
  a GitHub check run before the insert commits.
* **Idempotency is enforced by the database, not by application reads.**
  ``webhook_events.delivery_id`` is unique because GitHub redelivers events and a
  read-then-write check loses that race; ``verification_jobs`` is unique per
  ``(repository_full_name, pull_request_number, head_sha)`` because one PR revision
  gets exactly one verification (spec sections 10 and 62).

One table is deliberately **not** in spec section 42's list: ``approvals``. Sections
46 and 74 require the human authorization record to be durable, attributable, and
*invalidated rather than deleted* when a pull request moves past it, and
``ApprovalRepository`` cannot honour that without somewhere to write it. Review and
the parent workstream approved adding it as an explicit exception; it is named in
``tests/contract/persistence`` under ``EXTRA_TABLES`` with the same reason, so any
*other* table appearing here still fails the contract test.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum as _Enum
from typing import Any, Final

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from scout_domain.enums import (
    ActorType,
    AgentRole,
    AgentStatus,
    ApprovalDecision,
    Capability,
    FindingCategory,
    JobStatus,
    NotificationChannel,
    QuestionStatus,
    Recommendation,
    RiskLevel,
    SandboxBackendKind,
    Severity,
    ToolStatus,
    VerificationStatus,
)
from scout_domain.models.artifact import ArtifactKind
from scout_domain.models.finding import FindingOrigin
from scout_domain.models.job import JobStageOutcome
from scout_domain.models.knowledge import KnowledgeAuthority, KnowledgeSourceType
from scout_domain.models.model_request import ModelRequestStatus
from scout_domain.models.notification import NotificationStatus
from scout_domain.models.sandbox import SandboxStatus

__all__ = [
    "ENUM_COLUMN_WIDTH",
    "ENUM_SPECS",
    "NAMING_CONVENTION",
    "TABLE_NAMES",
    "WEBHOOK_EVENT_STATUSES",
    "AgentRunRow",
    "ArtifactRow",
    "AuditEventRow",
    "Base",
    "EngineeringDecisionRow",
    "FindingRow",
    "JobRow",
    "JobStageRow",
    "KnowledgeDocumentRow",
    "KnowledgeEmbeddingRow",
    "ModelRequestRow",
    "NotificationRow",
    "QuestionAnswerRow",
    "QuestionRow",
    "RepositoryRow",
    "SandboxRunRow",
    "TimestampMixin",
    "ToolRunRow",
    "WebhookEventRow",
    "enum_check",
    "json_object_default",
]


#: Stable, explicit names for every constraint PostgreSQL creates on Scout's behalf.
#: Alembic autogenerate compares constraints by name, so without this a rename or an
#: extra column silently produces "drop unnamed constraint / create unnamed
#: constraint" churn — and an unnamed constraint cannot be dropped in a later
#: migration without looking its generated name up by hand.
NAMING_CONVENTION: Final[dict[str, str]] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_N_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: Every table in the Scout schema (spec section 42). Kept as an explicit tuple so
#: the contract test can compare it with ``Base.metadata.tables`` and catch an
#: accidental extra or missing table.
TABLE_NAMES: Final[tuple[str, ...]] = (
    "repositories",
    "verification_jobs",
    "webhook_events",
    "job_stages",
    "agent_runs",
    "tool_runs",
    "findings",
    "questions",
    "question_answers",
    "approvals",
    "engineering_decisions",
    "knowledge_documents",
    "knowledge_embeddings",
    "notifications",
    "artifacts",
    "sandbox_runs",
    "model_requests",
    "audit_events",
)


class Base(DeclarativeBase):
    """Declarative base carrying Scout's constraint naming convention."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class TimestampMixin:
    """``created_at`` / ``updated_at`` on every table.

    Both are server-defaulted so that a row inserted by a migration, a ``psql``
    session, or a future service cannot arrive without a timestamp. ``updated_at``
    additionally carries ``onupdate``, but the authoritative rule is that a
    repository method which mutates state also sets it explicitly: a bulk
    ``UPDATE ... WHERE`` with ``synchronize_session=False`` bypasses ORM-level
    ``onupdate``, and an unchanged ``updated_at`` on a changed row is a lie in the
    audit trail.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


def enum_check[EnumT: _Enum](enum: type[EnumT], column: str, name: str) -> CheckConstraint:
    """Build a named ``CHECK`` constraint listing ``enum``'s permitted values.

    ``name`` is local; the metadata naming convention turns it into
    ``ck_<table>_<name>``. Values are rendered as ``'VALUE'`` literals, which is the
    same shape the contract test parses, so a new enum member cannot be added to the
    Python enum without the DDL (and therefore the migration) also changing — the
    contract test fails first, before anyone writes a migration they do not need.
    """
    values = ", ".join(f"'{member.value}'" for member in enum)
    return CheckConstraint(f"{column} IN ({values})", name=name)


#: Which enum backs each ``CHECK``-constrained column, keyed by
#: ``(table_name, column_name)``. This registry is the single place the vocabulary,
#: the DDL, and the contract test are wired together: the test iterates it, reads the
#: matching check constraint out of ``Base.metadata``, and asserts the two agree.
ENUM_SPECS: Final[dict[tuple[str, str], type[_Enum]]] = {
    ("verification_jobs", "status"): JobStatus,
    ("verification_jobs", "risk_level"): RiskLevel,
    ("verification_jobs", "verification_status"): VerificationStatus,
    ("verification_jobs", "recommendation"): Recommendation,
    ("job_stages", "stage"): JobStatus,
    ("job_stages", "outcome"): JobStageOutcome,
    ("agent_runs", "role"): AgentRole,
    ("agent_runs", "status"): AgentStatus,
    ("tool_runs", "status"): ToolStatus,
    ("tool_runs", "capability"): Capability,
    ("findings", "severity"): Severity,
    ("findings", "category"): FindingCategory,
    ("findings", "origin"): FindingOrigin,
    ("questions", "status"): QuestionStatus,
    ("questions", "severity"): Severity,
    ("questions", "created_by_type"): ActorType,
    ("question_answers", "answered_by_type"): ActorType,
    ("approvals", "decision"): ApprovalDecision,
    ("approvals", "decided_by_type"): ActorType,
    ("knowledge_documents", "source_type"): KnowledgeSourceType,
    ("knowledge_documents", "authority"): KnowledgeAuthority,
    ("notifications", "channel"): NotificationChannel,
    ("notifications", "severity"): Severity,
    ("notifications", "status"): NotificationStatus,
    ("artifacts", "kind"): ArtifactKind,
    ("sandbox_runs", "backend"): SandboxBackendKind,
    ("sandbox_runs", "status"): SandboxStatus,
    ("model_requests", "status"): ModelRequestStatus,
    ("model_requests", "agent"): AgentRole,
    ("audit_events", "actor_type"): ActorType,
}

#: The values ``webhook_events.status`` may take. Delivery receipts are not a domain
#: enum — they are local to the ingestion path — so the vocabulary lives here.
WEBHOOK_EVENT_STATUSES: Final[tuple[str, ...]] = (
    "RECEIVED",
    "PROCESSED",
    "DUPLICATE",
    "FAILED",
)

#: The widest permitted value across every constrained column. ``String`` widths are
#: sized from this so that adding a longer enum member is caught by the contract test
#: rather than by a truncation error in production.
ENUM_COLUMN_WIDTH: Final = 32


def enum_checks(table: str) -> tuple[CheckConstraint, ...]:
    """Return the generated ``CHECK`` constraints for ``table``'s enum columns.

    Tables call this instead of hand-writing each constraint, so
    :data:`ENUM_SPECS` is the only place a column's vocabulary is declared. The
    consequence is that the registry, the DDL, and the contract test cannot drift:
    adding an entry changes the DDL, and the test fails until a migration exists.
    """
    return tuple(
        enum_check(enum, column, f"{column}_values")
        for (table_name, column), enum in ENUM_SPECS.items()
        if table_name == table
    )


def json_object_default() -> Any:
    """Return the ``server_default`` used by every JSON object column.

    Defaulting to ``{}`` rather than ``NULL`` means readers never have to special-case
    "column exists but is null" and a JSON path query is total over the column.
    """
    return text("'{}'::jsonb")


# ---------------------------------------------------------------------------
# repositories
# ---------------------------------------------------------------------------


class RepositoryRow(Base, TimestampMixin):
    """A GitHub repository Scout is permitted to verify (spec section 13).

    ``full_name`` is unique rather than the primary key: ``owner/name`` changes when
    a repository is renamed, and the ULID primary key survives that. ``github_id``
    is the stable numeric identity and is what an upsert should prefer when it is
    known.
    """

    __tablename__ = "repositories"
    __table_args__ = (CheckConstraint("char_length(full_name) >= 3", name="full_name_length"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    github_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    default_branch: Mapped[str] = mapped_column(
        String(255), nullable=False, server_default=text("'main'")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    #: GitHub App installation id. A credential handle for the control plane only; it
    #: is never placed in agent context (spec sections 54 and 55).
    installation_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    #: Detected technology stack, cached from the analyzer agent (spec section 15).
    stack: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=json_object_default()
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=json_object_default()
    )


# ---------------------------------------------------------------------------
# verification_jobs
# ---------------------------------------------------------------------------


class JobRow(Base, TimestampMixin):
    """One verification of one pull-request revision (spec section 12).

    The lease columns (``lease_owner`` / ``lease_expires_at``) are the crash-recovery
    mechanism from spec section 50: a worker takes the lease when it starts and
    extends it while it runs, and the reclaim sweep looks for jobs whose lease has
    expired. The lease is *not* an advisory lock — it is a row value, so it survives
    a worker dying and is visible to the sweep.
    """

    __tablename__ = "verification_jobs"
    __table_args__ = (
        *enum_checks("verification_jobs"),
        # One verification job per PR revision. A force-push or a new commit is a new
        # head_sha and therefore legitimately a new job (spec sections 12 and 62).
        UniqueConstraint(
            "repository_full_name", "pull_request_number", "head_sha", name="pr_revision"
        ),
        CheckConstraint("pull_request_number >= 1", name="pull_request_number_positive"),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
        CheckConstraint("attempt <= max_attempts", name="attempt_within_max"),
        CheckConstraint("commits ? 'head_sha'", name="commits_have_head_sha"),
        Index(None, "status"),
        Index(None, "repository_full_name", "pull_request_number"),
        Index(None, "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)

    #: Full RepositoryRef as JSON. The denormalised ``repository_full_name`` below is
    #: the queryable projection of it, and is kept in sync by the mapping layer — the
    #: domain model rejects a job whose two spellings disagree.
    repository: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    repository_full_name: Mapped[str] = mapped_column(String(200), nullable=False)

    pull_request_number: Mapped[int] = mapped_column(Integer, nullable=False)
    pull_request_title: Mapped[str] = mapped_column(
        String(1000), nullable=False, server_default=text("''")
    )
    pull_request_author: Mapped[str] = mapped_column(
        String(200), nullable=False, server_default=text("''")
    )
    pull_request_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)

    commits: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    head_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    base_sha: Mapped[str] = mapped_column(String(64), nullable=False)

    #: The GitHub delivery that created this job: Scout's idempotency key.
    github_delivery_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    status: Mapped[str] = mapped_column(
        String(ENUM_COLUMN_WIDTH),
        nullable=False,
        server_default=text(f"'{JobStatus.RECEIVED.value}'"),
    )
    risk_level: Mapped[str] = mapped_column(
        String(ENUM_COLUMN_WIDTH),
        nullable=False,
        server_default=text(f"'{RiskLevel.MEDIUM.value}'"),
    )
    verification_status: Mapped[str] = mapped_column(
        String(ENUM_COLUMN_WIDTH),
        nullable=False,
        server_default=text(f"'{VerificationStatus.PENDING.value}'"),
    )
    #: Null until the report generator produces a recommendation.
    recommendation: Mapped[str | None] = mapped_column(String(ENUM_COLUMN_WIDTH), nullable=True)

    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("3"))

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    lease_owner: Mapped[str | None] = mapped_column(String(200), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    #: Shared by every log line, span, and audit event for this job.
    correlation_id: Mapped[str] = mapped_column(
        String(100), nullable=False, server_default=text("''")
    )
    failure_reason: Mapped[str | None] = mapped_column(String(8000), nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(String(2000), nullable=True)


# ---------------------------------------------------------------------------
# webhook_events
# ---------------------------------------------------------------------------


class WebhookEventRow(Base, TimestampMixin):
    """An ingested GitHub webhook delivery (spec section 10).

    ``delivery_id`` is unique because GitHub *will* redeliver: on a network blip, on
    a manual "Redeliver" in the UI, and on an at-least-once queue redelivery. A
    read-then-write "have I seen this?" check loses that race at two workers, so the
    constraint is the arbiter and the repository catches ``IntegrityError``.

    The full payload is retained (it is small and is the evidence for whatever job
    the delivery created), but it is stored as JSON rather than as a text blob so that
    a future query can filter on ``payload->'action'`` without parsing.
    """

    __tablename__ = "webhook_events"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ", ".join(f"'{value}'" for value in WEBHOOK_EVENT_STATUSES) + ")",
            name="status_values",
        ),
        Index(None, "repository_full_name"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    #: ``X-GitHub-Delivery``. UNIQUE NOT NULL: the whole idempotency story depends on it.
    delivery_id: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str | None] = mapped_column(String(100), nullable=True)
    repository_full_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    pull_request_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=json_object_default()
    )
    status: Mapped[str] = mapped_column(
        String(ENUM_COLUMN_WIDTH), nullable=False, server_default=text("'RECEIVED'")
    )
    error: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    #: Set once the delivery has been turned into (or matched to) a verification job.
    job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


# ---------------------------------------------------------------------------
# job_stages
# ---------------------------------------------------------------------------


class JobStageRow(Base, TimestampMixin):
    """One attempt at one pipeline stage (spec section 12).

    Stages are rows, not a mutable field on the job, so retries and skips leave a
    complete history — which is what makes "retries are recorded" (spec section 51)
    and "no stage is silently skipped" (spec section 12) verifiable after the fact.
    """

    __tablename__ = "job_stages"
    __table_args__ = (
        *enum_checks("job_stages"),
        UniqueConstraint("job_id", "stage", "attempt", name="job_stage_attempt"),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
        CheckConstraint(
            "NOT (outcome = 'SKIPPED' AND (skip_reason IS NULL OR skip_reason = ''))",
            name="skipped_has_reason",
        ),
        CheckConstraint(
            "NOT (outcome = 'FAILED' AND (error IS NULL OR error = ''))",
            name="failed_has_error",
        ),
        CheckConstraint(
            "duration_seconds IS NULL OR duration_seconds >= 0", name="duration_non_negative"
        ),
        Index(None, "job_id", "stage"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("verification_jobs.id", ondelete="CASCADE"), nullable=False
    )
    #: The JobStatus this stage corresponds to (PREPARING, ANALYZING, ...).
    stage: Mapped[str] = mapped_column(String(ENUM_COLUMN_WIDTH), nullable=False)
    outcome: Mapped[str] = mapped_column(
        String(ENUM_COLUMN_WIDTH),
        nullable=False,
        server_default=text(f"'{JobStageOutcome.PENDING.value}'"),
    )
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))

    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    error: Mapped[str | None] = mapped_column(String(8000), nullable=True)
    skip_reason: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    #: Artifact ids produced by this stage, for the job timeline view.
    artifact_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )


# ---------------------------------------------------------------------------
# agent_runs
# ---------------------------------------------------------------------------


class AgentRunRow(Base, TimestampMixin):
    """One agent invocation, for reproducibility and cost (spec section 32).

    The context itself is *not* stored — only ``context_digest``, a SHA-256 of the
    serialized context. Two runs can therefore be proven to have seen identical input
    without the table growing with repository text (which may be sensitive).
    """

    __tablename__ = "agent_runs"
    __table_args__ = (
        *enum_checks("agent_runs"),
        CheckConstraint("input_tokens >= 0", name="input_tokens_non_negative"),
        CheckConstraint("output_tokens >= 0", name="output_tokens_non_negative"),
        CheckConstraint("estimated_cost_usd >= 0", name="cost_non_negative"),
        Index(None, "job_id", "role"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("verification_jobs.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[str] = mapped_column(String(ENUM_COLUMN_WIDTH), nullable=False)
    status: Mapped[str] = mapped_column(
        String(ENUM_COLUMN_WIDTH),
        nullable=False,
        server_default=text(f"'{AgentStatus.COMPLETED.value}'"),
    )
    #: Exact prompt template version, so a historical result can be reproduced
    #: (spec section 39).
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)

    provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model: Mapped[str | None] = mapped_column(String(200), nullable=True)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    latency_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    estimated_cost_usd: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0")
    )
    context_digest: Mapped[str | None] = mapped_column(String(128), nullable=True)
    result_artifact_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error: Mapped[str | None] = mapped_column(String(8000), nullable=True)


# ---------------------------------------------------------------------------
# tool_runs
# ---------------------------------------------------------------------------


class ToolRunRow(Base, TimestampMixin):
    """One deterministic tool execution (spec section 21).

    ``invocation`` keeps the full :class:`~scout_domain.models.tool.ToolInvocation`
    — including the exact argv, working directory, and timeout — because "what did
    Scout actually run?" is the first question in any post-incident review. The
    resolved ``command`` is denormalised for cheap display. Output is *not* stored
    here: ``stdout_artifact_id`` and friends point at the artifact store.
    """

    __tablename__ = "tool_runs"
    __table_args__ = (
        *enum_checks("tool_runs"),
        CheckConstraint("duration_seconds >= 0", name="duration_non_negative"),
        Index(None, "job_id", "tool"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("verification_jobs.id", ondelete="CASCADE"), nullable=False
    )
    #: Human-readable stage label (e.g. ``TESTING`` or ``pytest``); free text by design.
    stage: Mapped[str] = mapped_column(String(100), nullable=False, server_default=text("''"))
    tool: Mapped[str] = mapped_column(String(100), nullable=False)
    capability: Mapped[str] = mapped_column(String(ENUM_COLUMN_WIDTH), nullable=False)
    invocation: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=json_object_default()
    )

    version: Mapped[str | None] = mapped_column(String(100), nullable=True)
    command: Mapped[str] = mapped_column(String(2000), nullable=False)
    argv: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    exit_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(ENUM_COLUMN_WIDTH), nullable=False)

    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)

    stdout_artifact_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    stderr_artifact_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    report_artifact_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=json_object_default()
    )

    error: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    infrastructure_failure: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    sandbox_run_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    narrative: Mapped[str] = mapped_column(String(2000), nullable=False, server_default=text("''"))
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=json_object_default()
    )


# ---------------------------------------------------------------------------
# findings
# ---------------------------------------------------------------------------


class FindingRow(Base, TimestampMixin):
    """A finding produced by an agent or a deterministic tool (spec section 33).

    ``evidence`` is a JSONB array of :class:`~scout_domain.models.evidence.EvidenceReference`
    *locators*. This does not violate Rule 15: an evidence reference is a pointer
    (an id, a path, a bounded excerpt), not raw output. The bytes live in artifacts.
    """

    __tablename__ = "findings"
    __table_args__ = (
        *enum_checks("findings"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        CheckConstraint("line_start IS NULL OR line_start >= 1", name="line_start_positive"),
        CheckConstraint(
            "NOT (file IS NULL AND (line_start IS NOT NULL OR line_end IS NOT NULL))",
            name="lines_require_file",
        ),
        Index(None, "job_id", "severity"),
        Index(None, "job_id", "category"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("verification_jobs.id", ondelete="CASCADE"), nullable=False
    )
    severity: Mapped[str] = mapped_column(String(ENUM_COLUMN_WIDTH), nullable=False)
    category: Mapped[str] = mapped_column(String(ENUM_COLUMN_WIDTH), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)

    #: Denormalised ``owner/name`` so a finding is readable on its own.
    repository: Mapped[str] = mapped_column(String(200), nullable=False)
    file: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    line_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    line_end: Mapped[int | None] = mapped_column(Integer, nullable=True)

    evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    recommendation: Mapped[str] = mapped_column(
        String(2000), nullable=False, server_default=text("''")
    )
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0.5"))
    requires_human: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    origin: Mapped[str] = mapped_column(String(ENUM_COLUMN_WIDTH), nullable=False)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    source_run_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    #: Set by the domain validator when confidence had to be clamped to the ceiling
    #: implied by the evidence. Surfaced, never hidden.
    confidence_capped: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )


# ---------------------------------------------------------------------------
# questions / question_answers / engineering_decisions
# ---------------------------------------------------------------------------


class QuestionRow(Base, TimestampMixin):
    """A question Scout could not answer from evidence (spec section 27).

    ``options`` and ``default_assumption`` are stored because a question without a
    stated default is how silence gets mistaken for approval — the default has to be
    captured at the moment the question is asked.
    """

    __tablename__ = "questions"
    __table_args__ = (
        *enum_checks("questions"),
        Index(None, "job_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("verification_jobs.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    context: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    investigation_summary: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=text("''")
    )
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    severity: Mapped[str] = mapped_column(String(ENUM_COLUMN_WIDTH), nullable=False)
    options: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    default_assumption: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    status: Mapped[str] = mapped_column(
        String(ENUM_COLUMN_WIDTH),
        nullable=False,
        server_default=text(f"'{QuestionStatus.OPEN.value}'"),
    )
    created_by: Mapped[str] = mapped_column(String(100), nullable=False)
    created_by_type: Mapped[str] = mapped_column(
        String(ENUM_COLUMN_WIDTH),
        nullable=False,
        server_default=text(f"'{ActorType.AGENT.value}'"),
    )
    accepted_answer_id: Mapped[str | None] = mapped_column(String(100), nullable=True)


class QuestionAnswerRow(Base, TimestampMixin):
    """A human (or, exceptionally, system) answer to a question (spec section 28).

    Answers are rows rather than a JSON array on the question so that each one is
    attributable, individually auditable, and promotable to engineering knowledge
    without rewriting the question.
    """

    __tablename__ = "question_answers"
    __table_args__ = (
        *enum_checks("question_answers"),
        Index(None, "question_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    question_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("questions.id", ondelete="CASCADE"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    answered_by: Mapped[str] = mapped_column(String(200), nullable=False)
    answered_by_type: Mapped[str] = mapped_column(
        String(ENUM_COLUMN_WIDTH),
        nullable=False,
        server_default=text(f"'{ActorType.HUMAN.value}'"),
    )
    answered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    promote_to_knowledge: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    knowledge_scope: Mapped[str | None] = mapped_column(String(200), nullable=True)


class ApprovalRow(Base, TimestampMixin):
    """A human authorization decision about one job at one commit (spec sections 46, 74).

    Invalidation is a column pair rather than a delete. When the pull request moves
    past an approval, :meth:`ApprovalRepository.invalidate_for_job` sets
    ``invalidated_at``/``invalidated_reason``; the record of *what a human authorized
    and when* is exactly what an incident review needs, so it is never removed.

    ``decision`` is constrained to :class:`ApprovalDecision`, and ``decided_by_type``
    is constrained to ``ActorType`` — the database rejects an approval produced by an
    agent or the system, mirroring the domain validator. Two independent layers
    enforcing "Scout cannot approve its own work" is deliberate.
    """

    __tablename__ = "approvals"
    __table_args__ = (
        *enum_checks("approvals"),
        CheckConstraint(
            "NOT (invalidated_at IS NULL AND invalidated_reason IS NOT NULL)",
            name="invalidation_reason_requires_timestamp",
        ),
        CheckConstraint(
            "NOT (invalidated_at IS NOT NULL AND (invalidated_reason IS NULL "
            "OR invalidated_reason = ''))",
            name="invalidation_requires_reason",
        ),
        CheckConstraint(
            "invalidated_at IS NULL OR invalidated_at >= decided_at",
            name="invalidation_follows_decision",
        ),
        # "does a decision still authorize this revision?" (spec section 74) is
        # answered per commit; the second index answers "is there still a valid
        # approval for this job at all?", which the report and the GitHub check ask
        # on every read.
        Index(None, "job_id", "commit_sha"),
        Index(None, "job_id", "invalidated_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("verification_jobs.id", ondelete="CASCADE"), nullable=False
    )
    #: The exact commit that was authorized. Mandatory (spec section 74).
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(ENUM_COLUMN_WIDTH), nullable=False)
    decided_by: Mapped[str] = mapped_column(String(200), nullable=False)
    decided_by_type: Mapped[str] = mapped_column(
        String(ENUM_COLUMN_WIDTH),
        nullable=False,
        server_default=text(f"'{ActorType.HUMAN.value}'"),
    )
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    comment: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidated_reason: Mapped[str | None] = mapped_column(String(2000), nullable=True)


class EngineeringDecisionRow(Base, TimestampMixin):
    """An explicit, attributable engineering decision (spec section 29).

    Decisions chain (``supersedes_id`` / ``superseded_by_id``) rather than being
    overwritten, so "why did Scout stop flagging this?" always has an answer.
    """

    __tablename__ = "engineering_decisions"
    __table_args__ = (Index(None, "scope"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    decided_by: Mapped[str] = mapped_column(String(200), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    #: ``owner/name`` or ``*`` for organisation-wide.
    scope: Mapped[str] = mapped_column(String(200), nullable=False, server_default=text("'*'"))
    tags: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    source_question_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_job_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_answer_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    supersedes_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    superseded_by_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    knowledge_document_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )


# ---------------------------------------------------------------------------
# knowledge_documents / knowledge_embeddings
# ---------------------------------------------------------------------------


class KnowledgeDocumentRow(Base, TimestampMixin):
    """A retrievable unit of engineering memory (spec sections 29 and 30).

    ``search_vector`` is a stored, generated ``tsvector`` rather than a
    query-time ``to_tsvector(body)`` call, so the GIN index is actually usable and
    Scout can search a large knowledge base without a sequential scan. Title is
    weighted ``A`` and body ``B``; the ranking function below relies on that.
    """

    __tablename__ = "knowledge_documents"
    __table_args__ = (
        *enum_checks("knowledge_documents"),
        CheckConstraint("authority_rank BETWEEN 0 AND 5", name="authority_rank_range"),
        Index(None, "scope"),
        Index(None, "authority_rank"),
        # The GIN index is the whole reason search_vector exists as a column: without
        # it every keyword search is a sequential scan plus a tsvector computation per
        # row, and knowledge search runs on every agent invocation.
        Index(None, "search_vector", postgresql_using="gin"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String(ENUM_COLUMN_WIDTH), nullable=False)
    authority: Mapped[str] = mapped_column(String(ENUM_COLUMN_WIDTH), nullable=False)
    #: Numeric mirror of ``authority`` for ranking; the enum's documented rank order.
    authority_rank: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    #: ``owner/name`` or ``*``.
    scope: Mapped[str] = mapped_column(String(200), nullable=False, server_default=text("'*'"))
    tags: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    source_reference: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    #: Identifier of the stored embedding, when one has been computed. Keyword search
    #: must work without it (spec section 30).
    embedding_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    superseded_by_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Generated, stored, weighted tsvector. Title Outranks body (``A`` over ``B``) so
    #: that a term in a title beats the same term buried in a long document. PostgreSQL
    #: maintains it, so a document can never be saved with a stale search vector.
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR,
        Computed(
            "setweight(to_tsvector('english', coalesce(title, '')), 'A') || "
            "setweight(to_tsvector('english', coalesce(body, '')), 'B')",
            persisted=True,
        ),
        nullable=False,
    )


class KnowledgeEmbeddingRow(Base, TimestampMixin):
    """A stored embedding vector for a knowledge document (spec section 30).

    The vector is JSON-encoded rather than a ``pgvector`` column so that Scout does
    not require an extension to start; the dimensional index and the vector type are
    an operational upgrade, not a schema prerequisite. ``dimensions`` is denormalised
    so a reader can reject a mismatched query vector without parsing the payload.
    """

    __tablename__ = "knowledge_embeddings"
    __table_args__ = (
        CheckConstraint("dimensions >= 1", name="dimensions_positive"),
        CheckConstraint("jsonb_typeof(vector) = 'array'", name="vector_is_array"),
        Index(None, "document_id"),
        Index(None, "model"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("knowledge_documents.id", ondelete="CASCADE"), nullable=False
    )
    #: Embedding model identifier, e.g. ``text-embedding-3-small``. Recorded so that a
    #: vector computed by an older model can be invalidated rather than silently mixed.
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    vector: Mapped[list[float]] = mapped_column(JSONB, nullable=False)
    #: Digest of the text that was embedded; a changed body means a stale vector.
    content_digest: Mapped[str | None] = mapped_column(String(128), nullable=True)


# ---------------------------------------------------------------------------
# notifications
# ---------------------------------------------------------------------------


class NotificationRow(Base, TimestampMixin):
    """One attempted delivery on one channel (spec section 47).

    Scout must not spam, so deduplication is expressed as a constraint:
    ``(dedup_key, channel)`` is unique. Two workers racing to notify about the same
    question at the same severity produce one row, not two messages.
    """

    __tablename__ = "notifications"
    __table_args__ = (
        *enum_checks("notifications"),
        UniqueConstraint("dedup_key", "channel", name="dedup_key_channel"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        # The unique constraint above cannot be indexed by a bare Index(None,
        # "dedup_key"): lookups by dedup key alone (the common case) would otherwise
        # not use the composite index.
        Index(None, "dedup_key"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    question_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    finding_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    channel: Mapped[str] = mapped_column(String(ENUM_COLUMN_WIDTH), nullable=False)
    severity: Mapped[str] = mapped_column(String(ENUM_COLUMN_WIDTH), nullable=False)
    subject: Mapped[str] = mapped_column(String(300), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    #: Stable key collapsing duplicates, e.g. ``question:{id}:severity``.
    dedup_key: Mapped[str] = mapped_column(String(300), nullable=False)

    status: Mapped[str] = mapped_column(
        String(ENUM_COLUMN_WIDTH),
        nullable=False,
        server_default=text(f"'{NotificationStatus.PENDING.value}'"),
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error: Mapped[str | None] = mapped_column(String(4000), nullable=True)


# ---------------------------------------------------------------------------
# artifacts
# ---------------------------------------------------------------------------


class ArtifactRow(Base, TimestampMixin):
    """Metadata for one stored blob (Rule 15, spec section 21).

    This table is the *only* place raw output is represented in the database, and it
    stores no output: ``sha256`` is the content address, ``storage_key`` is where the
    bytes live, and ``size_bytes`` lets retention report reclaimed space without
    fetching a single blob.
    """

    __tablename__ = "artifacts"
    __table_args__ = (
        *enum_checks("artifacts"),
        CheckConstraint("size_bytes >= 0", name="size_non_negative"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_hex"),
        Index(None, "job_id", "kind"),
        Index(None, "sha256"),
        Index(None, "expires_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    kind: Mapped[str] = mapped_column(String(ENUM_COLUMN_WIDTH), nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(200), nullable=False, server_default=text("'text/plain'")
    )
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: Hex SHA-256 of the stored bytes; also the content address.
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    #: Backend-specific locator, e.g. ``sha256/ab/cd/<digest>``.
    storage_key: Mapped[str] = mapped_column(String(1024), nullable=False)
    filename: Mapped[str | None] = mapped_column(String(300), nullable=True)

    tool_run_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    sandbox_run_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


# ---------------------------------------------------------------------------
# sandbox_runs
# ---------------------------------------------------------------------------


class SandboxRunRow(Base, TimestampMixin):
    """One sandbox instance and the commands executed inside it (spec section 53).

    ``limits`` and ``policy`` are stored as JSON *snapshots* taken when the sandbox
    was created, not as references to current configuration. A later configuration
    change must not retroactively alter the record of what isolation was actually
    enforced for a past run.
    """

    __tablename__ = "sandbox_runs"
    __table_args__ = (
        *enum_checks("sandbox_runs"),
        CheckConstraint(
            "duration_seconds IS NULL OR duration_seconds >= 0", name="duration_non_negative"
        ),
        Index(None, "job_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("verification_jobs.id", ondelete="CASCADE"), nullable=False
    )
    backend: Mapped[str] = mapped_column(String(ENUM_COLUMN_WIDTH), nullable=False)
    image: Mapped[str | None] = mapped_column(String(500), nullable=True)
    #: The limits and policy that were *enforced* for this run. No server default is
    #: possible: an empty object would claim the run had no limits, and the whole point of
    #: snapshotting them is that a later configuration change cannot rewrite history.
    limits: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(ENUM_COLUMN_WIDTH), nullable=False)
    #: Empty for the ``local`` backend, which has no workspace container.
    workspace_path: Mapped[str] = mapped_column(
        String(2048), nullable=False, server_default=text("''")
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    tool_run_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    error: Mapped[str | None] = mapped_column(String(8000), nullable=True)
    #: Container id or process group id, recorded so leaks can be found later.
    native_id: Mapped[str | None] = mapped_column(String(200), nullable=True)


# ---------------------------------------------------------------------------
# model_requests
# ---------------------------------------------------------------------------


class ModelRequestRow(Base, TimestampMixin):
    """One attempt to obtain a completion, for cost accounting (spec section 38).

    Prompt *content* is never stored — only ``prompt_digest``. Prompts can contain
    repository text and, more importantly, the table is not a secret store; a digest
    is enough to prove that two calls rendered the same input.
    """

    __tablename__ = "model_requests"
    __table_args__ = (
        *enum_checks("model_requests"),
        CheckConstraint("input_tokens >= 0", name="input_tokens_non_negative"),
        CheckConstraint("output_tokens >= 0", name="output_tokens_non_negative"),
        CheckConstraint("latency_seconds >= 0", name="latency_non_negative"),
        CheckConstraint("estimated_cost_usd >= 0", name="cost_non_negative"),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
        Index(None, "job_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    job_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    agent_run_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    agent: Mapped[str | None] = mapped_column(String(ENUM_COLUMN_WIDTH), nullable=True)

    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)

    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    latency_seconds: Mapped[float] = mapped_column(Float, nullable=False, server_default=text("0"))
    status: Mapped[str] = mapped_column(String(ENUM_COLUMN_WIDTH), nullable=False)
    estimated_cost_usd: Mapped[float] = mapped_column(
        Float, nullable=False, server_default=text("0")
    )
    prompt_digest: Mapped[str | None] = mapped_column(String(128), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    error: Mapped[str | None] = mapped_column(String(4000), nullable=True)


# ---------------------------------------------------------------------------
# audit_events
# ---------------------------------------------------------------------------


class AuditEventRow(Base, TimestampMixin):
    """A single audited action (spec section 43).

    Append-only by contract: :class:`~scout_persistence.repositories.audit.AuditEventRepository`
    exposes no update or delete method, and the integration test asserts that the
    absence is real rather than a convention.

    ``occurred_at`` is stored separately from ``created_at`` because an action can be
    recorded after the fact (a replay, a backfill) and the ledger should say when the
    action happened, not merely when the row landed.
    """

    __tablename__ = "audit_events"
    __table_args__ = (
        *enum_checks("audit_events"),
        Index(None, "resource", "resource_id"),
        Index(None, "job_id"),
        Index(None, "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    actor: Mapped[str] = mapped_column(String(200), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(ENUM_COLUMN_WIDTH), nullable=False)
    #: Dotted verb, e.g. ``job.transition``.
    action: Mapped[str] = mapped_column(String(200), nullable=False)
    resource: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(200), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=json_object_default()
    )
    job_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    correlation_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    source_ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
