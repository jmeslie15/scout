"""TESTING: did the tests pass, and do they cover the change? (spec sections 65, 67)

Two questions, deliberately kept distinct. "The suite passed" is a machine fact from the test
runner. "The change is exercised by the suite" is a second, weaker fact that a global coverage
percentage hides, so this stage populates
:attr:`~scout_domain.models.report.TestOutcomeSummary.change_is_covered` and
``uncovered_changed_paths`` (see :func:`scout_orchestrator.stages.tool_stage.summarize_tests`).

``pytest`` is declared a *required* tool. If it is not present in the environment, the stage
emits an ``UNVERIFIED`` section rather than an empty pass, because "we could not run the tests"
must never render as "the tests passed". That single rule is what makes the recommendation
derivation trustworthy: the report cannot reach ``PASS`` while the test suite is unknown.
"""

from __future__ import annotations

from typing import Final

from scout_domain.enums import EvidenceKind, FindingCategory, JobStatus, Severity
from scout_domain.ids import IdKind, new_id
from scout_domain.models.evidence import EvidenceReference
from scout_domain.models.finding import Finding, FindingOrigin
from scout_domain.models.report import TestOutcomeSummary
from scout_orchestrator.stages.checkout import ensure_checkout
from scout_orchestrator.stages.context import BaseStage, StageContext, StageResult
from scout_orchestrator.stages.tool_stage import (
    COVERAGE_TOOL_NAME,
    ToolRunBundle,
    change_coverage_section,
    changed_source_without_test_edit,
    run_tool_stage,
    summarize_tests,
)
from scout_tools.base import ToolCategory

__all__ = ["TestingStage"]

_STAGE: Final = JobStatus.TESTING

_TESTING_CATEGORIES: Final[tuple[ToolCategory, ...]] = (
    ToolCategory.TEST,
    ToolCategory.COVERAGE,
)

#: The test runner Scout requires. Its absence is an unverified area, not a skip.
_REQUIRED_TEST_TOOLS: Final[frozenset[str]] = frozenset({"pytest"})

#: Title used when the configuration asks for a finding about a source-only change.
_UNCOVERED_TITLE: Final = "source changed without any test being added or modified"


class TestingStage(BaseStage):
    """Run the test suite and the coverage measurement, then summarise both."""

    stage = _STAGE

    async def run(self, ctx: StageContext) -> StageResult:
        """Materialise the pinned revision, run the suite, and summarise the result.

        The checkout happens here because the workspace is per-stage: running pytest in a directory
        with no tests would collect nothing and report that as a pass.
        """
        await ensure_checkout(ctx)
        bundle = await run_tool_stage(
            ctx,
            categories=_TESTING_CATEGORIES,
            section_title="Tests",
            required_tool_names=_REQUIRED_TEST_TOOLS,
        )

        coverage_result = next(
            (result for result in bundle.results if result.tool == COVERAGE_TOOL_NAME), None
        )
        tests = summarize_tests(
            bundle.results,
            changed_paths=ctx.changed_paths,
            coverage_result=coverage_result,
        )

        findings = list(bundle.findings)
        without_test_edit = changed_source_without_test_edit(ctx.changed_paths)
        if (
            ctx.require_repository_config().verification.require_tests_for_code_changes
            and without_test_edit
        ):
            findings.append(
                _uncovered_change_finding(
                    ctx, bundle=bundle, without_test_edit=without_test_edit
                )
            )

        return StageResult.succeeded_for(
            self.stage,
            narrative=_narrative(tests, bundle),
            tool_results=bundle.results,
            findings=tuple(findings),
            artifacts=ctx.artifacts.take(),
            sections=(
                *bundle.sections,
                change_coverage_section(tests=tests, changed_paths=ctx.changed_paths),
            ),
            tests=tests,
            payload={
                "executed_tools": list(bundle.executed_tools),
                "skipped_tools": [list(item) for item in bundle.skipped_tools],
                "unverified_tools": [list(item) for item in bundle.unverified_tools],
                "limitations": list(bundle.limitations()),
                "change_is_covered": tests.change_is_covered,
                "uncovered_changed_paths": list(tests.uncovered_changed_paths),
                "changed_source_without_test_edit": list(without_test_edit),
            },
        )


def _narrative(tests: TestOutcomeSummary, bundle: ToolRunBundle) -> str:
    """Summarise test execution and coverage without conflating the two."""
    if tests.total == 0 and tests.change_is_covered is None:
        return "no test result is available for this revision"
    base = (
        f"{tests.passed} passed, {tests.failed} failed, {tests.errors} errored, "
        f"{tests.skipped} skipped"
    )
    if tests.change_is_covered is True:
        base += "; every changed source path is exercised by a test in this change"
    elif tests.change_is_covered is False:
        base += (
            "; the change modifies source without adding or modifying a test, so no test covers it"
        )
    if bundle.unverified_tools:
        base += f"; {len(bundle.unverified_tools)} check(s) produced no result"
    return base


def _uncovered_change_finding(
    ctx: StageContext,
    *,
    bundle: ToolRunBundle,
    without_test_edit: tuple[str, ...],
) -> Finding:
    """Build the finding a repository asks for when code changes without a test edit (spec 67).

    The claim is deliberately narrow: the change modified these source files and no test file. It
    does not assert that no test executes them -- only a coverage measurement could support that --
    and the description says so, so the finding cannot be read as a coverage verdict.
    """
    test_run = next(
        (result for result in bundle.results if result.tool in {"pytest", "vitest", "jest"}),
        None,
    )
    changed_paths_artifact_id = ctx.require_change_set().changed_paths_artifact_id
    evidence: list[EvidenceReference] = []
    if changed_paths_artifact_id is not None:
        evidence.append(
            EvidenceReference(
                kind=EvidenceKind.ARTIFACT,
                reference_id=changed_paths_artifact_id,
                summary="the pinned changed-path list this census is computed from",
                artifact_id=changed_paths_artifact_id,
            )
        )
    if test_run is not None:
        evidence.append(
            EvidenceReference(
                kind=EvidenceKind.TEST_RUN,
                reference_id=test_run.id,
                summary=f"{test_run.tool} executed the suite for this revision",
                artifact_id=test_run.stdout_artifact_id,
            )
        )
    listed = ", ".join(without_test_edit[:5])
    return Finding(
        id=new_id(IdKind.FINDING),
        job_id=ctx.job.id,
        severity=Severity.MEDIUM,
        category=FindingCategory.TESTS,
        title=_UNCOVERED_TITLE,
        description=(
            "This change modifies source files without adding or modifying any test: "
            f"{listed}. Scout cannot say whether the existing suite exercises them, because no "
            "coverage measurement was available."
        ),
        repository=ctx.job.repository_full_name,
        evidence=evidence,
        recommendation=(
            "Add or extend a test that exercises the changed behaviour, or record why the "
            "change is intentionally untested."
        ),
        confidence=1.0,
        origin=FindingOrigin.TOOL,
        source="change_coverage",
    )
