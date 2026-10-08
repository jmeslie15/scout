"""Scout's domain layer.

This package is intentionally pure: it depends on Pydantic and PyYAML and nothing else.
No database driver, no HTTP client, no model provider SDK. That constraint is what makes
the invariants in here — the job state machine, the evidence hierarchy, the recommendation
gates — testable in milliseconds and impossible to bypass from the outside.

Layering rule: dependencies point inward. ``api``, ``orchestrator``, ``worker``, and
every other package may import ``scout_domain``; ``scout_domain`` imports none of them.
"""

from __future__ import annotations

from scout_domain.config import (
    CONFIG_PATH,
    CommandSpec,
    ScoutRepositoryConfig,
    parse_repository_config,
)
from scout_domain.enums import (
    SEVERITY_ORDER,
    ActorType,
    AgentRole,
    AgentStatus,
    ApprovalDecision,
    Capability,
    CriterionStatus,
    EvidenceKind,
    FailureClass,
    FindingCategory,
    JobStatus,
    NotificationChannel,
    Recommendation,
    RiskLevel,
    Severity,
    ToolStatus,
    VerificationStatus,
)
from scout_domain.errors import (
    ConfigurationError,
    InvalidJobTransition,
    PolicyViolation,
    RepositoryConfigurationError,
    ScoutDomainError,
    ToolContractError,
    TransitionGuardFailed,
)
from scout_domain.ids import IdKind, new_id, new_ulid, validate_ulid
from scout_domain.risk import (
    ApprovalGate,
    RiskAssessment,
    classify_change_set,
    classify_paths,
    gate_for_risk,
)
from scout_domain.state import (
    DEFAULT_RETRY_POLICY,
    VERIFICATION_PIPELINE,
    RetryPolicy,
    TransitionContext,
    allowed_transitions,
    assert_transition,
    can_transition,
    classify_exception,
    next_pipeline_stage,
    register_failure_class,
    transition,
)

__version__ = "0.1.0"

__all__ = [
    "CONFIG_PATH",
    "DEFAULT_RETRY_POLICY",
    "SEVERITY_ORDER",
    "VERIFICATION_PIPELINE",
    "ActorType",
    "AgentRole",
    "AgentStatus",
    "ApprovalDecision",
    "ApprovalGate",
    "Capability",
    "CommandSpec",
    "ConfigurationError",
    "CriterionStatus",
    "EvidenceKind",
    "FailureClass",
    "FindingCategory",
    "IdKind",
    "InvalidJobTransition",
    "JobStatus",
    "NotificationChannel",
    "PolicyViolation",
    "Recommendation",
    "RepositoryConfigurationError",
    "RetryPolicy",
    "RiskAssessment",
    "RiskLevel",
    "ScoutDomainError",
    "ScoutRepositoryConfig",
    "Severity",
    "ToolContractError",
    "ToolStatus",
    "TransitionContext",
    "TransitionGuardFailed",
    "VerificationStatus",
    "__version__",
    "allowed_transitions",
    "assert_transition",
    "can_transition",
    "classify_change_set",
    "classify_exception",
    "classify_paths",
    "gate_for_risk",
    "new_id",
    "new_ulid",
    "next_pipeline_stage",
    "parse_repository_config",
    "register_failure_class",
    "transition",
    "validate_ulid",
]
