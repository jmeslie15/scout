"""Agent context and result contracts (spec sections 31 and 32).

Free-form text is not an acceptable agent interface. Every agent receives an explicit
:class:`AgentContext` and returns an explicit :class:`AgentResult`. Both are validated
Pydantic models, so a malformed model response fails a contract test rather than
silently degrading a report.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, model_validator

from scout_domain.enums import AgentRole, AgentStatus, Capability, RiskLevel
from scout_domain.models.base import ScoutModel, utcnow
from scout_domain.models.evidence import EvidenceReference
from scout_domain.models.finding import Finding
from scout_domain.models.knowledge import KnowledgeDocument
from scout_domain.models.question import Question
from scout_domain.models.repository import (
    ChangeSet,
    RepositorySummary,
)
from scout_domain.models.tool import ToolResult

__all__ = ["AgentContext", "AgentResult", "AgentRun"]


class AgentContext(ScoutModel):
    """Exactly what an agent is allowed to see (spec section 31).

    Note what is *absent*: the repository is not passed wholesale. Spec section 17
    requires selective retrieval, so the agent receives a bounded summary, the change
    set, deterministic tool output, and selectively retrieved knowledge. There are also
    no credentials here, by construction (spec section 55).
    """

    job_id: str = Field(min_length=1)
    repository_id: str = Field(min_length=1)
    repository_full_name: str = Field(min_length=3, max_length=200)
    commit_sha: str = Field(min_length=7, max_length=64)

    role: AgentRole
    risk_level: RiskLevel = RiskLevel.MEDIUM

    change_set: ChangeSet
    repository_summary: RepositorySummary

    #: Artifact holding the diff. The diff itself is not inlined (spec section 21).
    diff_artifact_id: str | None = None
    #: Artifact holding the PR description / linked issue text.
    requirement_artifact_id: str | None = None
    requirement_text: str = Field(default="", max_length=20_000)

    tool_results: list[ToolResult] = Field(default_factory=list)
    relevant_knowledge: list[KnowledgeDocument] = Field(default_factory=list)

    #: File contents the orchestrator selectively retrieved for this agent, keyed by
    #: repository-relative path. Bounded by ``max_context_files``.
    retrieved_files: dict[str, str] = Field(default_factory=dict)
    #: Extra retrieval budget label recorded for cost/complexity analysis.
    retrieval_reason: str = Field(default="", max_length=1000)

    capabilities: frozenset[Capability] = Field(default_factory=frozenset)

    @model_validator(mode="after")
    def _validate_context(self) -> AgentContext:
        if self.change_set.head_sha != self.commit_sha:
            raise ValueError(
                "AgentContext.commit_sha must equal change_set.head_sha; an agent must "
                "never analyse a different revision than the job pinned"
            )
        if self.repository_summary.repository.full_name != self.repository_full_name:
            raise ValueError("repository_summary does not match repository_full_name")
        return self

    def requires(self, capability: Capability) -> None:
        """Raise if this context was not granted ``capability``.

        Capability checks live outside the agent so that a hallucinating or compromised
        agent cannot widen its own reach (spec section 54).
        """
        from scout_domain.errors import PolicyViolation

        if capability not in self.capabilities:
            raise PolicyViolation(
                f"agent role {self.role.value!r} was not granted capability {capability.value!r}",
                role=self.role.value,
                capability=capability.value,
            )


class AgentRun(ScoutModel):
    """A persisted record of one agent invocation, for reproducibility and cost."""

    id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    role: AgentRole

    status: AgentStatus = AgentStatus.COMPLETED
    #: Version of the prompt template used. Spec section 39: historical results must be
    #: reproducible, which requires naming the exact prompt version.
    prompt_version: str = Field(min_length=1, max_length=100)

    provider: str | None = Field(default=None, max_length=100)
    model: str | None = Field(default=None, max_length=200)

    started_at: datetime = Field(default_factory=utcnow)
    completed_at: datetime | None = None
    latency_seconds: float | None = Field(default=None, ge=0.0)

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: float = Field(default=0.0, ge=0.0)

    #: SHA-256 of the serialized context, so two runs can be proven to have seen the
    #: same input. The context itself is not stored (it may be large).
    context_digest: str | None = Field(default=None, max_length=128)
    result_artifact_id: str | None = Field(default=None)
    error: str | None = Field(default=None, max_length=8000)
    created_at: datetime = Field(default_factory=utcnow)


class AgentResult(ScoutModel):
    """Structured agent output (spec section 32).

    ``payload`` carries role-specific structured output (for example a
    :class:`~scout_domain.models.repository.RepositorySummary` from the analyzer). It
    exists so that agents are never forced to smuggle structure through prose; the
    ``summary`` field is for the human reader, not for the orchestrator.
    """

    role: AgentRole
    status: AgentStatus

    findings: list[Finding] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    evidence: list[EvidenceReference] = Field(default_factory=list)
    summary: str = Field(min_length=1, max_length=8000)

    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_status_consistency(self) -> AgentResult:
        if self.status is AgentStatus.NEEDS_HUMAN and not self.questions:
            raise ValueError(
                "status=needs_human requires at least one question; Scout must state "
                "what it does not know rather than merely asserting uncertainty"
            )
        if self.status is AgentStatus.FAILED and self.findings:
            raise ValueError("a failed agent run must not report findings")
        if self.status is AgentStatus.COMPLETED and self.questions:
            # Asking a question is not a completed analysis.
            raise ValueError(
                "status=completed is inconsistent with open questions; use needs_human "
                "or needs_investigation"
            )
        return self

    @property
    def has_findings(self) -> bool:
        return bool(self.findings)

    def highest_severity_findings(self, *, limit: int = 10) -> list[Finding]:
        """Return the most severe findings, newest first within a severity band."""
        return sorted(
            self.findings,
            key=lambda finding: (finding.severity.rank, finding.created_at),
            reverse=True,
        )[:limit]
