"""Report assembly and the recommendation derivation (spec sections 44, 77).

The recommendation is the one field in the whole platform that could most easily be inflated,
so it is *derived* here from recorded evidence and never accepted from a caller. The domain's
:class:`~scout_domain.models.report.VerificationReport` validator then re-checks the result and
refuses an inconsistent report. When derivation and validation disagree, the fix belongs in the
derivation -- this module raises
:class:`~scout_orchestrator.errors.ReportDerivationError` rather than loosening anything.

Derivation table, in precedence order. The first row that matches wins:

===============================================  ==========================
Evidence                                          Recommendation
===============================================  ==========================
a CRITICAL finding                                 ``FAIL``
failing or errored tests                           ``FAIL``
a FAILED acceptance criterion                      ``REQUEST_CHANGES``
an open question                                   ``REQUIRES_HUMAN_REVIEW``
a HIGH finding                                     ``REQUIRES_HUMAN_REVIEW``
an area marked UNVERIFIED                          ``REQUIRES_HUMAN_REVIEW``
a BLOCKED acceptance criterion                     ``REQUIRES_HUMAN_REVIEW``
only MEDIUM/LOW/INFO findings                      ``PASS_WITH_WARNINGS``
nothing outstanding                                ``PASS``
===============================================  ==========================

Why failing tests outrank a failed criterion, and why CRITICAL findings outrank everything:
"Scout could not even run the suite" and "something dangerous is in this change" are both
harder stops than "the feature does not do what was asked", and a softer label would let a
reviewer treat a hard stop as a discussion. Why a HIGH finding is ``REQUIRES_HUMAN_REVIEW``
rather than ``REQUEST_CHANGES``: Scout has found something a human must judge, and claiming it
knows the answer would be exactly the authority spec section 2 denies it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from pydantic import ValidationError

from scout_domain.enums import (
    CriterionStatus,
    FindingCategory,
    Recommendation,
    Severity,
    VerificationStatus,
)
from scout_domain.models.artifact import ArtifactKind
from scout_domain.models.evidence import EvidenceReference
from scout_domain.models.report import (
    DependencySummary,
    ReportSection,
    ReportSectionStatus,
    TestOutcomeSummary,
    VerificationReport,
)
from scout_observability import get_logger
from scout_orchestrator.errors import ReportDerivationError
from scout_orchestrator.llm_analysis import deterministic_narrative
from scout_orchestrator.stages.artifacts import ANALYZING_ARTIFACT_FILENAME
from scout_orchestrator.stages.feature_verification import (
    build_criteria_for,
    criteria_section,
    needs_unverified_section,
    passing_tests_from_artifacts,
    test_runtime_available,
)
from scout_orchestrator.stages.tool_stage import (
    COVERAGE_TOOL_NAME,
    TEST_TOOL_NAMES,
    covered_changed_paths_note,
    section_for_tool_result,
    summarize_tests,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from scout_domain.models.artifact import Artifact
    from scout_domain.models.finding import Finding
    from scout_domain.models.job import VerificationJob
    from scout_domain.models.question import Question
    from scout_domain.models.report import CriterionVerification
    from scout_domain.models.tool import ToolResult
    from scout_orchestrator.stages.prepared import PreparedState
    from scout_tools.artifacts import ArtifactStore

__all__ = [
    "DerivedEvidence",
    "RecommendationInputs",
    "build_report",
    "derive_evidence",
    "derive_recommendation",
    "executive_summary",
    "limitations_for",
    "sections_for_report",
    "unverified_areas",
    "verification_status_for",
]

_log = get_logger(__name__)

#: Findings in these categories make up the report's ``security`` section.
_SECURITY_CATEGORIES: Final[frozenset[FindingCategory]] = frozenset(
    {
        FindingCategory.SECURITY,
        FindingCategory.SECRETS,
        FindingCategory.DEPENDENCIES,
    }
)

#: Findings in this category make up the ``static_analysis`` section.
_STATIC_CATEGORIES: Final[frozenset[FindingCategory]] = frozenset({FindingCategory.STATIC_ANALYSIS})

#: Everything else that belongs in ``code_review``. Anything not listed anywhere lands in
#: ``other_findings``, so no finding can be silently dropped from the report by a category that
#: nobody updated.
_CODE_REVIEW_CATEGORIES: Final[frozenset[FindingCategory]] = frozenset(
    {
        FindingCategory.CORRECTNESS,
        FindingCategory.MAINTAINABILITY,
        FindingCategory.API_BEHAVIOR,
        FindingCategory.DATA,
        FindingCategory.CONCURRENCY,
        FindingCategory.ERROR_HANDLING,
        FindingCategory.TESTS,
        FindingCategory.PERFORMANCE,
    }
)

#: Tool names whose results are dependency scans, used to build ``DependencySummary``.
_DEPENDENCY_TOOLS: Final[frozenset[str]] = frozenset({"pip-audit", "npm-audit", "trivy"})

#: Ceiling on the evidence list attached to the report itself. Evidence locators are cheap, but
#: a report with ten thousand of them is unreadable and the findings already carry their own.
_MAX_REPORT_EVIDENCE: Final = 100


@dataclass(frozen=True, slots=True)
class RecommendationInputs:
    """Exactly the evidence the derivation is allowed to consider."""

    findings: tuple[Finding, ...]
    open_questions: tuple[Question, ...]
    criteria: tuple[CriterionVerification, ...]
    tests: TestOutcomeSummary
    unverified_areas: tuple[str, ...]

    @property
    def critical_findings(self) -> tuple[Finding, ...]:
        """Findings severe enough to stop the change outright."""
        return tuple(finding for finding in self.findings if finding.severity is Severity.CRITICAL)

    @property
    def high_findings(self) -> tuple[Finding, ...]:
        """Findings that require a human judgement."""
        return tuple(finding for finding in self.findings if finding.severity is Severity.HIGH)

    @property
    def failing_tests(self) -> int:
        """Tests that failed or errored, which is a harder stop than a missing criterion."""
        return self.tests.failed + self.tests.errors

    @property
    def failed_criteria(self) -> tuple[CriterionVerification, ...]:
        """Acceptance criteria the change demonstrably does not meet."""
        return tuple(
            criterion for criterion in self.criteria if criterion.status is CriterionStatus.FAILED
        )

    @property
    def blocked_criteria(self) -> tuple[CriterionVerification, ...]:
        """Acceptance criteria Scout could not attempt."""
        return tuple(
            criterion for criterion in self.criteria if criterion.status is CriterionStatus.BLOCKED
        )

    @property
    def unverified_criteria(self) -> tuple[CriterionVerification, ...]:
        """Criteria that are neither proven nor disproven."""
        return tuple(
            criterion
            for criterion in self.criteria
            if criterion.status is CriterionStatus.NOT_VERIFIED
        )


def derive_recommendation(inputs: RecommendationInputs) -> Recommendation:
    """Derive the recommendation from evidence, in the documented precedence order.

    Pure and total: every combination of inputs produces a recommendation, and the order of the
    checks is the whole algorithm. See the module docstring for the table and the reasoning
    behind each row's precedence.
    """
    if inputs.critical_findings or inputs.failing_tests:
        return Recommendation.FAIL
    if inputs.failed_criteria:
        return Recommendation.REQUEST_CHANGES
    needs_human = (
        bool(inputs.open_questions)
        or bool(inputs.high_findings)
        or bool(inputs.unverified_areas)
        or bool(inputs.blocked_criteria)
        or bool(inputs.unverified_criteria)
    )
    if needs_human:
        return Recommendation.REQUIRES_HUMAN_REVIEW
    if inputs.findings:
        # Only findings below HIGH remain, which the gates allow alongside a warning-level pass.
        return Recommendation.PASS_WITH_WARNINGS
    return Recommendation.PASS


def verification_status_for(recommendation: Recommendation) -> VerificationStatus:
    """Map a recommendation onto the job-level verification status."""
    if recommendation is Recommendation.PASS:
        return VerificationStatus.PASSED
    if recommendation is Recommendation.PASS_WITH_WARNINGS:
        return VerificationStatus.PASSED_WITH_WARNINGS
    if recommendation in {Recommendation.FAIL, Recommendation.REQUEST_CHANGES}:
        return VerificationStatus.FAILED
    return VerificationStatus.INCONCLUSIVE


def unverified_areas(sections: Sequence[ReportSection]) -> tuple[str, ...]:
    """Return the reasons of every ``UNVERIFIED`` section.

    Only ``UNVERIFIED`` counts. A ``SKIPPED`` section is a stated limitation of the environment
    (a scanner that is not installed, a tool the repository does not configure); an
    ``UNVERIFIED`` one is a check that was expected and did not produce evidence. Treating them
    alike would either block every run or hide a real gap.
    """
    areas: list[str] = []
    for section in sections:
        if section.status is not ReportSectionStatus.UNVERIFIED:
            continue
        areas.append(section.unverified_reason or section.summary or section.title)
    return tuple(areas)


def sections_for_report(
    *,
    prepared: PreparedState,
    tool_results: Sequence[ToolResult],
    criteria: Sequence[CriterionVerification],
    tests: TestOutcomeSummary,
    artifacts: Sequence[Artifact],
    store: ArtifactStore,
) -> tuple[ReportSection, ...]:
    """Rebuild every report section from persisted facts.

    Rebuilding rather than caching is what makes the report producible after a resume: the
    inputs are the tool-run rows, the artifact locators, and the prepared state, all of which
    are durable. The stage that produced a fact and the report builder that renders it share the
    same pure functions, so the two cannot drift.
    """
    sections: list[ReportSection] = [
        ReportSection(
            key="preparing.risk",
            title="Risk classification",
            status=ReportSectionStatus.INFO,
            summary=prepared.risk.explain()[:8000],
        ),
        ReportSection(
            key="analyzing.architecture_impact",
            title="Architecture impact",
            status=ReportSectionStatus.INFO,
            summary="repository structure and the impact of this change",
            details=[_architecture_impact(prepared, artifacts, store)[:4000]],
        ),
    ]

    sections.extend(
        section_for_tool_result(stage_name=result.stage, result=result) for result in tool_results
    )
    sections.append(_change_coverage_section(tests))
    if needs_unverified_section(criteria):
        sections.append(criteria_section(criteria))
    return tuple(sections)


@dataclass(frozen=True, slots=True)
class DerivedEvidence:
    """The parts of a report that are computed rather than recorded.

    Derived from the same persisted facts every time, which is what lets the orchestrator
    produce an identical report immediately after the pipeline and after a resume: there is no
    accumulated in-memory state to lose (Rule 5).
    """

    tests: TestOutcomeSummary
    criteria: tuple[CriterionVerification, ...]
    sections: tuple[ReportSection, ...]
    unverified_areas: tuple[str, ...]

    @property
    def tool_sections(self) -> tuple[ReportSection, ...]:
        """Every section that came from a recorded tool run."""
        return tuple(
            section
            for section in self.sections
            if section.key.split(".", 1)[0]
            in {
                "REVIEWING",
                "SECURITY_SCANNING",
                "TESTING",
            }
        )


def derive_evidence(
    *,
    prepared: PreparedState,
    tool_results: Sequence[ToolResult],
    artifacts: Sequence[Artifact],
    store: ArtifactStore,
) -> DerivedEvidence:
    """Compute the test summary, the criteria, and the report sections from persisted facts.

    Shared by report generation and by stage-input loading so the two can never disagree about
    what the evidence says.
    """
    change_set = prepared.change_set
    test_results = [result for result in tool_results if result.tool in TEST_TOOL_NAMES]
    coverage_result = next(
        (result for result in tool_results if result.tool == COVERAGE_TOOL_NAME), None
    )
    tests = summarize_tests(
        test_results,
        changed_paths=change_set.changed_paths,
        coverage_result=coverage_result,
    )
    passing_tests = passing_tests_from_artifacts(artifacts, store)
    criteria = build_criteria_for(
        config=prepared.repository_config,
        requirement_text=prepared.requirement_text,
        changed_paths=change_set.changed_paths,
        passing_tests=passing_tests,
        test_results=test_results,
        test_runtime_available=test_runtime_available(test_results, passing_tests),
        head_sha=prepared.repository_summary.commit_sha,
    )
    sections = sections_for_report(
        prepared=prepared,
        tool_results=tool_results,
        criteria=criteria,
        tests=tests,
        artifacts=artifacts,
        store=store,
    )
    return DerivedEvidence(
        tests=tests,
        criteria=criteria,
        sections=sections,
        unverified_areas=unverified_areas(sections),
    )


def limitations_for(
    prepared: PreparedState,
    sections: Sequence[ReportSection],
) -> tuple[str, ...]:
    """Collect the stated limitations of this verification.

    Two sources, both persisted: stages the repository switched off, and checks that were
    applicable but did not run (a scanner that is not installed, a tool the repository does not
    configure). Limitations are visible in the report without inflating the recommendation --
    what inflates it is an ``UNVERIFIED`` section, which is a different thing entirely.
    """
    notes: list[str] = [
        f"{stage} is disabled by .scout/engineering.yaml (verification.disabled_stages)"
        for stage in prepared.repository_config.verification.disabled_stages
    ]
    for section in sections:
        if section.status is not ReportSectionStatus.SKIPPED:
            continue
        reason = section.unverified_reason or section.summary or "no reason recorded"
        notes.append(f"{section.title} did not run: {reason}")
    return tuple(notes)


def build_report(
    *,
    job: VerificationJob,
    prepared: PreparedState,
    tool_results: Sequence[ToolResult],
    findings: Sequence[Finding],
    questions: Sequence[Question],
    artifacts: Sequence[Artifact],
    store: ArtifactStore,
    agent_provenance: Mapping[str, str] | None = None,
    known_limitations: Sequence[str] = (),
) -> VerificationReport:
    """Assemble the gated verification report (spec section 44).

    Raises:
        ReportDerivationError: when the derived recommendation is rejected by the report's own
            gates. That is a bug in the derivation, and it must be fixed there.
    """
    change_set = prepared.change_set
    open_questions = [question for question in questions if question.is_open]

    derived = derive_evidence(
        prepared=prepared,
        tool_results=tool_results,
        artifacts=artifacts,
        store=store,
    )
    tests = derived.tests
    criteria = derived.criteria
    sections = derived.sections
    areas = derived.unverified_areas

    inputs = RecommendationInputs(
        findings=tuple(findings),
        open_questions=tuple(open_questions),
        criteria=criteria,
        tests=tests,
        unverified_areas=areas,
    )
    recommendation = derive_recommendation(inputs)

    split = _split_findings(findings)
    limitations = list(known_limitations)
    for note in limitations_for(prepared, sections):
        if note not in limitations:
            limitations.append(note)
    coverage_note = covered_changed_paths_note(tests)
    if coverage_note is not None and coverage_note not in limitations:
        limitations.append(coverage_note)

    try:
        return VerificationReport(
            job_id=job.id,
            repository_full_name=job.repository_full_name,
            pull_request_number=job.pull_request_number,
            commit_sha=job.head_sha,
            base_sha=job.base_sha,
            executive_summary=executive_summary(
                job=job,
                recommendation=recommendation,
                findings=findings,
                tests=tests,
                open_questions=len(open_questions),
                unverified_areas=areas,
            ),
            changed_components=list(change_set.files),
            architecture_impact=_architecture_impact(prepared, artifacts, store),
            code_review=split["code_review"],
            security=split["security"],
            dependencies=_dependency_summary(tool_results, findings),
            static_analysis=split["static_analysis"],
            tests=tests,
            feature_verification=list(criteria),
            open_questions=open_questions,
            known_limitations=limitations,
            risk_level=prepared.risk.level,
            recommendation=recommendation,
            evidence=_report_evidence(findings),
            verification_status=verification_status_for(recommendation),
            sections=list(sections),
            tool_results=list(tool_results),
            other_findings=split["other"],
            unverified_areas=list(areas),
            agent_provenance=dict(agent_provenance or {}),
        )
    except ValidationError as exc:
        _log.error(
            "the derived report was rejected by its own gates",
            job_id=job.id,
            recommendation=recommendation.value,
            detail=str(exc),
        )
        raise ReportDerivationError(
            "the derived recommendation is inconsistent with the recorded evidence; the "
            "derivation must be corrected rather than the report's gates loosened",
            job_id=job.id,
            recommendation=recommendation.value,
            open_questions=len(open_questions),
            failed_tests=inputs.failing_tests,
            critical_findings=len(inputs.critical_findings),
            unverified_areas=len(areas),
            validation=str(exc)[:2000],
        ) from exc


def executive_summary(
    *,
    job: VerificationJob,
    recommendation: Recommendation,
    findings: Sequence[Finding],
    tests: TestOutcomeSummary,
    open_questions: int,
    unverified_areas: Sequence[str],
) -> str:
    """Render the report's executive summary.

    Deterministic prose assembled from recorded facts, not a narrative: the summary is the first
    thing a reviewer reads and the last place a paragraph of generated text should decide what
    they believe.
    """
    counts = _severity_counts(findings)
    parts = [
        (
            f"Scout verified {job.repository_full_name} pull request "
            f"#{job.pull_request_number} at commit {job.head_sha[:12]} and recommends "
            f"{recommendation.value}."
        )
    ]
    if tests.total:
        parts.append(
            f"The test suite ran {tests.total} test(s): {tests.passed} passed, "
            f"{tests.failed} failed, {tests.errors} errored, {tests.skipped} skipped."
        )
    else:
        parts.append("No test suite result is available for this revision.")
    if findings:
        rendered = ", ".join(
            f"{count} {severity.value}" for severity, count in counts.items() if count
        )
        parts.append(f"Findings: {rendered}.")
    else:
        parts.append("No findings were recorded.")
    if open_questions:
        parts.append(
            f"{open_questions} question(s) require a human answer before the recommendation "
            "can be acted on."
        )
    if unverified_areas:
        parts.append(
            f"{len(unverified_areas)} area(s) could not be verified and are listed explicitly "
            "in this report."
        )
    return " ".join(parts)[:8000]


# --------------------------------------------------------------------------------------
# Internals
# --------------------------------------------------------------------------------------


def _split_findings(findings: Sequence[Finding]) -> dict[str, list[Finding]]:
    """Split findings into the report's four finding-carrying sections."""
    split: dict[str, list[Finding]] = {
        "code_review": [],
        "security": [],
        "static_analysis": [],
        "other": [],
    }
    for finding in findings:
        if finding.category in _SECURITY_CATEGORIES:
            split["security"].append(finding)
        elif finding.category in _STATIC_CATEGORIES:
            split["static_analysis"].append(finding)
        elif finding.category in _CODE_REVIEW_CATEGORIES:
            split["code_review"].append(finding)
        else:
            split["other"].append(finding)
    return split


def _severity_counts(findings: Sequence[Finding]) -> dict[Severity, int]:
    """Count findings per severity, worst first, including zeros."""
    counts = dict.fromkeys(reversed(list(Severity)), 0)
    for finding in findings:
        counts[finding.severity] += 1
    return counts


def _dependency_summary(
    tool_results: Sequence[ToolResult], findings: Sequence[Finding]
) -> DependencySummary:
    """Summarise dependency and vulnerability scanning from the recorded results."""
    dependency_results = [result for result in tool_results if result.tool in _DEPENDENCY_TOOLS]
    dependency_findings = [
        finding
        for finding in findings
        if finding.category is FindingCategory.DEPENDENCIES and finding.severity >= Severity.MEDIUM
    ]
    highest = None
    if dependency_findings:
        highest = max(
            (finding.severity for finding in dependency_findings),
            key=lambda severity: severity.rank,
        )
    return DependencySummary(
        ecosystems=sorted({result.tool for result in dependency_results}),
        vulnerable_packages=len(dependency_findings),
        outdated_packages=0,
        highest_severity=highest,
        tool_results=list(dependency_results),
    )


def _report_evidence(findings: Sequence[Finding]) -> list[EvidenceReference]:
    """Aggregate the findings' evidence, de-duplicated and bounded."""
    seen: set[tuple[str, str]] = set()
    evidence: list[EvidenceReference] = []
    for finding in findings:
        for reference in finding.evidence:
            key = (reference.kind.value, reference.reference_id)
            if key in seen:
                continue
            seen.add(key)
            evidence.append(reference)
            if len(evidence) >= _MAX_REPORT_EVIDENCE:
                return evidence
    return evidence


def _change_coverage_section(tests: TestOutcomeSummary) -> ReportSection:
    """Render the change-coverage census as its own section.

    Mirrors :func:`scout_orchestrator.stages.testing._coverage_section` deliberately: the report
    is rebuilt from persisted facts rather than from the testing stage's result, so both paths
    must produce the same section, and the shared :func:`covered_changed_paths_note` is what
    keeps the wording identical.
    """
    note = covered_changed_paths_note(tests)
    if note is not None:
        return ReportSection(
            key="testing.change_coverage",
            title="Change coverage",
            status=ReportSectionStatus.WARNING,
            summary=note[:8000],
        )
    summary = (
        "every source file changed by this pull request is exercised by the tests"
        if tests.change_is_covered is True
        else (
            "no coverage measurement and no test change were available, so whether the change "
            "is exercised could not be determined"
        )
    )
    return ReportSection(
        key="testing.change_coverage",
        title="Change coverage",
        status=ReportSectionStatus.INFO,
        summary=summary,
    )


def _architecture_impact(
    prepared: PreparedState,
    artifacts: Sequence[Artifact],
    store: ArtifactStore,
) -> str:
    """Return the architecture narrative, preferring the recorded one.

    The ANALYZING stage stores its narrative (model-assisted or deterministic) as an artifact so
    that a report generated after a resume quotes the same text the stage produced. When that
    artifact is missing -- a job resumed before ANALYZING ran, or a store that lost the blob --
    the deterministic narrative is regenerated from the prepared state, which is honest because
    it is derived from the same facts.
    """
    recorded = next(
        (
            artifact
            for artifact in artifacts
            if artifact.kind is ArtifactKind.REPOSITORY_SUMMARY
            and artifact.filename == ANALYZING_ARTIFACT_FILENAME
        ),
        None,
    )
    if recorded is not None:
        try:
            return store.get_bytes(recorded).decode("utf-8", errors="replace")[:8000]
        except Exception as exc:
            _log.warning(
                "the recorded architecture narrative could not be read",
                artifact_id=recorded.id,
                error=f"{type(exc).__name__}: {exc}",
            )
    return deterministic_narrative(
        summary=prepared.repository_summary, change_set=prepared.change_set
    )
