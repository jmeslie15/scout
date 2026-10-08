"""Enum vocabulary round-trips.

Scout stores every enum as a ``VARCHAR`` holding the member's ``.value``. That choice is
what makes a new state a plain, reversible migration instead of an ``ALTER TYPE``, but it
also means the database and the Python enum have to agree on the *spelling*. A member
whose value were accidentally written as its name (``JobStatus.PREPARING.value`` is
``"PREPARING"``, but ``AgentRole.REPOSITORY_ANALYZER.value`` is
``"repository_analyzer"``) would round-trip through Python and break only in the
database, at query time, in production.

These tests pin the actual stored spelling for every enum Scout persists, so a change
that looks cosmetic in Python shows up here first.
"""

from __future__ import annotations

import pytest

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
from scout_persistence.mapping import coerce
from scout_persistence.tables import ENUM_COLUMN_WIDTH, ENUM_SPECS, WEBHOOK_EVENT_STATUSES

#: One sample member per persisted enum. Used to prove ``coerce`` accepts both the member
#: and its string spelling, which is what lets the same value cross the boundary in
#: either direction.
SAMPLES: list[tuple[type[object], str]] = [
    (ActorType, ActorType.WEBHOOK.value),
    (AgentRole, AgentRole.FEATURE_VERIFICATION.value),
    (AgentStatus, AgentStatus.NEEDS_INVESTIGATION.value),
    (ApprovalDecision, ApprovalDecision.REQUEST_CHANGES.value),
    (ArtifactKind, ArtifactKind.TOOL_STDOUT.value),
    (Capability, Capability.TEST_RUN.value),
    (FindingCategory, FindingCategory.API_BEHAVIOR.value),
    (FindingOrigin, FindingOrigin.TOOL.value),
    (JobStageOutcome, JobStageOutcome.SUCCEEDED.value),
    (JobStatus, JobStatus.SECURITY_SCANNING.value),
    (KnowledgeAuthority, KnowledgeAuthority.HUMAN_DECISION.value),
    (KnowledgeSourceType, KnowledgeSourceType.REPOSITORY_DOCUMENTATION.value),
    (ModelRequestStatus, ModelRequestStatus.RATE_LIMITED.value),
    (NotificationChannel, NotificationChannel.GOOGLE_CHAT.value),
    (NotificationStatus, NotificationStatus.SUPPRESSED.value),
    (QuestionStatus, QuestionStatus.SUPERSEDED.value),
    (Recommendation, Recommendation.REQUIRES_HUMAN_REVIEW.value),
    (RiskLevel, RiskLevel.CRITICAL.value),
    (SandboxBackendKind, SandboxBackendKind.DOCKER.value),
    (SandboxStatus, SandboxStatus.TIMEOUT.value),
    (Severity, Severity.CRITICAL.value),
    (ToolStatus, ToolStatus.NOT_APPLICABLE.value),
    (VerificationStatus, VerificationStatus.PASSED_WITH_WARNINGS.value),
]


@pytest.mark.parametrize(("enum_type", "value"), SAMPLES)
def test_enum_value_survives_a_round_trip(enum_type: type[object], value: str) -> None:
    """``str(enum)`` -> member -> ``.value`` returns the original spelling.

    This is the exact journey a value makes: rendered to a column, read back as text,
    and reconstituted. A ``StrEnum`` whose value differed from its name would pass a
    naive ``str()`` comparison and fail here.
    """
    member = coerce(value, enum_type)  # type: ignore[arg-type]
    assert member.value == value
    assert coerce(member, enum_type) is member  # type: ignore[arg-type]


@pytest.mark.parametrize(("enum_type", "value"), SAMPLES)
def test_enum_values_fit_the_column_width(enum_type: type[object], value: str) -> None:
    """No persisted enum value can be longer than the column that holds it.

    The wider of ``String(32)``/``String(100)``/``String(200)`` columns is declared per
    table; this asserts the ``String(32)`` contract that the great majority use, so a
    new member with a 40-character value is caught before PostgreSQL truncates or
    rejects it.
    """
    for member in enum_type:  # type: ignore[attr-defined]
        rendered: str = member.value
        assert len(rendered) <= ENUM_COLUMN_WIDTH, (
            f"{enum_type.__name__}.{member.name} value {rendered!r} is "
            f"{len(rendered)} characters, wider than String({ENUM_COLUMN_WIDTH})"
        )
    assert len(value) <= ENUM_COLUMN_WIDTH


def test_every_persisted_enum_is_covered_by_a_sample() -> None:
    """Every enum the schema constrains has a sample above.

    Adding a column to ``ENUM_SPECS`` without adding its enum here would make the other
    tests silently stop covering it.
    """
    covered = {enum_type for enum_type, _ in SAMPLES}
    constrained = set(ENUM_SPECS.values())
    assert constrained <= covered, (
        f"enums with a CHECK constraint but no round-trip sample: "
        f"{sorted(t.__name__ for t in constrained - covered)}"
    )


def test_enum_columns_are_declared_for_every_persisted_enum() -> None:
    """The registry covers the vocabulary Scout actually writes.

    ``ENUM_SPECS`` is the single place a constrained column's vocabulary is declared, so
    an enum that Scout persists and the registry does not mention would be stored as
    unconstrained text — precisely the drift this whole scheme exists to prevent.
    """
    persisted = {
        ActorType,
        AgentRole,
        AgentStatus,
        ApprovalDecision,
        ArtifactKind,
        Capability,
        FindingCategory,
        FindingOrigin,
        JobStageOutcome,
        JobStatus,
        KnowledgeAuthority,
        KnowledgeSourceType,
        ModelRequestStatus,
        NotificationChannel,
        NotificationStatus,
        QuestionStatus,
        Recommendation,
        RiskLevel,
        SandboxBackendKind,
        SandboxStatus,
        Severity,
        ToolStatus,
        VerificationStatus,
    }
    assert set(ENUM_SPECS.values()) == persisted


def test_webhook_event_statuses_are_local_and_lowercase_free() -> None:
    """The delivery-receipt vocabulary is local to the ingestion path.

    It is not a domain enum, so it is declared beside the table. Pinning it here means the
    ``CHECK`` constraint and the repository's ``status="RECEIVED"`` literal cannot drift
    apart silently.
    """
    assert WEBHOOK_EVENT_STATUSES == ("RECEIVED", "PROCESSED", "DUPLICATE", "FAILED")


def test_job_status_str_matches_value() -> None:
    """``JobStatus`` is a ``StrEnum``, so a database filter can use the member directly.

    Query code that wrote ``str(JobStatus.QUEUED)`` and got ``"JobStatus.QUEUED"`` would
    match nothing and return an empty result rather than an error — the quietest possible
    bug.
    """
    for status in JobStatus:
        assert str(status) == status.value
        assert f"{status}" == status.value


def test_severity_ordering_is_data_not_string_order() -> None:
    """Severity ordering must not be inferred from alphabetical column order.

    ``MEDIUM`` sorts before ``INFO`` alphabetically but is more severe, so the repository
    orders with an explicit ``CASE`` built from ``SEVERITY_ORDER``. This pins the ranking
    that expression encodes.
    """
    assert Severity.CRITICAL.rank > Severity.HIGH.rank > Severity.MEDIUM.rank
    assert Severity.MEDIUM.rank > Severity.LOW.rank > Severity.INFO.rank
    assert sorted(Severity, key=lambda severity: severity.rank) == [
        Severity.INFO,
        Severity.LOW,
        Severity.MEDIUM,
        Severity.HIGH,
        Severity.CRITICAL,
    ]

    # The concrete trap: as text, MEDIUM precedes INFO, so ``ORDER BY severity`` would
    # rank an informational note above a medium finding.
    values = [severity.value for severity in Severity]
    assert sorted(values) != sorted(values, key=lambda value: Severity(value).rank)
    assert sorted(values)[0] == "CRITICAL", "text order starts at CRITICAL"
    assert sorted(values, key=lambda value: Severity(value).rank)[-1] == "CRITICAL"
