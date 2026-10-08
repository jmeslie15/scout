"""The final verification report (spec section 44).

The report is the product. Everything else in Scout exists to produce this object
honestly: every section either carries machine verification or is explicitly marked as
unverified, every question is surfaced rather than smoothed over, and the recommendation
is *constrained by* the evidence rather than chosen freely.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from scout_domain.enums import (
    CriterionStatus,
    FindingCategory,
    Recommendation,
    RiskLevel,
    Severity,
    VerificationStatus,
)
from scout_domain.models.base import ScoutModel, utcnow
from scout_domain.models.evidence import EvidenceReference, EvidenceStrength
from scout_domain.models.finding import Finding
from scout_domain.models.question import Question
from scout_domain.models.repository import ChangedFile
from scout_domain.models.tool import ToolResult

__all__ = [
    "CriterionVerification",
    "ReportSection",
    "ReportSectionStatus",
    "VerificationReport",
    "VerificationReportDraft",
]


class ReportSectionStatus(StrEnum):
    """Visual/semantic status of a report section."""

    PASSED = "PASSED"
    WARNING = "WARNING"
    FAILED = "FAILED"
    INFO = "INFO"
    SKIPPED = "SKIPPED"
    UNVERIFIED = "UNVERIFIED"


class ReportSection(ScoutModel):
    """One section of the report.

    ``status=UNVERIFIED`` exists specifically so a section that could not be checked
    never renders as a pass. Silence is not success.
    """

    key: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    status: ReportSectionStatus = ReportSectionStatus.INFO
    summary: str = Field(default="", max_length=8000)
    details: list[str] = Field(default_factory=list)
    evidence: list[EvidenceReference] = Field(default_factory=list)
    #: Set when the section could not be produced, explaining why.
    unverified_reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _validate_unverified(self) -> ReportSection:
        if self.status is ReportSectionStatus.UNVERIFIED and not self.unverified_reason:
            raise ValueError("an UNVERIFIED section must explain why")
        if self.status is ReportSectionStatus.SKIPPED and not self.unverified_reason:
            raise ValueError("a SKIPPED section must explain why")
        return self

    @property
    def machine_verified(self) -> bool:
        return any(reference.strength is EvidenceStrength.MACHINE for reference in self.evidence)


class CriterionVerification(ScoutModel):
    """Verification of one acceptance criterion (spec section 25).

    Spec section 23 draws the distinction this object exists to preserve: "the tests
    passed" is not the same claim as "the requested feature works". A criterion is only
    ``PASSED`` when a concrete procedure was executed and its observed result matched
    the expectation.
    """

    criterion: str = Field(min_length=1, max_length=2000)
    status: CriterionStatus
    #: How the criterion was checked, in steps a human can repeat by hand.
    procedure: list[str] = Field(default_factory=list)
    expected_result: str = Field(default="", max_length=4000)
    actual_result: str = Field(default="", max_length=4000)
    evidence: list[EvidenceReference] = Field(default_factory=list)
    notes: str = Field(default="", max_length=4000)
    #: Where the criterion came from: PR description, linked issue, existing test, ...
    source: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _validate_verification(self) -> CriterionVerification:
        if self.status is CriterionStatus.PASSED:
            if not self.evidence:
                raise ValueError(f"criterion {self.criterion!r} cannot pass without evidence")
            if not self.actual_result:
                raise ValueError(
                    f"criterion {self.criterion!r} cannot pass without an observed result"
                )
        if self.status is CriterionStatus.FAILED and not self.actual_result:
            raise ValueError(f"criterion {self.criterion!r} failed but recorded no result")
        return self


class TestOutcomeSummary(ScoutModel):
    """Aggregate test execution outcome.

    ``__test__ = False`` tells pytest this is a domain model and not a test class; without
    it, pytest tries to collect it because of the ``Test`` prefix.
    """

    __test__ = False

    total: int = Field(default=0, ge=0)
    passed: int = Field(default=0, ge=0)
    failed: int = Field(default=0, ge=0)
    skipped: int = Field(default=0, ge=0)
    errors: int = Field(default=0, ge=0)
    duration_seconds: float = Field(default=0.0, ge=0.0)
    #: Whether the changed production behaviour is exercised by at least one test.
    #:
    #: ``True`` means a test runs the changed code. It does **not** mean the change is correct:
    #: a test that fails because of the change still exercises it, and the signal for that is
    #: ``failed > 0``, a ``TESTS`` finding, and a recommendation of ``FAIL`` or
    #: ``REQUEST_CHANGES``. Conflating "covered" with "correct" would make it impossible to
    #: distinguish an untested change from a change whose tests caught a regression.
    #:
    #: ``False`` means a changed production path that no test touches — the case
    #: ``verification.require_tests_for_code_changes`` turns into a finding.
    #:
    #: ``None`` means Scout could not determine coverage, which is the honest answer when no
    #: test ran and no coverage report exists. It is deliberately not ``False``: an unset field
    #: cannot be mistaken for an assertion.
    change_is_covered: bool | None = None
    coverage_percent: float | None = Field(default=None, ge=0.0, le=100.0)
    #: Files changed by the PR that no test touches. This is the "tests passed" vs
    #: "tests sufficiently cover the change" distinction (spec section 67).
    uncovered_changed_paths: list[str] = Field(default_factory=list)


class DependencySummary(ScoutModel):
    """Dependency and vulnerability scan outcome."""

    ecosystems: list[str] = Field(default_factory=list)
    vulnerable_packages: int = Field(default=0, ge=0)
    outdated_packages: int = Field(default=0, ge=0)
    highest_severity: Severity | None = None
    tool_results: list[ToolResult] = Field(default_factory=list)


class VerificationReportDraft(ScoutModel):
    """The aggregation input.

    The orchestrator collects a draft from every stage, then the report generator
    renders a :class:`VerificationReport`. Keeping the draft separate from the final
    report means the gating rules below cannot be bypassed by a stage writing directly
    into the final object.
    """

    job_id: str = Field(min_length=1)
    repository_full_name: str = Field(min_length=3, max_length=200)
    pull_request_number: int = Field(ge=1)
    commit_sha: str = Field(min_length=7, max_length=64)

    risk_level: RiskLevel = RiskLevel.MEDIUM
    findings: list[Finding] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    criteria: list[CriterionVerification] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)
    sections: list[ReportSection] = Field(default_factory=list)
    known_limitations: list[str] = Field(default_factory=list)

    @property
    def open_questions(self) -> list[Question]:
        return [question for question in self.questions if question.is_open]

    @property
    def findings_by_severity(self) -> dict[Severity, list[Finding]]:
        grouped: dict[Severity, list[Finding]] = {severity: [] for severity in Severity}
        for finding in self.findings:
            grouped[finding.severity].append(finding)
        return grouped

    def findings_in(self, *categories: FindingCategory) -> list[Finding]:
        wanted = set(categories)
        return [finding for finding in self.findings if finding.category in wanted]


class VerificationReport(ScoutModel):
    """The structured report for one job (spec section 44).

    Required sections from the spec are present as typed fields rather than as free
    prose, so the dashboard and the GitHub check render the same facts without
    re-parsing a narrative.
    """

    job_id: str = Field(min_length=1)
    repository_full_name: str = Field(min_length=3, max_length=200)
    pull_request_number: int = Field(ge=1)
    commit_sha: str = Field(min_length=7, max_length=64)
    base_sha: str = Field(min_length=7, max_length=64)

    # -- Required sections -----------------------------------------------------------
    executive_summary: str = Field(min_length=1, max_length=8000)
    changed_components: list[ChangedFile] = Field(default_factory=list)
    architecture_impact: str = Field(default="", max_length=8000)
    code_review: list[Finding] = Field(default_factory=list)
    security: list[Finding] = Field(default_factory=list)
    dependencies: DependencySummary = Field(default_factory=DependencySummary)
    static_analysis: list[Finding] = Field(default_factory=list)
    tests: TestOutcomeSummary = Field(default_factory=TestOutcomeSummary)
    feature_verification: list[CriterionVerification] = Field(default_factory=list)
    open_questions: list[Question] = Field(default_factory=list)
    known_limitations: list[str] = Field(default_factory=list)
    risk_level: RiskLevel = RiskLevel.MEDIUM
    recommendation: Recommendation
    evidence: list[EvidenceReference] = Field(default_factory=list)

    # -- Supporting detail -----------------------------------------------------------
    verification_status: VerificationStatus = VerificationStatus.PENDING
    sections: list[ReportSection] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)
    #: Findings that are neither code review, security, nor static analysis (e.g.
    #: configuration, tests, documentation).
    other_findings: list[Finding] = Field(default_factory=list)

    #: Explicit list of what Scout could not determine. Populated from sections marked
    #: UNVERIFIED so the report never overstates its own completeness.
    unverified_areas: list[str] = Field(default_factory=list)

    generated_at: datetime = Field(default_factory=utcnow)
    #: Prompt versions and model ids used, for reproducibility (spec section 39).
    agent_provenance: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _enforce_recommendation_gates(self) -> VerificationReport:
        """Constrain the recommendation by the evidence.

        Spec section 2: "Scout should never silently transition from analysis to
        deployment authority." These gates are where that sentence becomes enforced
        behaviour instead of a comment.
        """
        all_findings = [
            *self.code_review,
            *self.security,
            *self.static_analysis,
            *self.other_findings,
        ]
        critical = [f for f in all_findings if f.severity is Severity.CRITICAL]
        high = [f for f in all_findings if f.severity is Severity.HIGH]
        failed_criteria = [
            c for c in self.feature_verification if c.status is CriterionStatus.FAILED
        ]
        blocked_criteria = [
            c for c in self.feature_verification if c.status is CriterionStatus.BLOCKED
        ]

        if self.open_questions and self.recommendation in {
            Recommendation.PASS,
            Recommendation.PASS_WITH_WARNINGS,
        }:
            raise ValueError(
                f"cannot recommend {self.recommendation.value} with "
                f"{len(self.open_questions)} open question(s); use REQUIRES_HUMAN_REVIEW "
                "or a stricter recommendation"
            )

        if critical and self.recommendation in {
            Recommendation.PASS,
            Recommendation.PASS_WITH_WARNINGS,
        }:
            raise ValueError(
                f"cannot recommend {self.recommendation.value} with "
                f"{len(critical)} CRITICAL finding(s)"
            )

        if failed_criteria and self.recommendation is Recommendation.PASS:
            raise ValueError(
                f"cannot recommend PASS with {len(failed_criteria)} failed acceptance "
                "criterion/criteria"
            )

        if self.tests.failed and self.recommendation in {
            Recommendation.PASS,
            Recommendation.PASS_WITH_WARNINGS,
        }:
            raise ValueError(
                f"cannot recommend {self.recommendation.value} with "
                f"{self.tests.failed} failing test(s)"
            )

        if blocked_criteria and not self.open_questions:
            raise ValueError(
                "a blocked acceptance criterion requires an explicit open question "
                "explaining what is missing"
            )

        if high and self.recommendation is Recommendation.PASS:
            raise ValueError(
                f"cannot recommend PASS with {len(high)} HIGH severity finding(s); "
                "use PASS_WITH_WARNINGS or stricter"
            )

        return self

    # -- Derived views used by the dashboard and the GitHub check --------------------

    @property
    def all_findings(self) -> list[Finding]:
        return [
            *self.code_review,
            *self.security,
            *self.static_analysis,
            *self.other_findings,
        ]

    @property
    def blocking_findings(self) -> list[Finding]:
        """Findings severe enough to block promotion to the next stage."""
        return [
            finding
            for finding in self.all_findings
            if finding.severity in {Severity.CRITICAL, Severity.HIGH}
        ]

    @property
    def requires_human(self) -> bool:
        return bool(self.open_questions) or any(
            finding.requires_human for finding in self.all_findings
        )

    def findings_by_severity(self) -> dict[Severity, int]:
        counts = dict.fromkeys(Severity, 0)
        for finding in self.all_findings:
            counts[finding.severity] += 1
        return counts

    def evidence_backed_findings(self) -> list[Finding]:
        """Findings whose evidence reaches at least INSPECTION strength."""
        return [
            finding
            for finding in self.all_findings
            if finding.evidence
            and max(reference.strength.rank for reference in finding.evidence) >= 2
        ]

    def check_run_summary_lines(self) -> list[str]:
        """Render the concise check-run lines shown in the GitHub UI (spec section 45)."""
        lines: list[str] = []
        if self.tests.total:
            mark = "x" if self.tests.failed else "v"
            lines.append(
                f"{mark} Tests: {self.tests.passed} passed, {self.tests.failed} failed, "
                f"{self.tests.skipped} skipped"
            )
        else:
            lines.append("- Tests: not run")
        security_findings = [f for f in self.security if f.severity >= Severity.MEDIUM]
        lines.append(
            "v Security checks passed"
            if not security_findings
            else f"x Security: {len(security_findings)} finding(s) at MEDIUM or above"
        )
        if self.dependencies.tool_results:
            vulnerable = self.dependencies.vulnerable_packages
            mark = "v" if not vulnerable else "x"
            lines.append(f"{mark} Dependency scan: {vulnerable} vulnerable package(s)")
        if self.feature_verification:
            failed = sum(
                1 for c in self.feature_verification if c.status is not CriterionStatus.PASSED
            )
            lines.append(
                f"v Feature verification: {len(self.feature_verification)} criterion/criteria met"
                if not failed
                else f"x Feature verification: {failed} criterion/criteria not met"
            )
        if self.open_questions:
            lines.append(f"! {len(self.open_questions)} engineering question(s) require review")
        if self.unverified_areas:
            lines.append(f"! {len(self.unverified_areas)} area(s) could not be verified")
        return lines
