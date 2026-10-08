"""Coverage: what the test suite actually exercised (spec section 65).

Coverage is a *measurement*, not a gate. Scout uses it for one specific question: of the
files this pull request changed, which ones are only partly executed by the tests? A
global percentage hides that; the intersection of "below 100%" with "changed in this pull
request" is the actionable set, which is why :attr:`ToolContext.changed_paths` is part of
the interpretation rather than a report-only concern.

The report is the JSON emitted by ``coverage json`` (the same file ``pytest
--cov-report=json`` writes). ``coverage.json`` is preferred over prose because it carries
per-file ``missing_lines``, which is what lets a reviewer jump straight to the untested
branch. When the repository has already produced ``coverage.json`` upstream (for example
via ``pytest --cov-report=json``), that file is used directly instead of re-running
coverage.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from scout_domain.enums import Capability, Severity, ToolStatus
from scout_domain.models.artifact import ArtifactKind
from scout_domain.models.tool import ToolSummary
from scout_tools.base import CommandTool, ParsedFinding, ToolCategory, ToolOutcome
from scout_tools.tools._common import (
    as_float,
    as_int,
    as_mapping,
    cap_findings,
    declares_any,
    ensure_report_directory,
    launch_failure,
    nonzero_without_evidence,
    parse_json,
    read_workspace_text,
    report_path,
    skipped_outcome,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from scout_sandbox.command import CommandOutcome
    from scout_tools.base import ToolContext

__all__ = ["CoverageTool"]

_EXIT_OK: Final[int] = 0
_FULL_COVERAGE: Final[float] = 100.0
_REPORT_FILENAME: Final[str] = "coverage.json"
#: Files whose presence or content means the repository measures coverage.
_DECLARATION_FILES: Final[tuple[str, ...]] = (
    "pyproject.toml",
    "setup.cfg",
    "tox.ini",
    ".coveragerc",
)
_DECLARATION_NEEDLES: Final[tuple[str, ...]] = ("[tool.coverage", "[coverage:", "[coverage]")
#: The largest number of missing lines stored per finding before the list is summarised.
_MAX_LISTED_LINES: Final[int] = 40
#: The largest number of changed files named in the narrative before it is summarised.
_MAX_LISTED_FILES: Final[int] = 5


@dataclass(frozen=True, slots=True)
class _CoverageFile:
    path: str
    percent_covered: float
    covered_lines: int
    num_statements: int
    missing_lines: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class _CoverageReport:
    percent_covered: float
    covered_statements: int
    total_statements: int
    files: tuple[_CoverageFile, ...]

    @property
    def incomplete_files(self) -> tuple[_CoverageFile, ...]:
        return tuple(entry for entry in self.files if entry.percent_covered < _FULL_COVERAGE)


class CoverageTool(CommandTool):
    """Regenerate or read ``coverage.json`` and report on the changed files."""

    name = "coverage"
    capability = Capability.TEST_RUN
    category = ToolCategory.COVERAGE
    description = (
        "Reads (or regenerates) coverage.json and reports the coverage percentage plus "
        "every changed file that the tests do not fully execute."
    )
    version_argv: tuple[str, ...] = ("coverage", "--version")
    ecosystems = frozenset({"python", "coverage"})
    marker_files = frozenset({".coveragerc", ".coverage", "coverage.json", "setup.cfg"})

    @property
    def report_file(self) -> str:
        """Workspace-relative location this tool asks ``coverage json`` to write."""
        return report_path(_REPORT_FILENAME)

    # -- Applicability ---------------------------------------------------------------

    def applicability_reason(self, context: ToolContext) -> str | None:
        if not self.enabled:
            return "tool is disabled by configuration"
        # ``[tool.coverage]`` in pyproject.toml and ``[coverage:...]`` in setup.cfg are
        # the two common ways to declare coverage without a dedicated file.
        if declares_any(context, _DECLARATION_FILES, _DECLARATION_NEEDLES):
            return None
        return super().applicability_reason(context)

    # -- Command ---------------------------------------------------------------------

    def build_argv(self, context: ToolContext) -> Sequence[str]:
        ensure_report_directory(context)
        return ["coverage", "json", "-o", self.report_file]

    # -- Interpretation --------------------------------------------------------------

    def interpret(self, outcome: CommandOutcome, context: ToolContext) -> ToolOutcome:
        location, text = self._locate_report(context)
        if text is not None:
            return self._from_existing_report(outcome, context, text, location=location)

        broken = launch_failure(self.name, outcome)
        if broken is not None:
            return broken

        exit_code = outcome.exit_code
        assert exit_code is not None  # launch_failure() guarantees an exit code
        if exit_code == _EXIT_OK:
            return ToolOutcome(
                status=ToolStatus.ERROR,
                narrative=(
                    f"{self.name} exited 0 but wrote no coverage report, so the measurement "
                    "cannot be trusted."
                ),
                error="coverage reported success without producing a report",
                infrastructure_failure=True,
            )

        stderr = outcome.read_stderr(limit_bytes=8000)
        stdout = outcome.read_stdout(limit_bytes=8000)
        combined = f"{stderr}\n{stdout}"
        if "no data to report" in combined.lower():
            return skipped_outcome(
                self.name,
                "there was no ``.coverage`` data to report, so no coverage measurement "
                "exists for this revision.",
            )
        return nonzero_without_evidence(self.name, outcome, what="coverage.json report")

    def _from_existing_report(
        self,
        outcome: CommandOutcome,
        context: ToolContext,
        text: str,
        *,
        location: str | None,
    ) -> ToolOutcome:
        report = _parse_report(text)
        if report is not None:
            return self._interpreted(context, report, location=location)
        if outcome.launch_error is not None or outcome.exit_code not in (None, _EXIT_OK):
            return nonzero_without_evidence(self.name, outcome, what="coverage.json report")
        return ToolOutcome(
            status=ToolStatus.ERROR,
            narrative=(
                "a coverage.json file was found but Scout could not parse it; the raw "
                "bytes are retained as an artifact."
            ),
            error="coverage.json could not be parsed",
            infrastructure_failure=True,
        )

    def _locate_report(self, context: ToolContext) -> tuple[str | None, str | None]:
        """Return ``(workspace_relative_path, text)`` for the best available report."""
        for candidate in (self.report_file, "coverage.json"):
            text = read_workspace_text(context, candidate)
            if text is not None:
                return candidate, text
        return None, None

    def _interpreted(
        self,
        context: ToolContext,
        report: _CoverageReport,
        *,
        location: str | None,
    ) -> ToolOutcome:
        uncovered = self._uncovered_changed_paths(context, report)
        summary = ToolSummary(
            values={
                "percent_covered": round(report.percent_covered, 2),
                "covered_statements": report.covered_statements,
                "total_statements": report.total_statements,
                "files_total": len(report.files),
                "files_below_100": len(report.incomplete_files),
                "uncovered_changed_paths": ",".join(uncovered),
                "uncovered_changed_paths_count": len(uncovered),
            }
        )

        findings: tuple[ParsedFinding, ...] = ()
        dropped = 0
        if uncovered:
            matched = _match_changed_paths(report, uncovered)
            findings, dropped = cap_findings(
                [_finding_for(changed, entry, report) for changed, entry in matched]
            )

        narrative = (
            f"coverage reports {report.percent_covered:.2f}% of "
            f"{report.total_statements} statements covered"
        )
        if uncovered:
            listed = ", ".join(uncovered[:_MAX_LISTED_FILES])
            more = (
                ""
                if len(uncovered) <= _MAX_LISTED_FILES
                else f" and {len(uncovered) - _MAX_LISTED_FILES} more"
            )
            narrative += (
                f"; {len(uncovered)} changed file(s) are not fully covered: {listed}{more}."
            )
        else:
            narrative += (
                "; every file changed by this pull request is fully covered by the tests."
                if context.changed_paths
                else "; no changed paths were supplied, so this is a repository-wide figure."
            )
        if dropped:
            narrative += f" Only the first {len(findings)} files are listed individually."

        return ToolOutcome(
            status=ToolStatus.FAILED if uncovered else ToolStatus.PASSED,
            summary=summary,
            narrative=narrative,
            findings=findings,
            report_path=location,
            report_kind=ArtifactKind.TOOL_REPORT,
            report_content_type="application/json",
        )

    @staticmethod
    def _uncovered_changed_paths(context: ToolContext, report: _CoverageReport) -> tuple[str, ...]:
        """Intersect files below 100% with the paths this pull request changed."""
        if not context.changed_paths:
            return ()
        matched: dict[str, None] = {}
        for entry in report.incomplete_files:
            for changed in context.changed_paths:
                if _paths_match(entry.path, changed):
                    matched.setdefault(changed, None)
        return tuple(sorted(matched))


def _parse_report(text: str) -> _CoverageReport | None:
    value, error = parse_json(text)
    if error is not None:
        return None
    root = as_mapping(value)
    if "files" not in root and "totals" not in root:
        return None

    totals = as_mapping(root.get("totals"))
    files: list[_CoverageFile] = []
    for raw_path, raw_entry in as_mapping(root.get("files")).items():
        entry = as_mapping(raw_entry)
        summary = as_mapping(entry.get("summary"))
        missing = tuple(
            number
            for number in (as_int(item, -1) for item in _as_list(entry.get("missing_lines")))
            if number > 0
        )
        files.append(
            _CoverageFile(
                path=raw_path,
                percent_covered=as_float(summary.get("percent_covered"), 0.0),
                covered_lines=as_int(summary.get("covered_lines"), 0),
                num_statements=as_int(summary.get("num_statements"), 0),
                missing_lines=missing,
            )
        )

    if not totals and not files:
        return None

    percent = as_float(totals.get("percent_covered"), 0.0)
    if not totals and files:
        statements = sum(entry.num_statements for entry in files)
        covered = sum(entry.covered_lines for entry in files)
        percent = (covered / statements * _FULL_COVERAGE) if statements else 0.0
    return _CoverageReport(
        percent_covered=percent,
        covered_statements=as_int(totals.get("covered_lines"), 0),
        total_statements=as_int(totals.get("num_statements"), 0),
        files=tuple(files),
    )


def _as_list(value: object) -> list[object]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _paths_match(coverage_path: str, changed_path: str) -> bool:
    """Whether a coverage key denotes the same file as a changed path."""
    left = coverage_path.replace("\\", "/").removeprefix("./").lstrip("/")
    right = changed_path.replace("\\", "/").removeprefix("./").lstrip("/")
    if left == right:
        return True
    return left.endswith("/" + right) or right.endswith("/" + left)


def _match_changed_paths(
    report: _CoverageReport, uncovered: Sequence[str]
) -> list[tuple[str, _CoverageFile]]:
    """Pair each uncovered changed path with the coverage entry that describes it."""
    selected: list[tuple[str, _CoverageFile]] = []
    for changed in uncovered:
        for entry in report.incomplete_files:
            if _paths_match(entry.path, changed):
                selected.append((changed, entry))
                break
    return selected


def _finding_for(changed_path: str, entry: _CoverageFile, report: _CoverageReport) -> ParsedFinding:
    missing = entry.missing_lines[:_MAX_LISTED_LINES]
    rendered = ", ".join(str(number) for number in missing)
    if len(entry.missing_lines) > _MAX_LISTED_LINES:
        rendered += f", ... ({len(entry.missing_lines) - _MAX_LISTED_LINES} more)"
    return ParsedFinding(
        severity=Severity.LOW,
        title=f"changed file is {entry.percent_covered:.0f}% covered: {changed_path}",
        description=(
            f"{changed_path} is {entry.percent_covered:.1f}% covered "
            f"({entry.covered_lines}/{entry.num_statements} statements); the tests added or "
            f"changed by this pull request do not execute all of it. Repository total: "
            f"{report.percent_covered:.1f}%."
        ),
        rule_id="coverage.changed-file-incomplete",
        file=changed_path,
        line_start=min(missing) if missing else None,
        line_end=max(missing) if missing else None,
        recommendation=(
            "Add or extend a test that exercises the uncovered lines, or record why the "
            "branch is intentionally untested."
        ),
        metadata={
            "percent_covered": round(entry.percent_covered, 2),
            "covered_lines": entry.covered_lines,
            "num_statements": entry.num_statements,
            "missing_lines": rendered,
            "missing_line_count": len(entry.missing_lines),
            "coverage_path": entry.path,
        },
    )
