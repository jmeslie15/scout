"""Explicit row-to-domain and domain-to-row conversion.

Why this module exists
----------------------

Scout's domain models are Pydantic value objects and its tables are SQLAlchemy
declarative rows. Neither is allowed to know about the other: the domain package has
no database driver in its dependency set, and the persistence layer must not smuggle
behaviour into a value object. So every crossing is written out by hand here.

The conversion is boring on purpose. There is no automatic model-to-ORM mapper, no
``from_orm``, and no dynamic attribute reflection, because a silent mapping bug in a
verification platform shows up as a *wrong report* rather than as a crash. Instead:

* Nested aggregates are stored as JSONB copies of the Pydantic models. An evidence
  reference is a pointer (an id, a path, a bounded excerpt), never raw output, so
  copying it into JSON cannot violate Rule 15 — but it does mean the row is the
  record, and the domain model is reconstructed field by field.
* Enums cross the boundary as their ``.value`` string, matching the ``VARCHAR`` +
  ``CHECK`` columns in :mod:`scout_persistence.tables`. The database therefore
  enforces the same vocabulary the domain declares.
* Anything the tables carry that the domain model does not know about (the
  ``repositories`` cache of detected stack, an audit row's ``updated_at``) is
  deliberately dropped rather than invented. A conversion that has to guess is a
  conversion that will guess wrong.

Round-tripping a domain model through a row and back produces an equal model; that
property is the primary unit test for this module and the reason the functions are
pairs rather than a single "write it however" helper.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import Any, Final, TypeVar, cast

from sqlalchemy import inspect as sa_inspect

from scout_domain.enums import (
    SEVERITY_ORDER,
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
from scout_domain.models.agent import AgentRun
from scout_domain.models.approval import Approval
from scout_domain.models.artifact import Artifact, ArtifactKind
from scout_domain.models.audit import AuditEvent
from scout_domain.models.base import ScoutModel
from scout_domain.models.evidence import EvidenceReference
from scout_domain.models.finding import Finding, FindingOrigin
from scout_domain.models.job import CommitRange, JobStage, JobStageOutcome, VerificationJob
from scout_domain.models.knowledge import (
    AUTHORITY_RANK,
    EngineeringDecision,
    KnowledgeAuthority,
    KnowledgeDocument,
    KnowledgeSourceType,
)
from scout_domain.models.model_request import ModelRequestRecord, ModelRequestStatus
from scout_domain.models.notification import NotificationRecord, NotificationStatus
from scout_domain.models.question import Question, QuestionAnswer
from scout_domain.models.repository import RepositoryRef
from scout_domain.models.sandbox import ResourceLimits, SandboxPolicy, SandboxRun, SandboxStatus
from scout_domain.models.tool import ToolInvocation, ToolResult, ToolSummary
from scout_persistence.tables import (
    AgentRunRow,
    ApprovalRow,
    ArtifactRow,
    AuditEventRow,
    EngineeringDecisionRow,
    FindingRow,
    JobRow,
    JobStageRow,
    KnowledgeDocumentRow,
    ModelRequestRow,
    NotificationRow,
    QuestionAnswerRow,
    QuestionRow,
    RepositoryRow,
    SandboxRunRow,
    ToolRunRow,
)

__all__ = [
    "ENTITY_MAPPINGS",
    "EntityMapping",
    "coerce",
    "dump_json",
    "dump_models",
    "from_agent_run",
    "from_approval",
    "from_artifact",
    "from_audit_event",
    "from_domain",
    "from_engineering_decision",
    "from_finding",
    "from_job_stage",
    "from_knowledge_document",
    "from_model_request",
    "from_notification",
    "from_question",
    "from_question_answer",
    "from_repository_ref",
    "from_sandbox_run",
    "from_tool_result",
    "from_verification_job",
    "load_json",
    "load_models",
    "mapping_for",
    "register",
    "to_agent_run",
    "to_approval",
    "to_artifact",
    "to_audit_event",
    "to_domain",
    "to_engineering_decision",
    "to_finding",
    "to_from_domain_row",
    "to_job_stage",
    "to_knowledge_document",
    "to_model_request",
    "to_notification",
    "to_question",
    "to_question_answer",
    "to_repository_ref",
    "to_sandbox_run",
    "to_tool_result",
    "to_verification_job",
]

ModelT = TypeVar("ModelT", bound=ScoutModel)
RowT = TypeVar("RowT")

# ---------------------------------------------------------------------------
# Primitive helpers
# ---------------------------------------------------------------------------


def coerce[EnumT: Enum](value: object, enum: type[EnumT]) -> EnumT:
    """Convert ``value`` (an enum instance or its string value) into ``enum``.

    Accepts both spellings because a row read from the database carries the plain
    string and a domain model carries the member; forcing callers to remember which
    side they are on is how a stray ``str`` ends up flowing into a report.
    """
    if isinstance(value, enum):
        return value
    return enum(str(value))


def dump_json(value: object) -> Any:
    """Return a JSONB-serializable copy of ``value``.

    Uses ``mode="json"`` so datetimes become ISO-8601 strings and enums become their
    values *before* PostgreSQL sees them. That keeps the JSONB in a row readable by
    ``psql`` and by a future non-Python reader, at the cost of a parse on the way
    back — a trade worth making for a table a human debugs during an incident.
    """
    if isinstance(value, ScoutModel):
        return value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): dump_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [dump_json(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def dump_models(models: Iterable[ScoutModel]) -> list[Any]:
    """Dump a sequence of domain models into a JSONB-ready list."""
    return [dump_json(model) for model in models]


def load_json(value: object) -> Any:
    """Return ``value`` as plain Python data, parsing it first when necessary.

    A JSONB column read through asyncpg arrives already decoded, but the same column
    read through a driver without a JSON codec arrives as text. Handling both keeps
    this module usable from a ``psql`` dump and from a test fixture string.
    """
    if isinstance(value, str):
        parsed: Any = json.loads(value)
        return parsed
    return value


def load_models[T: ScoutModel](value: object, model: type[T]) -> list[T]:
    """Reconstruct a list of domain models from a JSONB column value."""
    loaded = load_json(value) or []
    return [model.model_validate(item) for item in loaded]


def _dt(value: object) -> datetime:
    """Coerce a column value into a ``datetime``, failing loudly on anything else."""
    if isinstance(value, datetime):
        return value
    raise TypeError(f"expected a datetime column, got {type(value).__name__}")


def _dt_or_none(value: object) -> datetime | None:
    """Coerce a nullable timestamp column, preserving ``NULL``."""
    if value is None:
        return None
    return _dt(value)


def _text(value: object) -> str:
    """Coerce a non-nullable text column into ``str``."""
    if value is None:
        return ""
    return str(value)


def _text_or_none(value: object) -> str | None:
    """Coerce a nullable text column, mapping the empty-string default to ``None``."""
    if value is None:
        return None
    rendered = str(value)
    return rendered or None


def _obj(value: object) -> dict[str, Any]:
    """Coerce a JSONB object column into a ``dict``."""
    loaded = load_json(value)
    if loaded is None:
        return {}
    if isinstance(loaded, dict):
        return cast("dict[str, Any]", loaded)
    raise TypeError(f"expected a JSON object column, got {type(loaded).__name__}")


#: Columns that exist on a row but have no counterpart on the domain model. Named
#: explicitly so a reader can see that the omission is a decision, and so the parity
#: test knows which side may legitimately be larger.
_ROW_ONLY: Final[dict[str, frozenset[str]]] = {
    "repositories": frozenset(
        {"installation_id", "stack", "is_active", "github_id", "default_branch", "metadata"}
    ),
    "audit_events": frozenset({"updated_at"}),
    "verification_jobs": frozenset({"head_sha", "base_sha"}),
    "job_stages": frozenset({"duration_seconds"}),
    "findings": frozenset({"confidence_capped"}),
    "questions": frozenset({"accepted_answer_id"}),
    "notifications": frozenset({"updated_at"}),
    "artifacts": frozenset({"updated_at", "expires_at"}),
    "agent_runs": frozenset({"updated_at"}),
    "tool_runs": frozenset({"updated_at"}),
    "sandbox_runs": frozenset({"updated_at"}),
    "model_requests": frozenset({"updated_at"}),
    "knowledge_documents": frozenset({"updated_at", "authority_rank"}),
    "knowledge_embeddings": frozenset(
        {"updated_at", "model", "dimensions", "vector", "content_digest"}
    ),
    "engineering_decisions": frozenset({"updated_at"}),
    "question_answers": frozenset({"updated_at"}),
    "webhook_events": frozenset({"updated_at"}),
}

#: Domain-model fields that have no column because they are derived, not stored.
_DOMAIN_ONLY: Final[dict[str, frozenset[str]]] = {
    "job_stages": frozenset({"is_resolved"}),
    "knowledge_documents": frozenset({"authority_rank"}),
}


def _row_columns(row: object) -> dict[str, Any]:
    """Return the loaded column attributes of a mapped row, keyed by Python name."""
    mapper = sa_inspect(type(row))
    if not hasattr(mapper, "columns"):
        raise TypeError(f"{type(row).__name__} is not a mapped SQLAlchemy row")
    return {attr.key: getattr(row, attr.key) for attr in mapper.columns}  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# repositories
# ---------------------------------------------------------------------------


def to_repository_ref(row: RepositoryRow) -> RepositoryRef:
    """Build the domain repository identity from a ``repositories`` row.

    The row caches a detected technology stack and a GitHub installation id; neither
    belongs to :class:`RepositoryRef` (the installation id is a credential handle and
    must never reach an agent), so they are intentionally not carried across.
    """
    return RepositoryRef(
        full_name=row.full_name,
        github_id=row.github_id,
        default_branch=row.default_branch,
        is_active=row.is_active,
    )


def from_repository_ref(model: RepositoryRef) -> RepositoryRow:
    """Build a ``repositories`` row from a domain repository identity.

    :class:`RepositoryRef` carries no id of its own, so ``full_name`` is used as the
    row id. This conversion exists for round-trip fidelity and for seeding; normal
    writes go through :meth:`RepositoryRepository.upsert_by_full_name`, which mints a
    ``repo_`` ULID and preserves the id across renames.
    """
    return RepositoryRow(
        id=model.full_name,
        full_name=model.full_name,
        github_id=model.github_id,
        default_branch=model.default_branch,
        is_active=model.is_active,
        stack={},
        metadata_={},
    )


# ---------------------------------------------------------------------------
# verification_jobs
# ---------------------------------------------------------------------------


def to_verification_job(row: JobRow) -> VerificationJob:
    """Reconstruct a :class:`VerificationJob` from its row."""
    repository = RepositoryRef.model_validate(_obj(row.repository))
    return VerificationJob(
        id=row.id,
        repository=repository,
        repository_full_name=row.repository_full_name,
        pull_request_number=row.pull_request_number,
        pull_request_title=row.pull_request_title,
        pull_request_author=row.pull_request_author,
        pull_request_url=row.pull_request_url,
        commits=CommitRange.model_validate(_obj(row.commits)),
        github_delivery_id=row.github_delivery_id,
        status=coerce(row.status, JobStatus),
        risk_level=coerce(row.risk_level, RiskLevel),
        verification_status=coerce(row.verification_status, VerificationStatus),
        recommendation=_opt_enum(row.recommendation, Recommendation),
        attempt=row.attempt,
        max_attempts=row.max_attempts,
        created_at=_dt(row.created_at),
        started_at=_dt_or_none(row.started_at),
        completed_at=_dt_or_none(row.completed_at),
        lease_owner=row.lease_owner,
        lease_expires_at=_dt_or_none(row.lease_expires_at),
        correlation_id=row.correlation_id,
        failure_reason=row.failure_reason,
        cancel_reason=row.cancel_reason,
    )


def from_verification_job(model: VerificationJob) -> JobRow:
    """Build a ``verification_jobs`` row from a :class:`VerificationJob`.

    ``head_sha`` and ``base_sha`` are projected out of ``commits`` into their own
    columns: they are the keys of the PR-revision uniqueness constraint and of every
    "which revision did we verify?" query, and a JSONB expression index would be both
    slower and harder to lock.
    """
    return JobRow(
        id=model.id,
        repository=model.repository.model_dump(mode="json"),
        repository_full_name=model.repository_full_name,
        pull_request_number=model.pull_request_number,
        pull_request_title=model.pull_request_title,
        pull_request_author=model.pull_request_author,
        pull_request_url=model.pull_request_url,
        commits=model.commits.model_dump(mode="json"),
        head_sha=model.commits.head_sha,
        base_sha=model.commits.base_sha,
        github_delivery_id=model.github_delivery_id,
        status=model.status.value,
        risk_level=model.risk_level.value,
        verification_status=model.verification_status.value,
        recommendation=None if model.recommendation is None else model.recommendation.value,
        attempt=model.attempt,
        max_attempts=model.max_attempts,
        created_at=model.created_at,
        started_at=model.started_at,
        completed_at=model.completed_at,
        lease_owner=model.lease_owner,
        lease_expires_at=model.lease_expires_at,
        correlation_id=model.correlation_id,
        failure_reason=model.failure_reason,
        cancel_reason=model.cancel_reason,
    )


# ---------------------------------------------------------------------------
# job_stages
# ---------------------------------------------------------------------------


def to_job_stage(row: JobStageRow) -> JobStage:
    """Reconstruct a :class:`JobStage` from its row."""
    return JobStage(
        id=row.id,
        job_id=row.job_id,
        stage=coerce(row.stage, JobStatus),
        outcome=coerce(row.outcome, JobStageOutcome),
        attempt=row.attempt,
        started_at=_dt_or_none(row.started_at),
        completed_at=_dt_or_none(row.completed_at),
        duration_seconds=row.duration_seconds,
        error=_text_or_none(row.error),
        skip_reason=_text_or_none(row.skip_reason),
        artifact_ids=[str(item) for item in (load_json(row.artifact_ids) or [])],
        created_at=_dt(row.created_at),
        updated_at=_dt(row.updated_at),
    )


def from_job_stage(model: JobStage) -> JobStageRow:
    """Build a ``job_stages`` row from a :class:`JobStage`."""
    return JobStageRow(
        id=model.id,
        job_id=model.job_id,
        stage=model.stage.value,
        outcome=model.outcome.value,
        attempt=model.attempt,
        started_at=model.started_at,
        completed_at=model.completed_at,
        duration_seconds=model.duration_seconds,
        error=model.error,
        skip_reason=model.skip_reason,
        artifact_ids=list(model.artifact_ids),
        created_at=model.created_at,
        updated_at=model.updated_at,
    )


# ---------------------------------------------------------------------------
# findings
# ---------------------------------------------------------------------------


def to_finding(row: FindingRow) -> Finding:
    """Reconstruct a :class:`Finding` from its row.

    The evidence array is rebuilt through the domain validator, so a row whose
    evidence is malformed is rejected here rather than shipped in a report.
    """
    return Finding(
        id=row.id,
        job_id=row.job_id,
        severity=coerce(row.severity, Severity),
        category=coerce(row.category, FindingCategory),
        title=row.title,
        description=row.description,
        repository=row.repository,
        file=row.file,
        line_start=row.line_start,
        line_end=row.line_end,
        evidence=load_models(row.evidence, EvidenceReference),
        recommendation=row.recommendation,
        confidence=row.confidence,
        requires_human=row.requires_human,
        origin=coerce(row.origin, FindingOrigin),
        source=row.source,
        source_run_id=row.source_run_id,
        created_at=_dt(row.created_at),
        confidence_capped=row.confidence_capped,
    )


def from_finding(model: Finding) -> FindingRow:
    """Build a ``findings`` row from a :class:`Finding`."""
    return FindingRow(
        id=model.id,
        job_id=model.job_id,
        severity=model.severity.value,
        category=model.category.value,
        title=model.title,
        description=model.description,
        repository=model.repository,
        file=model.file,
        line_start=model.line_start,
        line_end=model.line_end,
        evidence=dump_models(model.evidence),
        recommendation=model.recommendation,
        confidence=model.confidence,
        requires_human=model.requires_human,
        origin=model.origin.value,
        source=model.source,
        source_run_id=model.source_run_id,
        created_at=model.created_at,
        confidence_capped=model.confidence_capped,
    )


# ---------------------------------------------------------------------------
# questions / answers
# ---------------------------------------------------------------------------


def to_question(row: QuestionRow, answers: Iterable[QuestionAnswerRow] = ()) -> Question:
    """Reconstruct a :class:`Question` from its row plus its answer rows.

    Answers live in ``question_answers`` rather than in a JSONB column on the
    question, so the caller must supply them; omitting them yields a question with no
    answers, which the domain model accepts only while the status is ``OPEN``.
    """
    ordered = sorted(answers, key=lambda answer: (_dt(answer.answered_at), answer.id))
    return Question(
        id=row.id,
        job_id=row.job_id,
        title=row.title,
        description=row.description,
        context=row.context,
        investigation_summary=row.investigation_summary,
        evidence=load_models(row.evidence, EvidenceReference),
        severity=coerce(row.severity, Severity),
        options=[str(item) for item in (load_json(row.options) or [])],
        default_assumption=row.default_assumption,
        status=coerce(row.status, QuestionStatus),
        created_by=row.created_by,
        created_by_type=coerce(row.created_by_type, ActorType),
        created_at=_dt(row.created_at),
        answers=[to_question_answer(answer) for answer in ordered],
        accepted_answer_id=row.accepted_answer_id,
    )


def from_question(model: Question) -> QuestionRow:
    """Build a ``questions`` row from a :class:`Question`.

    ``severity_rank`` is not stored: :data:`scout_domain.enums.SEVERITY_ORDER` is the
    canonical ordering and is applied in Python, so there is no second copy of the
    ranking that could drift.
    """
    return QuestionRow(
        id=model.id,
        job_id=model.job_id,
        title=model.title,
        description=model.description,
        context=model.context,
        investigation_summary=model.investigation_summary,
        evidence=dump_models(model.evidence),
        severity=model.severity.value,
        options=list(model.options),
        default_assumption=model.default_assumption,
        status=model.status.value,
        created_by=model.created_by,
        created_by_type=model.created_by_type.value,
        created_at=model.created_at,
        accepted_answer_id=model.accepted_answer_id,
    )


def to_question_answer(row: QuestionAnswerRow) -> QuestionAnswer:
    """Reconstruct a :class:`QuestionAnswer` from its row."""
    return QuestionAnswer(
        id=row.id,
        question_id=row.question_id,
        body=row.body,
        answered_by=row.answered_by,
        answered_by_type=coerce(row.answered_by_type, ActorType),
        answered_at=_dt(row.answered_at),
        promote_to_knowledge=row.promote_to_knowledge,
        knowledge_scope=row.knowledge_scope,
    )


def from_question_answer(model: QuestionAnswer) -> QuestionAnswerRow:
    """Build a ``question_answers`` row from a :class:`QuestionAnswer`."""
    return QuestionAnswerRow(
        id=model.id,
        question_id=model.question_id,
        body=model.body,
        answered_by=model.answered_by,
        answered_by_type=model.answered_by_type.value,
        answered_at=model.answered_at,
        promote_to_knowledge=model.promote_to_knowledge,
        knowledge_scope=model.knowledge_scope,
    )


# ---------------------------------------------------------------------------
# approvals
# ---------------------------------------------------------------------------


def to_approval(row: ApprovalRow) -> Approval:
    """Reconstruct an :class:`Approval` from its row."""
    return Approval(
        id=row.id,
        job_id=row.job_id,
        commit_sha=row.commit_sha,
        decision=coerce(row.decision, ApprovalDecision),
        decided_by=row.decided_by,
        decided_by_type=coerce(row.decided_by_type, ActorType),
        decided_at=_dt(row.decided_at),
        comment=row.comment,
        invalidated_at=_dt_or_none(row.invalidated_at),
        invalidated_reason=_text_or_none(row.invalidated_reason),
    )


def from_approval(model: Approval) -> ApprovalRow:
    """Build an ``approvals`` row from an :class:`Approval`."""
    return ApprovalRow(
        id=model.id,
        job_id=model.job_id,
        commit_sha=model.commit_sha,
        decision=model.decision.value,
        decided_by=model.decided_by,
        decided_by_type=model.decided_by_type.value,
        decided_at=model.decided_at,
        comment=model.comment,
        invalidated_at=model.invalidated_at,
        invalidated_reason=model.invalidated_reason,
    )


# ---------------------------------------------------------------------------
# artifacts
# ---------------------------------------------------------------------------


def to_artifact(row: ArtifactRow) -> Artifact:
    """Reconstruct an :class:`Artifact` from its row."""
    return Artifact(
        id=row.id,
        job_id=row.job_id,
        kind=coerce(row.kind, ArtifactKind),
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        sha256=row.sha256,
        storage_key=row.storage_key,
        filename=row.filename,
        tool_run_id=row.tool_run_id,
        agent_run_id=row.agent_run_id,
        sandbox_run_id=row.sandbox_run_id,
        created_at=_dt(row.created_at),
        expires_at=_dt_or_none(row.expires_at),
    )


def from_artifact(model: Artifact) -> ArtifactRow:
    """Build an ``artifacts`` row from an :class:`Artifact`."""
    return ArtifactRow(
        id=model.id,
        job_id=model.job_id,
        kind=model.kind.value,
        content_type=model.content_type,
        size_bytes=model.size_bytes,
        sha256=model.sha256,
        storage_key=model.storage_key,
        filename=model.filename,
        tool_run_id=model.tool_run_id,
        agent_run_id=model.agent_run_id,
        sandbox_run_id=model.sandbox_run_id,
        created_at=model.created_at,
        expires_at=model.expires_at,
    )


# ---------------------------------------------------------------------------
# tool_runs
# ---------------------------------------------------------------------------


def to_tool_result(row: ToolRunRow) -> ToolResult:
    """Reconstruct a :class:`ToolResult` from its row."""
    return ToolResult(
        id=row.id,
        job_id=row.job_id,
        stage=row.stage,
        tool=row.tool,
        version=row.version,
        command=row.command,
        argv=[str(item) for item in (load_json(row.argv) or [])],
        exit_code=row.exit_code,
        status=coerce(row.status, ToolStatus),
        started_at=_dt(row.started_at),
        completed_at=_dt(row.completed_at),
        duration_seconds=row.duration_seconds,
        stdout_artifact_id=row.stdout_artifact_id,
        stderr_artifact_id=row.stderr_artifact_id,
        report_artifact_id=row.report_artifact_id,
        summary=ToolSummary.model_validate(_obj(row.summary)),
        error=_text_or_none(row.error),
        infrastructure_failure=row.infrastructure_failure,
        sandbox_run_id=row.sandbox_run_id,
        narrative=row.narrative,
        metadata=_obj(row.metadata_),
    )


def from_tool_result(model: ToolResult) -> ToolRunRow:
    """Build a ``tool_runs`` row from a :class:`ToolResult`.

    ``capability`` has no home on :class:`ToolResult` (a result states what happened,
    not what it was permitted to do), so the row records the capability the invocation
    was authorized under, supplied by the caller.
    """
    return ToolRunRow(
        id=model.id,
        job_id=model.job_id,
        stage=model.stage,
        tool=model.tool,
        capability=Capability.TEST_RUN.value,
        invocation={},
        version=model.version,
        command=model.command,
        argv=list(model.argv),
        exit_code=model.exit_code,
        status=model.status.value,
        started_at=model.started_at,
        completed_at=model.completed_at,
        duration_seconds=model.duration_seconds,
        stdout_artifact_id=model.stdout_artifact_id,
        stderr_artifact_id=model.stderr_artifact_id,
        report_artifact_id=model.report_artifact_id,
        summary=model.summary.model_dump(mode="json"),
        error=model.error,
        infrastructure_failure=model.infrastructure_failure,
        sandbox_run_id=model.sandbox_run_id,
        narrative=model.narrative,
        metadata_=dict(model.metadata),
    )


def invocation_to_json(invocation: ToolInvocation) -> dict[str, Any]:
    """Serialize a :class:`ToolInvocation` for the ``tool_runs.invocation`` column."""
    dumped: dict[str, Any] = invocation.model_dump(mode="json")
    return dumped


def tool_run_capability(invocation: ToolInvocation | None) -> str:
    """Return the capability to record for a tool run, defaulting to test execution."""
    if invocation is None:
        return Capability.TEST_RUN.value
    return invocation.capability.value


# ---------------------------------------------------------------------------
# agent_runs
# ---------------------------------------------------------------------------


def to_agent_run(row: AgentRunRow) -> AgentRun:
    """Reconstruct an :class:`AgentRun` from its row."""
    return AgentRun(
        id=row.id,
        job_id=row.job_id,
        role=coerce(row.role, AgentRole),
        status=coerce(row.status, AgentStatus),
        prompt_version=row.prompt_version,
        provider=row.provider,
        model=row.model,
        started_at=_dt(row.started_at),
        completed_at=_dt_or_none(row.completed_at),
        latency_seconds=row.latency_seconds,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        estimated_cost_usd=row.estimated_cost_usd,
        context_digest=row.context_digest,
        result_artifact_id=row.result_artifact_id,
        error=_text_or_none(row.error),
        created_at=_dt(row.created_at),
    )


def from_agent_run(model: AgentRun) -> AgentRunRow:
    """Build an ``agent_runs`` row from an :class:`AgentRun`."""
    return AgentRunRow(
        id=model.id,
        job_id=model.job_id,
        role=model.role.value,
        status=model.status.value,
        prompt_version=model.prompt_version,
        provider=model.provider,
        model=model.model,
        started_at=model.started_at,
        completed_at=model.completed_at,
        latency_seconds=model.latency_seconds,
        input_tokens=model.input_tokens,
        output_tokens=model.output_tokens,
        estimated_cost_usd=model.estimated_cost_usd,
        context_digest=model.context_digest,
        result_artifact_id=model.result_artifact_id,
        error=model.error,
        created_at=model.created_at,
    )


# ---------------------------------------------------------------------------
# model_requests
# ---------------------------------------------------------------------------


def to_model_request(row: ModelRequestRow) -> ModelRequestRecord:
    """Reconstruct a :class:`ModelRequestRecord` from its row."""
    return ModelRequestRecord(
        id=row.id,
        job_id=row.job_id,
        agent_run_id=row.agent_run_id,
        agent=_opt_enum(row.agent, AgentRole),
        provider=row.provider,
        model=row.model,
        prompt_version=row.prompt_version,
        input_tokens=row.input_tokens,
        output_tokens=row.output_tokens,
        latency_seconds=row.latency_seconds,
        status=coerce(row.status, ModelRequestStatus),
        estimated_cost_usd=row.estimated_cost_usd,
        prompt_digest=row.prompt_digest,
        attempt=row.attempt,
        error=_text_or_none(row.error),
        created_at=_dt(row.created_at),
    )


def from_model_request(model: ModelRequestRecord) -> ModelRequestRow:
    """Build a ``model_requests`` row from a :class:`ModelRequestRecord`."""
    return ModelRequestRow(
        id=model.id,
        job_id=model.job_id,
        agent_run_id=model.agent_run_id,
        agent=None if model.agent is None else model.agent.value,
        provider=model.provider,
        model=model.model,
        prompt_version=model.prompt_version,
        input_tokens=model.input_tokens,
        output_tokens=model.output_tokens,
        latency_seconds=model.latency_seconds,
        status=model.status.value,
        estimated_cost_usd=model.estimated_cost_usd,
        prompt_digest=model.prompt_digest,
        attempt=model.attempt,
        error=model.error,
        created_at=model.created_at,
    )


# ---------------------------------------------------------------------------
# notifications
# ---------------------------------------------------------------------------


def to_notification(row: NotificationRow) -> NotificationRecord:
    """Reconstruct a :class:`NotificationRecord` from its row."""
    return NotificationRecord(
        id=row.id,
        job_id=row.job_id,
        question_id=row.question_id,
        finding_id=row.finding_id,
        channel=coerce(row.channel, NotificationChannel),
        severity=coerce(row.severity, Severity),
        subject=row.subject,
        body=row.body,
        dedup_key=row.dedup_key,
        status=coerce(row.status, NotificationStatus),
        attempts=row.attempts,
        sent_at=_dt_or_none(row.sent_at),
        error=_text_or_none(row.error),
        created_at=_dt(row.created_at),
    )


def from_notification(model: NotificationRecord) -> NotificationRow:
    """Build a ``notifications`` row from a :class:`NotificationRecord`."""
    return NotificationRow(
        id=model.id,
        job_id=model.job_id,
        question_id=model.question_id,
        finding_id=model.finding_id,
        channel=model.channel.value,
        severity=model.severity.value,
        subject=model.subject,
        body=model.body,
        dedup_key=model.dedup_key,
        status=model.status.value,
        attempts=model.attempts,
        sent_at=model.sent_at,
        error=model.error,
        created_at=model.created_at,
    )


# ---------------------------------------------------------------------------
# knowledge
# ---------------------------------------------------------------------------


def to_knowledge_document(row: KnowledgeDocumentRow) -> KnowledgeDocument:
    """Reconstruct a :class:`KnowledgeDocument` from its row.

    ``authority_rank`` is recomputed from the enum rather than read from the column;
    the column exists to make ranking a SQL ``ORDER BY`` and is written from the same
    table, so the two cannot disagree unless a row was hand-edited.
    """
    return KnowledgeDocument(
        id=row.id,
        title=row.title,
        body=row.body,
        source_type=coerce(row.source_type, KnowledgeSourceType),
        authority=coerce(row.authority, KnowledgeAuthority),
        scope=row.scope,
        tags=[str(item) for item in (load_json(row.tags) or [])],
        source_reference=row.source_reference,
        embedding_id=row.embedding_id,
        created_at=_dt(row.created_at),
        updated_at=_dt(row.updated_at),
        superseded_by_id=row.superseded_by_id,
        expires_at=_dt_or_none(row.expires_at),
    )


def from_knowledge_document(model: KnowledgeDocument) -> KnowledgeDocumentRow:
    """Build a ``knowledge_documents`` row from a :class:`KnowledgeDocument`."""
    return KnowledgeDocumentRow(
        id=model.id,
        title=model.title,
        body=model.body,
        source_type=model.source_type.value,
        authority=model.authority.value,
        authority_rank=AUTHORITY_RANK[model.authority],
        scope=model.scope,
        tags=list(model.tags),
        source_reference=model.source_reference,
        embedding_id=model.embedding_id,
        created_at=model.created_at,
        updated_at=model.updated_at,
        superseded_by_id=model.superseded_by_id,
        expires_at=model.expires_at,
    )


def to_engineering_decision(row: EngineeringDecisionRow) -> EngineeringDecision:
    """Reconstruct an :class:`EngineeringDecision` from its row."""
    return EngineeringDecision(
        id=row.id,
        statement=row.statement,
        rationale=row.rationale,
        decided_by=row.decided_by,
        decided_at=_dt(row.decided_at),
        scope=row.scope,
        tags=[str(item) for item in (load_json(row.tags) or [])],
        source_question_id=row.source_question_id,
        source_job_id=row.source_job_id,
        source_answer_id=row.source_answer_id,
        supersedes_id=row.supersedes_id,
        superseded_by_id=row.superseded_by_id,
        knowledge_document_ids=[
            str(item) for item in (load_json(row.knowledge_document_ids) or [])
        ],
    )


def from_engineering_decision(model: EngineeringDecision) -> EngineeringDecisionRow:
    """Build an ``engineering_decisions`` row from an :class:`EngineeringDecision`."""
    return EngineeringDecisionRow(
        id=model.id,
        statement=model.statement,
        rationale=model.rationale,
        decided_by=model.decided_by,
        decided_at=model.decided_at,
        scope=model.scope,
        tags=list(model.tags),
        source_question_id=model.source_question_id,
        source_job_id=model.source_job_id,
        source_answer_id=model.source_answer_id,
        supersedes_id=model.supersedes_id,
        superseded_by_id=model.superseded_by_id,
        knowledge_document_ids=list(model.knowledge_document_ids),
    )


# ---------------------------------------------------------------------------
# sandbox_runs
# ---------------------------------------------------------------------------


def to_sandbox_run(row: SandboxRunRow) -> SandboxRun:
    """Reconstruct a :class:`SandboxRun` from its row.

    ``limits`` and ``policy`` are read back as snapshots, which is the point: they
    record what was enforced for this run, not what is configured now.
    """
    return SandboxRun(
        id=row.id,
        job_id=row.job_id,
        backend=coerce(row.backend, SandboxBackendKind),
        image=row.image,
        limits=ResourceLimits.model_validate(_obj(row.limits)),
        policy=SandboxPolicy.model_validate(_obj(row.policy)),
        status=coerce(row.status, SandboxStatus),
        workspace_path=row.workspace_path,
        started_at=_dt(row.started_at),
        completed_at=_dt_or_none(row.completed_at),
        duration_seconds=row.duration_seconds,
        tool_run_ids=[str(item) for item in (load_json(row.tool_run_ids) or [])],
        error=_text_or_none(row.error),
        native_id=row.native_id,
    )


def from_sandbox_run(model: SandboxRun) -> SandboxRunRow:
    """Build a ``sandbox_runs`` row from a :class:`SandboxRun`."""
    return SandboxRunRow(
        id=model.id,
        job_id=model.job_id,
        backend=model.backend.value,
        image=model.image,
        limits=model.limits.model_dump(mode="json"),
        policy=model.policy.model_dump(mode="json"),
        status=model.status.value,
        workspace_path=model.workspace_path,
        started_at=model.started_at,
        completed_at=model.completed_at,
        duration_seconds=model.duration_seconds,
        tool_run_ids=list(model.tool_run_ids),
        error=model.error,
        native_id=model.native_id,
    )


# ---------------------------------------------------------------------------
# audit_events
# ---------------------------------------------------------------------------


def to_audit_event(row: AuditEventRow) -> AuditEvent:
    """Reconstruct an :class:`AuditEvent` from its row."""
    return AuditEvent(
        id=row.id,
        actor=row.actor,
        actor_type=coerce(row.actor_type, ActorType),
        action=row.action,
        resource=row.resource,
        resource_id=row.resource_id,
        timestamp=_dt(row.occurred_at),
        metadata=_obj(row.metadata_),
        job_id=row.job_id,
        correlation_id=row.correlation_id,
        source_ip=row.source_ip,
    )


def from_audit_event(model: AuditEvent) -> AuditEventRow:
    """Build an ``audit_events`` row from an :class:`AuditEvent`."""
    return AuditEventRow(
        id=model.id,
        actor=model.actor,
        actor_type=model.actor_type.value,
        action=model.action,
        resource=model.resource,
        resource_id=model.resource_id,
        occurred_at=model.timestamp,
        metadata_=dict(model.metadata),
        job_id=model.job_id,
        correlation_id=model.correlation_id,
        source_ip=model.source_ip,
    )


def _opt_enum[EnumT: Enum](value: object, enum: type[EnumT]) -> EnumT | None:
    """Convert a nullable enum column, preserving ``NULL``."""
    if value is None:
        return None
    return coerce(value, enum)


# ---------------------------------------------------------------------------
# The shared interface
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntityMapping[RowT, ModelT]:
    """One row/model pair with the two directions of its conversion.

    ``to_domain`` takes an optional second argument on some entities (a question's
    answers arrive from a second table), so the field is typed as a callable that
    accepts one required argument rather than a bare function.
    """

    name: str
    row_type: type[RowT]
    model_type: type[ModelT]
    table: str
    to_domain: Callable[[Any], ModelT]
    from_domain: Callable[[ModelT], RowT]
    domain_only_fields: frozenset[str] = frozenset()
    row_only_fields: frozenset[str] = frozenset()


#: Every row/model pair Scout can convert, keyed by table name. The registry is built
#: by :func:`register`, which derives the table name and the field-parity bookkeeping
#: from SQLAlchemy's mapper, so adding an entity is a single call and nothing here can
#: drift from the table definitions.
ENTITY_MAPPINGS: Final[dict[str, EntityMapping[Any, Any]]] = {}

#: Reverse index over the same registry, used by :func:`from_domain`.
ENTITY_MAPPINGS_BY_MODEL: Final[dict[type[Any], EntityMapping[Any, Any]]] = {}


def register[RowT, ModelT: ScoutModel](
    row_type: type[RowT],
    model_type: type[ModelT],
    to_domain: Callable[[Any], ModelT],
    from_domain: Callable[[ModelT], RowT],
) -> EntityMapping[RowT, ModelT]:
    """Register one row/model pair and return its mapping.

    ``domain_only_fields`` and ``row_only_fields`` are computed here rather than
    declared by hand: the parity contract test reads them to decide which side may be
    larger, so they have to come from the real column sets.
    """
    mapper = sa_inspect(row_type)
    table = mapper.local_table.name  # type: ignore[union-attr]
    row_fields = frozenset(column.key for column in mapper.columns)  # type: ignore[union-attr]
    domain_fields = frozenset(model_type.model_fields)
    mapping = EntityMapping(
        name=model_type.__name__,
        row_type=row_type,
        model_type=model_type,
        table=table,
        to_domain=to_domain,
        from_domain=from_domain,
        domain_only_fields=domain_fields - row_fields,
        row_only_fields=row_fields - domain_fields,
    )
    ENTITY_MAPPINGS[table] = mapping
    ENTITY_MAPPINGS_BY_MODEL[model_type] = mapping
    return mapping


register(RepositoryRow, RepositoryRef, to_repository_ref, from_repository_ref)
register(JobRow, VerificationJob, to_verification_job, from_verification_job)
register(JobStageRow, JobStage, to_job_stage, from_job_stage)
register(FindingRow, Finding, to_finding, from_finding)
register(QuestionRow, Question, to_question, from_question)
register(QuestionAnswerRow, QuestionAnswer, to_question_answer, from_question_answer)
register(ApprovalRow, Approval, to_approval, from_approval)
register(ArtifactRow, Artifact, to_artifact, from_artifact)
register(ToolRunRow, ToolResult, to_tool_result, from_tool_result)
register(AgentRunRow, AgentRun, to_agent_run, from_agent_run)
register(ModelRequestRow, ModelRequestRecord, to_model_request, from_model_request)
register(NotificationRow, NotificationRecord, to_notification, from_notification)
register(KnowledgeDocumentRow, KnowledgeDocument, to_knowledge_document, from_knowledge_document)
register(
    EngineeringDecisionRow,
    EngineeringDecision,
    to_engineering_decision,
    from_engineering_decision,
)
register(SandboxRunRow, SandboxRun, to_sandbox_run, from_sandbox_run)
register(AuditEventRow, AuditEvent, to_audit_event, from_audit_event)


def mapping_for(row_or_table: str | type[Any]) -> EntityMapping[Any, Any]:
    """Return the :class:`EntityMapping` for a table name or a row class."""
    if isinstance(row_or_table, str):
        try:
            return ENTITY_MAPPINGS[row_or_table]
        except KeyError as exc:
            raise KeyError(f"no entity mapping registered for table {row_or_table!r}") from exc
    mapper = sa_inspect(row_or_table)
    return ENTITY_MAPPINGS[mapper.local_table.name]


def to_domain(row: Any) -> Any:
    """Convert any mapped Scout row into its domain model."""
    return mapping_for(type(row)).to_domain(row)


def from_domain(model: Any) -> Any:
    """Convert any Scout domain model into its row."""
    mapping = ENTITY_MAPPINGS_BY_MODEL.get(type(model))
    if mapping is None:
        raise KeyError(f"no entity mapping registered for model {type(model).__name__!r}")
    return mapping.from_domain(model)


def to_from_domain_row(model: ScoutModel) -> Any:
    """Round-trip ``model`` through its row representation and back.

    Used by the unit tests and by anything that wants to normalise a value the same way
    persistence will, without opening a session.
    """
    return to_domain(from_domain(model))


#: Severity ordering, re-exported so callers that only need to sort findings do not
#: have to reach into the domain package. The ranking itself is not duplicated.
SEVERITY_RANK: Final[dict[Severity, int]] = dict(SEVERITY_ORDER)
