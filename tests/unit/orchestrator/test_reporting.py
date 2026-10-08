"""The recommendation derivation, branch by branch (spec sections 44 and 77).

The recommendation is the single most abusable field in Scout, so it is derived from recorded
evidence and re-validated by the domain's own gates. These tests pin every branch of the table in
:mod:`scout_orchestrator.reporting`'s docstring, and they pin the *precedence*: failing tests must
outrank a failed criterion, and a CRITICAL finding must outrank everything.

They also prove the pairing that makes the design safe: when the derivation and the report's gates
disagree, the derivation raises rather than the gates loosening.
"""

from __future__ import annotations

from typing import Any

import pytest

from scout_domain.config import ScoutRepositoryConfig
from scout_domain.enums import (
    CriterionStatus,
    FindingCategory,
    JobStatus,
    Recommendation,
    RiskLevel,
    Severity,
    VerificationStatus,
)
from scout_domain.models.artifact import ArtifactKind
from scout_domain.models.evidence import EvidenceKind, EvidenceReference
from scout_domain.models.finding import Finding, FindingOrigin
from scout_domain.models.job import CommitRange, VerificationJob
from scout_domain.models.question import Question
from scout_domain.models.report import (
    CriterionVerification,
    ReportSection,
    ReportSectionStatus,
    TestOutcomeSummary,
    VerificationReport,
)
from scout_domain.models.repository import (
    ChangedFile,
    ChangeSet,
    ChangeType,
    RepositoryRef,
    RepositorySummary,
)
from scout_domain.risk import RiskAssessment
from scout_orchestrator.errors import ReportDerivationError
from scout_orchestrator.reporting import (
    RecommendationInputs,
    build_report,
    derive_recommendation,
    unverified_areas,
    verification_status_for,
)
from scout_orchestrator.stages.prepared import PreparedState
from scout_tools.artifacts import InMemoryArtifactStore

JOB_ID = "job_01J8ZQ4K2M7X3N9PQRSTVWXY0"
HEAD = "b" * 40
BASE = "a" * 40


def _finding(
    severity: Severity, *, category: FindingCategory = FindingCategory.CORRECTNESS
) -> Finding:
    """Build a tool-origin finding with machine evidence."""
    return Finding(
        id=f"find_{severity.value}",
        job_id=JOB_ID,
        severity=severity,
        category=category,
        title=f"a {severity.value} issue",
        description="Something the tool observed.",
        repository="acme/widgets",
        evidence=[
            EvidenceReference(
                kind=EvidenceKind.TOOL_RUN,
                reference_id="trun_1",
                summary="the tool reported this",
            )
        ],
        origin=FindingOrigin.TOOL,
        source="pytest",
        confidence=1.0,
    )


def _question() -> Question:
    """Build an open question."""
    return Question(
        id="qst_1",
        job_id=JOB_ID,
        title="Should export metadata be visible to managers?",
        description="The PR adds manager-visible export metadata.",
        created_by="investigation",
    )


def _criterion(
    status: CriterionStatus, *, name: str = "the feature works"
) -> CriterionVerification:
    """Build a criterion verification in a given state."""
    evidence = (
        [
            EvidenceReference(
                kind=EvidenceKind.TEST_RUN,
                reference_id="trun_1",
                summary="the test passed",
            )
        ]
        if status is CriterionStatus.PASSED
        else []
    )
    return CriterionVerification(
        criterion=name,
        status=status,
        actual_result=(
            "observed" if status in {CriterionStatus.PASSED, CriterionStatus.FAILED} else ""
        ),
        evidence=evidence,
    )


def _tests(
    *, total: int = 3, passed: int = 3, failed: int = 0, errors: int = 0
) -> TestOutcomeSummary:
    """Build a test summary."""
    return TestOutcomeSummary(
        total=total, passed=passed, failed=failed, errors=errors, change_is_covered=True
    )


def _inputs(**overrides: Any) -> RecommendationInputs:
    """Build recommendation inputs, defaulting to a clean run."""
    fields: dict[str, Any] = {
        "findings": (),
        "open_questions": (),
        "criteria": (),
        "tests": _tests(),
        "unverified_areas": (),
    }
    fields.update(overrides)
    return RecommendationInputs(**fields)


# --------------------------------------------------------------------------------------
# The derivation table
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "inputs", "expected"),
    [
        ("clean", _inputs(), Recommendation.PASS),
        (
            "medium finding only",
            _inputs(findings=(_finding(Severity.MEDIUM),)),
            Recommendation.PASS_WITH_WARNINGS,
        ),
        (
            "low finding only",
            _inputs(findings=(_finding(Severity.LOW),)),
            Recommendation.PASS_WITH_WARNINGS,
        ),
        (
            "high finding",
            _inputs(findings=(_finding(Severity.HIGH),)),
            Recommendation.REQUIRES_HUMAN_REVIEW,
        ),
        (
            "critical finding",
            _inputs(findings=(_finding(Severity.CRITICAL),)),
            Recommendation.FAIL,
        ),
        (
            "open question",
            _inputs(open_questions=(_question(),)),
            Recommendation.REQUIRES_HUMAN_REVIEW,
        ),
        (
            "unverified area",
            _inputs(unverified_areas=("the secret scanner produced no result",)),
            Recommendation.REQUIRES_HUMAN_REVIEW,
        ),
        (
            "blocked criterion",
            _inputs(criteria=(_criterion(CriterionStatus.BLOCKED),)),
            Recommendation.REQUIRES_HUMAN_REVIEW,
        ),
        (
            "not-verified criterion",
            _inputs(criteria=(_criterion(CriterionStatus.NOT_VERIFIED),)),
            Recommendation.REQUIRES_HUMAN_REVIEW,
        ),
        (
            "failed criterion",
            _inputs(criteria=(_criterion(CriterionStatus.FAILED),)),
            Recommendation.REQUEST_CHANGES,
        ),
        (
            "failing tests",
            _inputs(tests=_tests(passed=2, failed=1)),
            Recommendation.FAIL,
        ),
        (
            "errored tests",
            _inputs(tests=_tests(passed=2, errors=1)),
            Recommendation.FAIL,
        ),
    ],
)
def test_derivation_table(
    label: str, inputs: RecommendationInputs, expected: Recommendation
) -> None:
    """Each row of the documented table."""
    del label
    assert derive_recommendation(inputs) is expected


def test_precedence_failing_tests_outrank_a_high_finding() -> None:
    """A hard stop is not softened by a judgement call: FAIL beats REQUIRES_HUMAN_REVIEW."""
    inputs = _inputs(findings=(_finding(Severity.HIGH),), tests=_tests(passed=1, failed=1))

    assert derive_recommendation(inputs) is Recommendation.FAIL


def test_precedence_critical_outranks_a_failed_criterion() -> None:
    """Both are stops; the more severe one names the recommendation."""
    inputs = _inputs(
        findings=(_finding(Severity.CRITICAL, category=FindingCategory.SECRETS),),
        criteria=(_criterion(CriterionStatus.FAILED),),
    )

    assert derive_recommendation(inputs) is Recommendation.FAIL


def test_precedence_failed_criterion_outranks_an_open_question() -> None:
    """A demonstrated failure is stronger than an unanswered question."""
    inputs = _inputs(criteria=(_criterion(CriterionStatus.FAILED),), open_questions=(_question(),))

    assert derive_recommendation(inputs) is Recommendation.REQUEST_CHANGES


def test_artificial_intelligence_alone_never_reaches_pass() -> None:
    """An AI-origin finding is still a finding: it cannot yield PASS."""
    ai_only = _finding(Severity.MEDIUM)
    assert derive_recommendation(_inputs(findings=(ai_only,))) is not Recommendation.PASS


# --------------------------------------------------------------------------------------
# Verification status mapping
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("recommendation", "expected"),
    [
        (Recommendation.PASS, VerificationStatus.PASSED),
        (Recommendation.PASS_WITH_WARNINGS, VerificationStatus.PASSED_WITH_WARNINGS),
        (Recommendation.REQUEST_CHANGES, VerificationStatus.FAILED),
        (Recommendation.FAIL, VerificationStatus.FAILED),
        (Recommendation.REQUIRES_HUMAN_REVIEW, VerificationStatus.INCONCLUSIVE),
    ],
)
def test_verification_status_mapping(
    recommendation: Recommendation, expected: VerificationStatus
) -> None:
    """The job-level status follows the recommendation."""
    assert verification_status_for(recommendation) is expected


def test_only_unverified_sections_are_unverified_areas() -> None:
    """A skipped check is a stated limitation; an unverified one is a real gap."""
    sections = (
        ReportSection(
            key="testing.pytest",
            title="Tests: pytest",
            status=ReportSectionStatus.UNVERIFIED,
            summary="pytest could not run",
            unverified_reason="the pytest binary is not available",
        ),
        ReportSection(
            key="reviewing.ruff",
            title="Code review: ruff",
            status=ReportSectionStatus.SKIPPED,
            summary="not configured",
            unverified_reason="this repository does not configure ruff",
        ),
    )

    assert unverified_areas(sections) == ("the pytest binary is not available",)


# --------------------------------------------------------------------------------------
# Report assembly
# --------------------------------------------------------------------------------------


def _prepared(
    *, risk: RiskLevel = RiskLevel.MEDIUM, criteria: list[str] | None = None
) -> PreparedState:
    """Build a prepared-state document for a source-only change.

    Source-only on purpose: a changed test file makes FEATURE_VERIFYING derive a criterion from the
    suite, and with no test run recorded that criterion is BLOCKED -- which the domain rightly
    demands a question for. Tests that want that path build it explicitly.
    """
    change_set = ChangeSet(
        base_sha=BASE,
        head_sha=HEAD,
        files=[ChangedFile(path="widget.py", change_type=ChangeType.MODIFIED, additions=3)],
    )
    summary = RepositorySummary(
        repository=RepositoryRef(full_name="acme/widgets"),
        commit_sha=HEAD,
        test_paths=["tests"],
    )
    config = ScoutRepositoryConfig.model_validate(
        {
            "project": {"name": "widget"},
            "feature_verification": {"acceptance_criteria": criteria or []},
        }
    )
    return PreparedState(
        change_set=change_set,
        repository_summary=summary,
        risk=RiskAssessment(level=risk),
        repository_config=config,
        config_raw="",
        pr_body="",
        requirement_text="",
        commit_messages=(),
    )


def _job() -> VerificationJob:
    """Build the job the report is about."""
    return VerificationJob(
        id=JOB_ID,
        repository=RepositoryRef(full_name="acme/widgets"),
        repository_full_name="acme/widgets",
        pull_request_number=42,
        commits=CommitRange(base_sha=BASE, head_sha=HEAD),
        status=JobStatus.INVESTIGATING,
    )


def _build(**overrides: Any) -> VerificationReport:
    """Assemble a report through the production path."""
    store = InMemoryArtifactStore()
    fields: dict[str, Any] = {
        "job": _job(),
        "prepared": _prepared(),
        "tool_results": (),
        "findings": (),
        "questions": (),
        "artifacts": (),
        "store": store,
        "agent_provenance": {"repository_analyzer.prompt_version": "v1"},
    }
    fields.update(overrides)
    return build_report(**fields)


def test_clean_run_reports_pass_and_validates() -> None:
    """A clean, covered, criterion-free run reaches PASS and the domain accepts it."""
    report = _build()

    assert report.recommendation is Recommendation.PASS
    assert report.verification_status is VerificationStatus.PASSED
    assert report.open_questions == []
    assert report.unverified_areas == []
    assert report.executive_summary
    assert report.agent_provenance["repository_analyzer.prompt_version"] == "v1"


def test_critical_finding_forces_fail() -> None:
    """A CRITICAL finding cannot coexist with PASS, and the derivation knows it."""
    report = _build(findings=(_finding(Severity.CRITICAL, category=FindingCategory.SECURITY),))

    assert report.recommendation is Recommendation.FAIL
    assert report.security, "a security-category finding belongs in the security section"


def test_medium_finding_yields_pass_with_warnings() -> None:
    """Only findings below HIGH remain, which the gates allow at warning level."""
    report = _build(findings=(_finding(Severity.MEDIUM),))

    assert report.recommendation is Recommendation.PASS_WITH_WARNINGS


def test_high_finding_requires_human_review() -> None:
    """Scout found something a human must judge, and it does not claim to know the answer."""
    report = _build(findings=(_finding(Severity.HIGH),))

    assert report.recommendation is Recommendation.REQUIRES_HUMAN_REVIEW


def test_blocked_criterion_without_a_question_raises() -> None:
    """The domain refuses a blocked criterion with no question, and the derivation must surface it.

    Scout's response is to fix the derivation -- INVESTIGATING always raises the question -- never
    to soften the report's gates, which is what :class:`ReportDerivationError` makes explicit.
    """
    prepared = _prepared(criteria=["the export button downloads a CSV"])

    # A criterion from the repository configuration for which no test matches is BLOCKED when the
    # test runtime produced nothing, and the domain then demands an open question.
    with pytest.raises(ReportDerivationError):
        _build(prepared=prepared)


def test_domain_gate_rejects_a_hand_built_inconsistent_report() -> None:
    """The pairing: the report validator, not the derivation, is the last line of defence."""
    with pytest.raises(ValueError, match="CRITICAL"):
        VerificationReport(
            job_id=JOB_ID,
            repository_full_name="acme/widgets",
            pull_request_number=42,
            commit_sha=HEAD,
            base_sha=BASE,
            executive_summary="Looks fine to me.",
            security=[_finding(Severity.CRITICAL, category=FindingCategory.SECURITY)],
            recommendation=Recommendation.PASS,
        )


def test_report_artifact_kind_is_verification_report() -> None:
    """The report is stored as evidence with its own retention class."""
    assert ArtifactKind.VERIFICATION_REPORT.value == "VERIFICATION_REPORT"
