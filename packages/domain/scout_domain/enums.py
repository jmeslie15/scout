"""Enumerations that form Scout's shared vocabulary.

These are deliberately string-valued (``StrEnum``) so they serialize to stable,
human-readable JSON and remain readable in the database and in logs. Agents,
tools, and the orchestrator all speak this vocabulary; no component is permitted
to invent its own status strings.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

__all__ = [
    "SEVERITY_ORDER",
    "ActorType",
    "AgentRole",
    "AgentStatus",
    "ApprovalDecision",
    "Capability",
    "CriterionStatus",
    "EvidenceKind",
    "FailureClass",
    "FindingCategory",
    "JobStatus",
    "NotificationChannel",
    "QuestionStatus",
    "Recommendation",
    "RiskLevel",
    "SandboxBackendKind",
    "ToolStatus",
    "VerificationStatus",
]


class Severity(StrEnum):
    """Severity of a finding. Ordered from least to most serious."""

    INFO = "INFO"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def rank(self) -> int:
        return SEVERITY_ORDER[self]

    def __lt__(self, other: Severity) -> bool:  # type: ignore[override]
        if not isinstance(other, Severity):
            return NotImplemented
        return self.rank < other.rank

    def __le__(self, other: Severity) -> bool:  # type: ignore[override]
        if not isinstance(other, Severity):
            return NotImplemented
        return self.rank <= other.rank

    def __gt__(self, other: Severity) -> bool:  # type: ignore[override]
        if not isinstance(other, Severity):
            return NotImplemented
        return self.rank > other.rank

    def __ge__(self, other: Severity) -> bool:  # type: ignore[override]
        if not isinstance(other, Severity):
            return NotImplemented
        return self.rank >= other.rank


#: Numeric rank for severity. Used for ordering, thresholds, and notification routing.
SEVERITY_ORDER: Final[dict[Severity, int]] = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


class RiskLevel(StrEnum):
    """Risk classification of a change (spec section 41).

    Higher risk triggers deeper verification and stricter human gates.
    """

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"

    @property
    def depth_rank(self) -> int:
        return _RISK_ORDER[self]

    def __lt__(self, other: RiskLevel) -> bool:  # type: ignore[override]
        if not isinstance(other, RiskLevel):
            return NotImplemented
        return self.depth_rank < other.depth_rank


_RISK_ORDER: Final[dict[RiskLevel, int]] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


class JobStatus(StrEnum):
    """Every verification job has exactly one explicit state (spec section 11).

    Transitions between these values are validated by
    :mod:`scout_domain.state.machine`; nothing else may assign a status directly.
    """

    RECEIVED = "RECEIVED"
    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    ANALYZING = "ANALYZING"
    REVIEWING = "REVIEWING"
    SECURITY_SCANNING = "SECURITY_SCANNING"
    TESTING = "TESTING"
    FEATURE_VERIFYING = "FEATURE_VERIFYING"
    INVESTIGATING = "INVESTIGATING"
    WAITING_FOR_HUMAN = "WAITING_FOR_HUMAN"
    READY_FOR_REVIEW = "READY_FOR_REVIEW"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    COMPLETED = "COMPLETED"

    @property
    def is_terminal(self) -> bool:
        """``True`` when no automatic transition may follow.

        ``FAILED`` is deliberately excluded: an operator may explicitly re-queue a
        failed job (spec section 51), so it is a resting state rather than a tombstone.
        """
        return self in _TERMINAL_STATUSES

    @property
    def is_active(self) -> bool:
        """``True`` while the orchestrator is expected to be doing work."""
        return self in _ACTIVE_STATUSES

    @property
    def is_success_terminal(self) -> bool:
        return self is JobStatus.COMPLETED


_TERMINAL_STATUSES: Final[frozenset[JobStatus]] = frozenset(
    {JobStatus.COMPLETED, JobStatus.CANCELLED}
)

_ACTIVE_STATUSES: Final[frozenset[JobStatus]] = frozenset(
    {
        JobStatus.RECEIVED,
        JobStatus.QUEUED,
        JobStatus.PREPARING,
        JobStatus.ANALYZING,
        JobStatus.REVIEWING,
        JobStatus.SECURITY_SCANNING,
        JobStatus.TESTING,
        JobStatus.FEATURE_VERIFYING,
        JobStatus.INVESTIGATING,
    }
)


class ActorType(StrEnum):
    """Who performed an audited action (spec section 43)."""

    SYSTEM = "SYSTEM"
    AGENT = "AGENT"
    HUMAN = "HUMAN"
    WEBHOOK = "WEBHOOK"
    WORKER = "WORKER"


class FindingCategory(StrEnum):
    """Review categories from spec section 18, plus tooling-derived categories."""

    CORRECTNESS = "CORRECTNESS"
    MAINTAINABILITY = "MAINTAINABILITY"
    API_BEHAVIOR = "API_BEHAVIOR"
    DATA = "DATA"
    CONCURRENCY = "CONCURRENCY"
    ERROR_HANDLING = "ERROR_HANDLING"
    TESTS = "TESTS"
    SECURITY = "SECURITY"
    DEPENDENCIES = "DEPENDENCIES"
    STATIC_ANALYSIS = "STATIC_ANALYSIS"
    SECRETS = "SECRETS"
    CONFIGURATION = "CONFIGURATION"
    PERFORMANCE = "PERFORMANCE"
    DOCUMENTATION = "DOCUMENTATION"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    PROCESS = "PROCESS"


class Capability(StrEnum):
    """Capabilities granted to an agent or tool (spec section 54).

    An agent receives only the capabilities required for its role. Capability
    checks happen in the orchestration layer, not inside the agent, so that a
    compromised or hallucinating agent cannot widen its own reach.
    """

    REPOSITORY_READ = "repository.read"
    REPOSITORY_SEARCH = "repository.search"
    GIT_DIFF = "git.diff"
    TEST_RUN = "test.run"
    TEST_GENERATE = "test.generate"
    LINT_RUN = "lint.run"
    TYPECHECK_RUN = "typecheck.run"
    SECURITY_SCAN = "security.scan"
    DEPENDENCY_SCAN = "dependency.scan"
    KNOWLEDGE_SEARCH = "knowledge.search"
    GITHUB_READ = "github.read"
    GITHUB_WRITE_CHECK = "github.write.check"
    NOTIFICATION_CREATE = "notification.create"


class AgentRole(StrEnum):
    """The fixed set of reasoning agents (spec section 40).

    Agents do not spawn other agents; the orchestrator schedules these roles
    explicitly.
    """

    REPOSITORY_ANALYZER = "repository_analyzer"
    CODE_REVIEWER = "code_reviewer"
    SECURITY = "security"
    TEST_ANALYSIS = "test_analysis"
    FEATURE_VERIFICATION = "feature_verification"
    INVESTIGATION = "investigation"
    REPORT_GENERATOR = "report_generator"


class AgentStatus(StrEnum):
    """Agent result status (spec section 32)."""

    COMPLETED = "completed"
    NEEDS_INVESTIGATION = "needs_investigation"
    NEEDS_HUMAN = "needs_human"
    FAILED = "failed"


class ToolStatus(StrEnum):
    """Outcome of a single deterministic tool execution (spec section 21)."""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    TIMEOUT = "timeout"
    SKIPPED = "skipped"
    NOT_APPLICABLE = "not_applicable"

    @property
    def is_failure(self) -> bool:
        return self in {ToolStatus.FAILED, ToolStatus.ERROR, ToolStatus.TIMEOUT}


class FailureClass(StrEnum):
    """Retry classification (spec section 51).

    Only ``RETRYABLE`` failures are retried, with exponential backoff.
    """

    RETRYABLE = "retryable"
    NON_RETRYABLE = "non_retryable"
    UNKNOWN = "unknown"


class EvidenceKind(StrEnum):
    """Every piece of evidence identifies its source (spec section 34)."""

    TOOL_RUN = "tool_run"
    TEST_RUN = "test_run"
    FILE_RANGE = "file_range"
    GIT_DIFF = "git_diff"
    REPOSITORY_INSPECTION = "repository_inspection"
    GITHUB_CHECK = "github_check"
    KNOWLEDGE_DOCUMENT = "knowledge_document"
    HUMAN_ANSWER = "human_answer"
    AGENT_RUN = "agent_run"
    ARTIFACT = "artifact"


class QuestionStatus(StrEnum):
    """Lifecycle of a first-class question (spec section 27)."""

    OPEN = "OPEN"
    ANSWERED = "ANSWERED"
    DISMISSED = "DISMISSED"
    SUPERSEDED = "SUPERSEDED"


class CriterionStatus(StrEnum):
    """Result of verifying one acceptance criterion (spec section 25)."""

    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    NOT_VERIFIED = "not_verified"


class VerificationStatus(StrEnum):
    """Overall verification outcome for a job."""

    PENDING = "PENDING"
    PASSED = "PASSED"
    PASSED_WITH_WARNINGS = "PASSED_WITH_WARNINGS"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"


class Recommendation(StrEnum):
    """Final recommendation in the verification report (spec section 44)."""

    PASS = "PASS"
    PASS_WITH_WARNINGS = "PASS_WITH_WARNINGS"
    REQUIRES_HUMAN_REVIEW = "REQUIRES_HUMAN_REVIEW"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    FAIL = "FAIL"


class ApprovalDecision(StrEnum):
    """Human authorization decision (spec section 46).

    Scout can never produce one of these on its own behalf.
    """

    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    REJECT = "REJECT"


class NotificationChannel(StrEnum):
    """Delivery channels (spec section 47)."""

    GOOGLE_CHAT = "GOOGLE_CHAT"
    EMAIL = "EMAIL"
    GITHUB = "GITHUB"


class SandboxBackendKind(StrEnum):
    """Available isolation backends."""

    LOCAL = "local"
    DOCKER = "docker"
