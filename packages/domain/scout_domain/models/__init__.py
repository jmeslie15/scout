"""Domain model package: the shared vocabulary of jobs, findings, and evidence."""

from __future__ import annotations

from scout_domain.models.agent import AgentContext, AgentResult, AgentRun
from scout_domain.models.approval import Approval
from scout_domain.models.artifact import DEFAULT_RETENTION, Artifact, ArtifactKind
from scout_domain.models.audit import AuditEvent
from scout_domain.models.base import ScoutModel, TimestampedModel, utcnow
from scout_domain.models.evidence import (
    CONFIDENCE_CEILING,
    EVIDENCE_STRENGTH,
    MAX_EXCERPT_CHARS,
    UNSUPPORTED_CONFIDENCE_CEILING,
    EvidenceReference,
    EvidenceStrength,
    evidence_strength_of,
    strongest_evidence,
)
from scout_domain.models.finding import Finding, FindingOrigin, FindingProvenance
from scout_domain.models.job import (
    CommitRange,
    JobStage,
    JobStageOutcome,
    VerificationJob,
)
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
from scout_domain.models.report import (
    CriterionVerification,
    DependencySummary,
    ReportSection,
    ReportSectionStatus,
    TestOutcomeSummary,
    VerificationReport,
    VerificationReportDraft,
)
from scout_domain.models.repository import (
    ChangedFile,
    ChangeGraph,
    ChangeGraphEdge,
    ChangeGraphNode,
    ChangeSet,
    ChangeType,
    DetectedService,
    RepositoryRef,
    RepositorySummary,
    TechnologyStack,
)
from scout_domain.models.sandbox import (
    DEFAULT_RESOURCE_LIMITS,
    NetworkMode,
    ResourceLimits,
    SandboxPolicy,
    SandboxRun,
    SandboxStatus,
)
from scout_domain.models.tool import ToolInvocation, ToolResult, ToolSummary

__all__ = [
    "AUTHORITY_RANK",
    "CONFIDENCE_CEILING",
    "DEFAULT_RESOURCE_LIMITS",
    "DEFAULT_RETENTION",
    "EVIDENCE_STRENGTH",
    "MAX_EXCERPT_CHARS",
    "UNSUPPORTED_CONFIDENCE_CEILING",
    "AgentContext",
    "AgentResult",
    "AgentRun",
    "Approval",
    "Artifact",
    "ArtifactKind",
    "AuditEvent",
    "ChangeGraph",
    "ChangeGraphEdge",
    "ChangeGraphNode",
    "ChangeSet",
    "ChangeType",
    "ChangedFile",
    "CommitRange",
    "CriterionVerification",
    "DependencySummary",
    "DetectedService",
    "EngineeringDecision",
    "EvidenceReference",
    "EvidenceStrength",
    "Finding",
    "FindingOrigin",
    "FindingProvenance",
    "JobStage",
    "JobStageOutcome",
    "KnowledgeAuthority",
    "KnowledgeDocument",
    "KnowledgeSourceType",
    "ModelRequestRecord",
    "ModelRequestStatus",
    "NetworkMode",
    "NotificationRecord",
    "NotificationStatus",
    "Question",
    "QuestionAnswer",
    "ReportSection",
    "ReportSectionStatus",
    "RepositoryRef",
    "RepositorySummary",
    "ResourceLimits",
    "SandboxPolicy",
    "SandboxRun",
    "SandboxStatus",
    "ScoutModel",
    "TechnologyStack",
    "TestOutcomeSummary",
    "TimestampedModel",
    "ToolInvocation",
    "ToolResult",
    "ToolSummary",
    "VerificationJob",
    "VerificationReport",
    "VerificationReportDraft",
    "evidence_strength_of",
    "strongest_evidence",
    "utcnow",
]
