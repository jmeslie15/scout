"""The shared tool-execution path for REVIEWING, SECURITY_SCANNING, and TESTING.

Three stages ask the same question in different vocabularies: "which deterministic tools
apply here, what did they observe, and what does that mean for the report?". This module
answers it once so that the review, security, and testing stages differ only in which tool
categories they plan.

Two decisions worth stating, because both look like duplication until you see why:

**The command is run here rather than through** :meth:`scout_tools.base.CommandTool.run`.
That method returns a :class:`~scout_domain.models.tool.ToolResult` and deliberately drops
the tool-native :class:`~scout_tools.base.ParsedFinding` objects it parsed, keeping the tool
vocabulary out of the domain contract. A stage, however, is exactly the layer whose job is
turning parsed results into domain findings, so it needs both halves. This module therefore
performs the same sequence (version probe, argv, sandbox run, artifact capture, safe
interpretation) and returns the result *and* the parse. The alternatives -- running the tool
twice, or re-reading capture files after the workspace has been torn down -- would be worse.

**A tool that is missing from the environment is not the same as a tool that broke.** The
first is a stated limitation of where Scout ran; the second is silence where evidence was
expected. The first becomes a ``SKIPPED`` report section, the second an ``UNVERIFIED`` one,
because spec section 2 forbids claiming a clean result from a check that never happened.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from scout_domain.enums import EvidenceKind, FindingCategory, ToolStatus
from scout_domain.ids import IdKind, new_id
from scout_domain.models.artifact import ArtifactKind
from scout_domain.models.base import utcnow
from scout_domain.models.evidence import EvidenceReference
from scout_domain.models.finding import Finding, FindingOrigin
from scout_domain.models.report import ReportSection, ReportSectionStatus, TestOutcomeSummary
from scout_domain.models.tool import ToolResult, ToolSummary
from scout_observability import get_logger, instrument_span
from scout_sandbox.command import CommandOutcome, SandboxCommand
from scout_sandbox.errors import SandboxError
from scout_tools.base import CommandTool, ParsedFinding, ToolCategory, ToolContext, ToolOutcome
from scout_tools.detection import classify_paths

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from scout_domain.models.artifact import Artifact
    from scout_orchestrator.stages.context import StageContext
    from scout_tools.base import Tool

__all__ = [
    "COVERAGE_TOOL_NAME",
    "TEST_TOOL_NAMES",
    "ToolRunBundle",
    "finding_category_for",
    "run_tool_stage",
    "section_for_tool_result",
    "summarize_tests",
]

_log = get_logger(__name__)

#: Tool names whose results describe test execution, used to select the results that feed
#: :class:`TestOutcomeSummary` without depending on a tool object staying in scope.
TEST_TOOL_NAMES: Final[frozenset[str]] = frozenset(
    {"pytest", "vitest", "jest", "mocha", "go-test", "cargo-test", "rspec"}
)

#: Tool name of the coverage measurement. Named here so the testing stage and the report
#: builder agree without importing each other.
COVERAGE_TOOL_NAME: Final = "coverage"

#: Longest excerpt carried on a finding's evidence. Excerpts are pointers for a human
#: reader; the full output lives in the artifact.
_MAX_EXCERPT: Final = 600
_MAX_SUMMARY: Final = 500
_MAX_TITLE: Final = 200
_MAX_FILE: Final = 1024
_MAX_NARRATIVE: Final = 2000
_MAX_ERROR: Final = 4000


@dataclass(frozen=True, slots=True)
class ToolRunBundle:
    """Everything a tool-running stage produced, in one value."""

    results: tuple[ToolResult, ...] = ()
    findings: tuple[Finding, ...] = ()
    sections: tuple[ReportSection, ...] = ()
    #: ``(tool, reason)`` for tools that did not apply, were disabled, or are absent here.
    skipped_tools: tuple[tuple[str, str], ...] = ()
    #: ``(tool, reason)`` for tools that ran and could not produce trustworthy evidence.
    unverified_tools: tuple[tuple[str, str], ...] = ()
    #: Names of the tools that actually executed.
    executed_tools: tuple[str, ...] = ()

    def limitations(self) -> tuple[str, ...]:
        """Render skips and failures as report limitations.

        Kept separate from ``unverified_areas``: a missing scanner is a limitation of the
        environment worth stating, whereas a scanner that ran and produced nothing is a
        genuine gap in verification. Conflating them would either inflate the
        recommendation for every local run or hide a real gap.
        """
        lines: list[str] = [f"{tool}: {reason}" for tool, reason in self.skipped_tools]
        lines.extend(f"{tool}: {reason}" for tool, reason in self.unverified_tools)
        return tuple(lines)


def finding_category_for(category: ToolCategory) -> FindingCategory:
    """Map a tool's category onto the report section its findings belong in.

    The mapping is total by construction: the fallback is the security bucket, which is the
    conservative choice for any future scanner. A new scanner landing in the wrong section
    is a presentation problem; a security tool silently landing in "other findings" would
    weaken the report's most load-bearing section.
    """
    if category in {ToolCategory.TEST, ToolCategory.COVERAGE}:
        return FindingCategory.TESTS
    if category in {
        ToolCategory.LINT,
        ToolCategory.FORMAT,
        ToolCategory.TYPECHECK,
        ToolCategory.BUILD,
    }:
        return FindingCategory.STATIC_ANALYSIS
    if category is ToolCategory.SECRET_SCAN:
        return FindingCategory.SECRETS
    if category is ToolCategory.DEPENDENCY:
        return FindingCategory.DEPENDENCIES
    return FindingCategory.SECURITY


async def run_tool_stage(
    ctx: StageContext,
    *,
    categories: Sequence[ToolCategory],
    section_title: str,
    tool_names: Sequence[str] | None = None,
    required_tool_names: frozenset[str] = frozenset(),
) -> ToolRunBundle:
    """Plan, execute, and interpret every applicable tool for one stage.

    Args:
        ctx: The stage context; supplies the workspace, sandbox, artifact store, registry,
            repository configuration, and the prepared change set.
        categories: The tool categories this stage owns.
        section_title: Human-facing title for the per-tool report sections.
        tool_names: Restrict to these names (a repository's explicit tool allowlist).
        required_tool_names: Tools whose *absence* is an unverified area rather than a
            skipped one. TESTING passes ``{"pytest"}`` because "we could not run the test
            suite" must never read as a clean pass.

    Returns:
        A :class:`ToolRunBundle` carrying tool results, domain findings, report sections,
        and the reasons other tools did not contribute.
    """
    repository_config = ctx.require_repository_config()
    summary = ctx.require_repository_summary()
    risk = ctx.inputs.risk

    tool_context = ToolContext(
        job_id=ctx.job.id,
        stage=ctx.stage_name,
        repository_full_name=ctx.job.repository_full_name,
        commit_sha=ctx.job.head_sha,
        workspace=ctx.workspace,
        sandbox=ctx.sandbox,
        artifacts=ctx.artifacts,
        repository_config=repository_config,
        changed_paths=ctx.changed_paths,
        risk_level=None if risk is None else risk.level,
        repository_summary=summary,
        command_overrides=_command_overrides(repository_config),
    )

    plan = ctx.tools.plan(tool_context, categories=list(categories), names=tool_names)

    results: list[ToolResult] = []
    findings: list[Finding] = []
    skipped: list[tuple[str, str]] = []
    unverified: list[tuple[str, str]] = []
    executed: list[str] = []

    for tool_name, reason in plan.skipped:
        skipped.append((tool_name, reason))
        results.append(
            _synthetic_tool_result(
                ctx,
                tool_name=tool_name,
                reason=reason,
                status=ToolStatus.NOT_APPLICABLE,
            )
        )

    for tool in plan.selected:
        unavailable = await _tool_unavailable_reason(tool, tool_context)
        if unavailable is not None:
            required = tool.name in required_tool_names
            if required:
                unverified.append((tool.name, unavailable))
            else:
                skipped.append((tool.name, unavailable))
            # A durable record of the decision, not just an in-memory section: a resumed job
            # rebuilds its report from persisted rows, so a check that could not run has to
            # leave one or the report would silently omit it.
            results.append(
                _synthetic_tool_result(
                    ctx,
                    tool_name=tool.name,
                    reason=unavailable,
                    status=ToolStatus.ERROR if required else ToolStatus.SKIPPED,
                    infrastructure_failure=required,
                )
            )
            continue

        result, outcome = await _execute_tool(tool, tool_context)
        executed.append(tool.name)
        results.append(result)

        if result.infrastructure_failure or result.status in {
            ToolStatus.ERROR,
            ToolStatus.TIMEOUT,
        }:
            # Never a finding about the pull request: the tool never produced one.
            unverified.append((tool.name, _infrastructure_reason(result)))
            continue

        findings.extend(
            _findings_from_outcome(
                outcome,
                category=finding_category_for(tool.category),
                tool_result=result,
                job_id=ctx.job.id,
                repository=ctx.job.repository_full_name,
            )
        )

    sections = [
        section_for_tool_result(
            stage_name=ctx.stage_name, result=result, section_title=section_title
        )
        for result in results
    ]

    return ToolRunBundle(
        results=tuple(results),
        findings=tuple(findings),
        sections=tuple(sections),
        skipped_tools=tuple(skipped),
        unverified_tools=tuple(unverified),
        executed_tools=tuple(executed),
    )


def _synthetic_tool_result(
    ctx: StageContext,
    *,
    tool_name: str,
    reason: str,
    status: ToolStatus,
    infrastructure_failure: bool = False,
) -> ToolResult:
    """Build the durable record for a tool that was considered and did not run.

    Reported sections are re-derived from persisted tool-run rows when the report is built, so a
    check that never executed still has to leave a row behind -- otherwise a resumed job would
    lose the "this was not verified" signal, which is exactly the silence spec section 2
    forbids.
    """
    now = utcnow()
    return ToolResult(
        id=new_id(IdKind.TOOL_RUN),
        job_id=ctx.job.id,
        stage=ctx.stage_name,
        tool=tool_name[:100],
        command=f"{tool_name} (not run)"[:2000],
        argv=[],
        exit_code=None,
        status=status,
        started_at=now,
        completed_at=now,
        duration_seconds=0.0,
        summary=ToolSummary(
            values={"not_run": True, "infrastructure_failure": infrastructure_failure}
        ),
        error=reason[:4000] if infrastructure_failure else None,
        infrastructure_failure=infrastructure_failure,
        sandbox_run_id=None,
        narrative=reason[:2000],
        metadata={"not_run": True, "reason": reason[:2000]},
    )


def coverage_measurement_present(result: ToolResult | None) -> bool:
    """Whether a tool result is an actual coverage *measurement*.

    Why this is not simply "a ``coverage`` row exists": a tool that was not applicable, or that
    failed for infrastructure reasons, is still recorded as a row so the decision is visible in a
    report rebuilt after a resume. Those rows carry no measurement, and treating one as "coverage
    ran and found nothing uncovered" would assert that a change is exercised by tests on the
    strength of a tool that never executed.
    """
    if result is None:
        return False
    if result.status not in {ToolStatus.PASSED, ToolStatus.FAILED}:
        return False
    return "percent_covered" in result.summary.values


def changed_source_without_test_edit(changed_paths: Sequence[str]) -> tuple[str, ...]:
    """Return changed *source* paths for a change that modified no test file.

    This is a census, not a measurement. It answers "did this change arrive with a test edit?",
    which is a useful and honest observation, and it is deliberately *not* the same question as
    "does a test execute this code?" -- an existing test may already cover the change, and a
    freshly added test may not reach the changed line. Callers that need the second question need
    a coverage measurement.
    """
    buckets = classify_paths(changed_paths)
    if buckets["tests"]:
        return ()
    return tuple(buckets["source"])


def summarize_tests(
    results: Sequence[ToolResult],
    *,
    changed_paths: Sequence[str] = (),
    coverage_result: ToolResult | None = None,
) -> TestOutcomeSummary:
    """Aggregate test execution and report what is *established* about change coverage (spec 67).

    ``change_is_covered`` answers exactly one question -- "does a test execute the changed
    code?" -- and only a coverage measurement can answer it:

    * a measurement ran and no changed path was left uncovered -> ``True``;
    * a measurement ran and some changed path is uncovered -> ``False``, with the paths listed;
    * no measurement -> ``None``, deliberately not ``False``. Scout does not claim that nothing
      exercises the change, and it does not claim that something does.

    The related-but-different question, "did this change arrive with a test edit?", is answered by
    :func:`changed_source_without_test_edit` and surfaced as its own limitation
    (:func:`scan_without_test_edit_note`) rather than folded into this field. Conflating the two
    made the field mean two things depending on which branch produced it, which is precisely the
    kind of ambiguity a report may not have.
    """
    total = passed = failed = skipped = errored = 0
    duration = 0.0
    for result in results:
        if result.tool not in TEST_TOOL_NAMES:
            continue
        total += result.summary.get_int("total")
        passed += result.summary.get_int("passed")
        failed += result.summary.get_int("failed")
        skipped += result.summary.get_int("skipped")
        errored += result.summary.get_int("errored")
        duration += float(result.summary.values.get("duration_seconds") or 0.0)

    summary = TestOutcomeSummary(
        total=total,
        passed=passed,
        failed=failed,
        skipped=skipped,
        errors=errored,
        duration_seconds=duration,
    )

    if not coverage_measurement_present(coverage_result):
        # Nothing measured execution, so the field stays undetermined even though the change may
        # well be covered -- by an existing test, or by a test this change added.
        del changed_paths
        return summary.model_copy(
            update={"change_is_covered": None, "uncovered_changed_paths": []}
        )

    assert coverage_result is not None  # guaranteed by coverage_measurement_present
    values = coverage_result.summary.values
    uncovered = _split_paths(values.get("uncovered_changed_paths"))
    percent = values.get("percent_covered")
    return summary.model_copy(
        update={
            "change_is_covered": not uncovered,
            "coverage_percent": None if percent is None else float(percent),
            "uncovered_changed_paths": uncovered,
        }
    )


def scan_without_test_edit_note(changed_paths: Sequence[str]) -> str | None:
    """Render the test-edit census as a stated limitation, or ``None`` when it found nothing.

    Kept as a limitation rather than a verdict because it is a proxy: a change that arrived
    without a test edit may still be covered by the existing suite. The repository's
    ``verification.require_tests_for_code_changes`` setting decides whether it becomes a finding.
    """
    paths = changed_source_without_test_edit(changed_paths)
    if not paths:
        return None
    listed = ", ".join(paths[:5])
    remainder = len(paths) - 5
    suffix = "" if remainder <= 0 else f" and {remainder} more"
    return (
        "no coverage measurement was available, and this change modifies source files without "
        f"adding or modifying any test, so Scout cannot state whether a test exercises: "
        f"{listed}{suffix}"
    )


def change_coverage_section(
    *,
    tests: TestOutcomeSummary,
    changed_paths: Sequence[str] = (),
) -> ReportSection:
    """Render the change-coverage state as a report section.

    One implementation for the testing stage and the report builder, so a report rebuilt after a
    resume cannot describe the coverage state differently from the run that produced it. The
    section states *which* question was answered -- a measurement, or the test-edit census -- so a
    reader never has to guess what a ``True`` meant.
    """
    if tests.coverage_percent is not None or tests.change_is_covered is not None:
        if tests.change_is_covered is False and tests.uncovered_changed_paths:
            listed = ", ".join(tests.uncovered_changed_paths[:5])
            return ReportSection(
                key="testing.change_coverage",
                title="Change coverage",
                status=ReportSectionStatus.WARNING,
                summary=(
                    f"a coverage measurement ran and left {len(tests.uncovered_changed_paths)} "
                    f"changed source path(s) not fully executed: {listed}"
                ),
            )
        return ReportSection(
            key="testing.change_coverage",
            title="Change coverage",
            status=ReportSectionStatus.INFO,
            summary=(
                "a coverage measurement ran and every changed source path is executed by the "
                "suite"
                if tests.change_is_covered is True
                else "a coverage measurement ran; see the tool output for details"
            ),
        )

    census_note = scan_without_test_edit_note(changed_paths)
    if census_note is not None:
        return ReportSection(
            key="testing.change_coverage",
            title="Change coverage",
            status=ReportSectionStatus.WARNING,
            summary=census_note[:8000],
        )
    return ReportSection(
        key="testing.change_coverage",
        title="Change coverage",
        status=ReportSectionStatus.INFO,
        summary=(
            "no coverage measurement was available, so whether a test executes the changed code "
            "was not determined. This change did add or modify a test."
            if changed_paths
            else "no coverage measurement was available, so whether a test executes the changed "
            "code was not determined."
        ),
    )


# --------------------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------------------


async def _execute_tool(tool: Tool, context: ToolContext) -> tuple[ToolResult, ToolOutcome]:
    """Run one tool, retaining both the domain result and the tool-native parse."""
    if isinstance(tool, CommandTool):
        return await _execute_command_tool(tool, context)
    # A non-command tool (an HTTP-based scanner, a future capability) owns its own
    # execution. It cannot expose ParsedFinding objects through this path, so its result is
    # surfaced as-is and the narrative carries whatever it observed.
    result = await tool.run(context)
    return result, ToolOutcome(
        status=result.status,
        summary=result.summary,
        narrative=result.narrative,
        error=result.error,
        infrastructure_failure=result.infrastructure_failure,
    )


async def _execute_command_tool(
    tool: CommandTool,
    context: ToolContext,
) -> tuple[ToolResult, ToolOutcome]:
    """Run one command tool, mirroring :meth:`scout_tools.base.CommandTool.run`.

    See the module docstring for why this exists rather than delegating to ``run``. The
    artifact capture ordering is preserved exactly: stdout and stderr are stored *before*
    interpretation, so a parser bug cannot lose the raw output it failed to read.
    """
    tool_run_id = new_id(IdKind.TOOL_RUN)
    started_at = utcnow()
    monotonic_start = time.monotonic()

    with instrument_span(
        "orchestrator.tool.run",
        attributes={
            "tool.name": tool.name,
            "tool.category": tool.category.value,
            "job.id": context.job_id,
            "job.stage": context.stage,
        },
    ):
        version = await tool.resolve_version(context)
        argv = list(tool.build_argv(context))
        command = SandboxCommand(
            argv=argv,
            timeout_seconds=tool.effective_timeout(context),
            label=tool.name,
        )
        outcome = await context.sandbox.run(command)
        duration = time.monotonic() - monotonic_start

        stdout_artifact = tool.store_artifact(
            context,
            outcome.stdout_path,
            kind=ArtifactKind.TOOL_STDOUT,
            tool_run_id=tool_run_id,
            filename=f"{tool.name}-stdout.log",
        )
        stderr_artifact = tool.store_artifact(
            context,
            outcome.stderr_path,
            kind=ArtifactKind.TOOL_STDERR,
            tool_run_id=tool_run_id,
            filename=f"{tool.name}-stderr.log",
        )

        parsed = _interpret_safely(tool, outcome, context)

        report_artifact = None
        if parsed.report_path:
            report_file = _resolve_report(context, parsed.report_path)
            if report_file is not None:
                report_artifact = tool.store_artifact(
                    context,
                    report_file,
                    kind=parsed.report_kind,
                    content_type=parsed.report_content_type,
                    tool_run_id=tool_run_id,
                    filename=f"{tool.name}-report",
                )

    result = ToolResult(
        id=tool_run_id,
        job_id=context.job_id,
        stage=context.stage,
        tool=tool.name,
        version=version,
        command=command.display or tool.name,
        argv=argv,
        exit_code=outcome.exit_code,
        status=parsed.status,
        started_at=started_at,
        completed_at=utcnow(),
        duration_seconds=duration,
        stdout_artifact_id=_artifact_id(stdout_artifact),
        stderr_artifact_id=_artifact_id(stderr_artifact),
        report_artifact_id=_artifact_id(report_artifact),
        summary=parsed.summary,
        error=parsed.error,
        infrastructure_failure=parsed.infrastructure_failure,
        sandbox_run_id=outcome.sandbox_run_id,
        narrative=parsed.narrative[:_MAX_NARRATIVE],
        metadata={
            "findings_count": len(parsed.findings),
            "stdout_bytes": outcome.stdout_bytes,
            "stderr_bytes": outcome.stderr_bytes,
            "stdout_truncated": outcome.stdout_truncated,
            "stderr_truncated": outcome.stderr_truncated,
            "launch_error": outcome.launch_error,
        },
    )
    return result, parsed


def _interpret_safely(
    tool: CommandTool,
    outcome: CommandOutcome,
    context: ToolContext,
) -> ToolOutcome:
    """Interpret an outcome, turning a parser bug into a visible infrastructure failure.

    A crash while parsing a scanner's output is a bug in Scout, not a finding about a pull
    request. Converting it to a typed, recorded ``ERROR`` keeps Rule 12 satisfied while
    making sure a malformed report cannot fail a job with an opaque traceback.
    """
    try:
        return tool.interpret(outcome, context)
    except Exception as exc:
        _log.error(
            "tool output parser raised",
            tool=tool.name,
            job_id=context.job_id,
            error=str(exc),
            exc_info=True,
        )
        return ToolOutcome(
            status=ToolStatus.ERROR,
            narrative=(
                f"{tool.name} produced output that Scout could not parse; the raw output is "
                "retained as an artifact and this check is reported as unverified rather "
                "than as a finding."
            ),
            error=f"parser failure: {type(exc).__name__}: {exc}"[:_MAX_ERROR],
            infrastructure_failure=True,
        )


def _resolve_report(context: ToolContext, relative_path: str) -> Path | None:
    """Resolve a tool-reported report path inside the workspace, or ``None``.

    A path that escapes the workspace is refused by :class:`scout_sandbox.Workspace`, and a
    refused path must not lose the tool's result: the stdout artifact is already stored.
    """
    try:
        return context.workspace.resolve(relative_path)
    except Exception:
        _log.warning("tool reported an unusable report path", path=relative_path)
        return None


def _artifact_id(artifact: Artifact | None) -> str | None:
    """Return an artifact record's id, or ``None`` when nothing was stored."""
    return None if artifact is None else artifact.id


async def _tool_unavailable_reason(tool: Tool, context: ToolContext) -> str | None:
    """Return why ``tool`` cannot run in this environment, or ``None`` when it can.

    Only command tools can be probed: their absence is visible through
    :meth:`scout_sandbox.Sandbox.which`. A tool that cannot be probed is assumed runnable,
    because guessing "unavailable" would silently drop a check, whereas the tool's own
    result reports an infrastructure failure if it truly cannot start.
    """
    executable = _probe_executable(tool, context)
    if executable is None or await _resolves_in_sandbox(context, executable):
        return None
    return (
        f"the {executable!r} executable is not available in this environment, so this check "
        "could not run"
    )


def _probe_executable(tool: Tool, context: ToolContext) -> str | None:
    """Return the executable worth probing for ``tool``, or ``None`` when there is none.

    A path-like executable is not probed: :meth:`scout_sandbox.Sandbox.which` validates the name it
    is given (no paths, no shell metacharacters), and a path is better left to the tool's own
    launch-error reporting than rejected here.
    """
    if not isinstance(tool, CommandTool):
        return None
    try:
        argv = list(tool.build_argv(context))
    except Exception:
        return None
    if not argv:
        return None
    executable = argv[0]
    if "/" in executable or "\\" in executable:
        return None
    return executable


async def _resolves_in_sandbox(context: ToolContext, executable: str) -> bool:
    """Whether the sandbox can resolve ``executable``, treating an unprobeable name as present."""
    try:
        return await context.sandbox.which(executable) is not None
    except (ValueError, SandboxError):
        return True


# --------------------------------------------------------------------------------------
# Interpretation
# --------------------------------------------------------------------------------------


def _findings_from_outcome(
    outcome: ToolOutcome,
    *,
    category: FindingCategory,
    tool_result: ToolResult,
    job_id: str,
    repository: str,
) -> list[Finding]:
    """Convert a tool's parsed findings into domain findings with machine evidence."""
    return [
        _finding_from_parsed(
            parsed,
            category=category,
            tool_result=tool_result,
            job_id=job_id,
            repository=repository,
        )
        for parsed in outcome.findings
    ]


def _finding_from_parsed(
    parsed: ParsedFinding,
    *,
    category: FindingCategory,
    tool_result: ToolResult,
    job_id: str,
    repository: str,
) -> Finding:
    """Build one domain finding from one parsed tool finding.

    ``confidence=1.0`` is legitimate here and nowhere else: the evidence is
    ``EvidenceKind.TOOL_RUN``, which is ``MACHINE`` strength, so the domain's confidence
    ceiling is exactly 1.0 and the finding records a deterministic observation rather than
    a claim.
    """
    file_path = _bounded(parsed.file, _MAX_FILE)
    line_start, line_end = _normalise_lines(parsed.line_start, parsed.line_end)
    excerpt = _bounded(parsed.description, _MAX_EXCERPT)
    evidence = EvidenceReference(
        kind=EvidenceKind.TOOL_RUN,
        reference_id=tool_result.id,
        summary=_bounded(
            f"{tool_result.tool} reported this while running {tool_result.command}",
            _MAX_SUMMARY,
        )
        or f"{tool_result.tool} execution record",
        file_path=file_path,
        line_start=line_start,
        line_end=line_end,
        excerpt=excerpt,
        artifact_id=tool_result.stdout_artifact_id,
    )
    title = _bounded(parsed.title, _MAX_TITLE) or f"{tool_result.tool} finding"
    return Finding(
        id=new_id(IdKind.FINDING),
        job_id=job_id,
        severity=parsed.severity,
        category=category,
        title=title,
        description=parsed.description or title,
        repository=repository,
        file=file_path,
        line_start=line_start,
        line_end=line_end,
        evidence=[evidence],
        recommendation=parsed.recommendation[:2000],
        confidence=1.0,
        origin=FindingOrigin.TOOL,
        source=tool_result.tool[:100],
        source_run_id=tool_result.id,
    )


def section_for_tool_result(
    *,
    stage_name: str,
    result: ToolResult,
    section_title: str = "Deterministic checks",
) -> ReportSection:
    """Render one tool result as a report section.

    The section's status is the tool's own status translated faithfully, so a reader can tell
    "the scanner looked and found nothing" (``PASSED``) from "the scanner never completed"
    (``UNVERIFIED``) without reading the narrative.

    Public and context-free on purpose: the orchestrator re-derives report sections from
    persisted tool-run rows when it builds the report, which is what makes report generation
    work after a resume.
    """
    evidence = [
        EvidenceReference(
            kind=EvidenceKind.TOOL_RUN,
            reference_id=result.id,
            summary=_bounded(f"{result.tool} execution record", _MAX_SUMMARY)
            or f"{result.tool} execution record",
            artifact_id=result.stdout_artifact_id,
        )
    ]
    stage_key = stage_name or "tooling"
    key = f"{stage_key}.{result.tool}"
    title = f"{section_title}: {result.tool}"
    if result.infrastructure_failure or result.status in {ToolStatus.ERROR, ToolStatus.TIMEOUT}:
        reason = _infrastructure_reason(result)
        return _section(
            key=key,
            title=title,
            status=ReportSectionStatus.UNVERIFIED,
            summary=result.narrative or reason,
            reason=reason,
            evidence=evidence,
        )
    if result.status is ToolStatus.FAILED:
        return _section(
            key=key,
            title=title,
            status=ReportSectionStatus.FAILED,
            summary=result.narrative or f"{result.tool} reported problems.",
            evidence=evidence,
        )
    if result.status in {ToolStatus.SKIPPED, ToolStatus.NOT_APPLICABLE}:
        reason = result.narrative or f"{result.tool} had nothing to check."
        return _section(
            key=key,
            title=title,
            status=ReportSectionStatus.SKIPPED,
            summary=reason,
            reason=reason,
            evidence=evidence,
        )
    return _section(
        key=key,
        title=title,
        status=ReportSectionStatus.PASSED,
        summary=result.narrative or f"{result.tool} reported no problems.",
        evidence=evidence,
    )


def _section(
    *,
    key: str,
    title: str,
    status: ReportSectionStatus,
    summary: str,
    reason: str | None = None,
    evidence: Sequence[EvidenceReference] = (),
) -> ReportSection:
    """Build a report section, always supplying the reason a non-passing status requires."""
    return ReportSection(
        key=key[:100],
        title=title[:200],
        status=status,
        summary=summary[:8000],
        evidence=list(evidence),
        unverified_reason=None if reason is None else reason[:2000],
    )


def _infrastructure_reason(result: ToolResult) -> str:
    """Render why a tool result is not evidence about the change."""
    detail = result.error or result.narrative or f"{result.tool} produced no result"
    return _bounded(f"{result.tool} failed for infrastructure reasons: {detail}", 2000) or (
        f"{result.tool} failed for infrastructure reasons"
    )


def _command_overrides(config: object) -> dict[str, tuple[str, ...]]:
    """Project the repository's declared commands into the tool layer's override map."""
    overrides: dict[str, tuple[str, ...]] = {}
    commands = getattr(config, "commands", None)
    if commands is None:
        return overrides
    for name in ("install", "test", "lint", "format", "typecheck", "build"):
        spec = getattr(commands, name, None)
        if spec is not None:
            overrides[name] = tuple(spec.argv)
    custom = getattr(commands, "custom", {}) or {}
    for name, spec in custom.items():
        overrides[f"custom.{name}"] = tuple(spec.argv)
    return overrides


def _split_paths(value: object) -> list[str]:
    """Split a comma-joined path list stored in a tool summary."""
    if not isinstance(value, str) or not value.strip():
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _bounded(value: str | None, limit: int) -> str | None:
    """Trim ``value`` to ``limit`` characters, preserving ``None`` and empty strings."""
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if len(stripped) <= limit:
        return stripped
    return stripped[: limit - 1].rstrip() + "\u2026"


def _normalise_lines(start: int | None, end: int | None) -> tuple[int | None, int | None]:
    """Coerce a tool's line range into something the finding validator accepts.

    A tool that reports an end without a start, or an inverted range, has a bug; the correct
    response is to drop the range and keep the finding rather than to fail a job over
    presentation metadata.
    """
    if start is None or start < 1:
        return None, None
    if end is None or end < start:
        return start, start
    return start, end
