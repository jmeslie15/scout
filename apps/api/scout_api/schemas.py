"""API response schemas.

Domain models are reused directly wherever they are already the right shape — the API
contract *is* the domain model, so duplicating it here would create two things to keep in
sync. This module only defines the composite views the dashboard needs: a job list row with
denormalised counts, a job detail with its stages and runs, and the aggregate views.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from scout_domain.enums import JobStatus, Recommendation, RiskLevel, Severity, VerificationStatus
from scout_domain.models.agent import AgentRun
from scout_domain.models.approval import Approval
from scout_domain.models.artifact import Artifact
from scout_domain.models.audit import AuditEvent
from scout_domain.models.base import ScoutModel
from scout_domain.models.finding import Finding
from scout_domain.models.job import CommitRange, JobStage
from scout_domain.models.knowledge import KnowledgeDocument
from scout_domain.models.question import Question
from scout_domain.models.report import VerificationReport
from scout_domain.models.tool import ToolResult

__all__ = [
    "AgentCatalogEntry",
    "ApprovalRequest",
    "AuditPage",
    "CancelRequest",
    "ErrorBody",
    "HealthResponse",
    "JobDetail",
    "JobSummary",
    "KnowledgeCreateRequest",
    "MetricsSummary",
    "Page",
    "QuestionAnswerRequest",
    "ReadyResponse",
    "RepositorySummaryView",
    "ToolCatalogEntry",
    "VerificationReport",
]


class ErrorBody(ScoutModel):
    """Uniform error body.

    ``code`` is machine-readable and stable; ``detail`` is for humans. A client can branch
    on ``code`` without parsing prose.
    """

    code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1)
    detail: dict[str, object] = Field(default_factory=dict)


class DependencyCheck(ScoutModel):
    ok: bool
    detail: str | None = None


class HealthResponse(ScoutModel):
    status: str = "ok"
    service: str = "scout-api"
    version: str


class ReadyResponse(ScoutModel):
    status: str
    checks: dict[str, DependencyCheck] = Field(default_factory=dict)


class Page[T](ScoutModel):
    """A page of results.

    Generic so that every list endpoint shares one envelope and the dashboard needs one
    pagination component rather than one per resource.
    """

    items: list[T]
    total: int = Field(ge=0)
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class JobSummary(ScoutModel):
    """A row in the jobs table.

    ``finding_counts`` is denormalised on purpose: rendering a hundred job rows must not
    require a hundred finding queries.
    """

    id: str
    repository_full_name: str
    pull_request_number: int
    pull_request_title: str = ""
    pull_request_url: str | None = None
    head_sha: str
    status: JobStatus
    risk_level: RiskLevel
    recommendation: Recommendation | None = None
    verification_status: VerificationStatus
    created_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None
    finding_counts: dict[Severity, int] = Field(default_factory=dict)
    open_question_count: int = 0


class JobDetail(JobSummary):
    """Everything the job page needs, in one response.

    Assembled in a single request deliberately: the detail page shows stages, findings,
    questions, tool runs, agent runs, and approvals together, and six round trips to render
    one page is the kind of decision that makes an internal tool feel slow.
    """

    base_sha: str
    pull_request_author: str = ""
    commits: CommitRange
    attempt: int = 1
    max_attempts: int = 3
    failure_reason: str | None = None
    cancel_reason: str | None = None

    stages: list[JobStage] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    tool_runs: list[ToolResult] = Field(default_factory=list)
    agent_runs: list[AgentRun] = Field(default_factory=list)
    approvals: list[Approval] = Field(default_factory=list)

    #: Whether a valid approval exists for the commit currently under review. Spec section
    #: 74: when the PR moves, the old approval is invalid and re-verification is required.
    #: The dashboard must make this impossible to miss, so it is a first-class field.
    current_approval_valid: bool = False

    report: VerificationReport | None = None


class RepositorySummaryView(ScoutModel):
    full_name: str
    github_id: int | None = None
    default_branch: str = "main"
    is_active: bool = True
    job_count: int = 0
    last_job_at: datetime | None = None


class ApprovalRequest(ScoutModel):
    """A human authorization decision (spec section 46).

    ``decided_by`` is required: an approval with no attributable human is not an approval.
    """

    decision: str = Field(pattern=r"^(APPROVE|REQUEST_CHANGES|REJECT)$")
    decided_by: str = Field(min_length=1, max_length=200)
    comment: str = Field(default="", max_length=8000)


class CancelRequest(ScoutModel):
    reason: str = Field(min_length=1, max_length=2000)


class QuestionAnswerRequest(ScoutModel):
    body: str = Field(min_length=1)
    answered_by: str = Field(min_length=1, max_length=200)
    #: When set, the answer becomes durable engineering memory (spec section 29) so Scout
    #: does not ask the same question twice.
    promote_to_knowledge: bool = False
    knowledge_scope: str | None = Field(default=None, max_length=200)


class KnowledgeCreateRequest(ScoutModel):
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1)
    source_type: str = Field(default="HUMAN_ANSWER")
    authority: str = Field(default="HUMAN_DECISION")
    scope: str = Field(default="*", max_length=200)
    tags: list[str] = Field(default_factory=list)


class ToolCatalogEntry(ScoutModel):
    name: str
    category: str
    description: str
    capability: str
    applicable: bool = True
    runs: int = 0
    failures: int = 0
    avg_duration_seconds: float | None = None


class AgentCatalogEntry(ScoutModel):
    role: str
    description: str
    capabilities: list[str] = Field(default_factory=list)
    prompt_versions: list[str] = Field(default_factory=list)
    runs: int = 0
    avg_latency_seconds: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0


class MetricsSummary(ScoutModel):
    """Aggregate metrics for the dashboard landing page (spec section 49)."""

    jobs_total: int = 0
    jobs_failed: int = 0
    jobs_by_status: dict[str, int] = Field(default_factory=dict)
    findings_by_severity: dict[Severity, int] = Field(default_factory=dict)
    human_escalations: int = 0
    llm_tokens: int = 0
    llm_cost_usd: float = 0.0
    avg_job_duration_seconds: float | None = None
    test_failures: int = 0


class AuditPage(Page[AuditEvent]):
    pass


# Re-exported so routers can name them without importing from the domain package directly.
__all__ += ["Artifact", "AuditEvent", "KnowledgeDocument", "Question", "VerificationReport"]
