"""``pytest``: the primary deterministic test runner (spec section 65).

This is the most consequential tool in the set, because "do the tests pass?" is the
question the report is ultimately answering. Two design choices matter:

* **Structured over scraped.** The run is asked for a JUnit XML report
  (``--junit-xml``) and that report is parsed with :mod:`xml.etree.ElementTree`. Console
  text is only used as a fallback when no report was produced, because console output
  changes between pytest versions and localisation settings while the XML schema does
  not.
* **Exit codes are a documented contract, not a guess.** pytest exits ``0`` when every
  collected test passed, ``1`` when tests ran and some failed, ``2`` when collection
  failed, ``4`` on a usage error, and ``5`` when no tests were collected. Scout honours
  that contract: ``1`` is a real finding about the change; ``2`` and ``4`` are
  infrastructure failures; ``5`` is a skip with a narrative, never a failure.

The report is written inside the workspace (``.scout/reports/junit.xml``), so it is
captured as an artifact and never lands on the host. ``-p no:cacheprovider`` keeps
pytest from writing ``.pytest_cache`` into a checkout Scout does not own.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from scout_domain.enums import Capability, Severity, ToolStatus
from scout_domain.models.artifact import ArtifactKind
from scout_domain.models.tool import ToolSummary
from scout_tools.base import CommandTool, ParsedFinding, ToolCategory, ToolOutcome
from scout_tools.tools._common import (
    SourceCache,
    as_float,
    as_int,
    cap_findings,
    ensure_report_directory,
    excerpt,
    launch_failure,
    read_workspace_text,
    relative_workspace_path,
    report_path,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from scout_sandbox.command import CommandOutcome
    from scout_tools.base import ToolContext

__all__ = ["PytestTool"]

# -- pytest's documented exit codes -------------------------------------------------
_EXIT_OK: Final[int] = 0
_EXIT_TESTS_FAILED: Final[int] = 1
_EXIT_INTERRUPTED: Final[int] = 2
_EXIT_INTERNAL_ERROR: Final[int] = 3
_EXIT_USAGE_ERROR: Final[int] = 4
_EXIT_NO_TESTS: Final[int] = 5

_ERROR_EXIT_CODES: Final[frozenset[int]] = frozenset(
    {_EXIT_INTERRUPTED, _EXIT_INTERNAL_ERROR, _EXIT_USAGE_ERROR}
)

_REPORT_FILENAME: Final[str] = "junit.xml"

#: ``1 failed, 2 passed, 1 skipped in 0.12s`` — pytest's console summary line.
_COUNT_TOKEN: Final[re.Pattern[str]] = re.compile(
    r"(\d+)\s+(passed|failed|error|errors|skipped|xfailed|xpassed|deselected|warning|warnings)\b"
)

#: ``FAILED tests/test_x.py::test_y - AssertionError: assert 1 == 2``
_SHORT_SUMMARY: Final[re.Pattern[str]] = re.compile(
    r"^(?P<kind>FAILED|ERROR)\s+(?P<nodeid>\S+?)(?::(?P<line>\d+))?(?:\s+-\s+(?P<message>.*))?$"
)


@dataclass(frozen=True, slots=True)
class _JUnitProblem:
    """One failed or errored test case, extracted from the JUnit report."""

    name: str
    classname: str
    file: str | None
    line: int | None
    kind: str
    message: str
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class _JUnitReport:
    passed: int
    failed: int
    errored: int
    skipped: int
    total: int
    duration_seconds: float
    problems: tuple[_JUnitProblem, ...]


class PytestTool(CommandTool):
    """Run the repository's Python test suite and interpret the result."""

    name = "pytest"
    capability = Capability.TEST_RUN
    category = ToolCategory.TEST
    description = (
        "Runs pytest with a JUnit XML report inside the sandbox and converts the report "
        "into passed/failed/errored counts plus one finding per failing test."
    )
    version_argv: tuple[str, ...] = ("pytest", "--version")
    ecosystems = frozenset({"python", "pytest", "unittest"})
    marker_files = frozenset(
        {"pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini", "conftest.py", "tests"}
    )
    finding_exit_codes = frozenset({_EXIT_TESTS_FAILED})

    @property
    def report_file(self) -> str:
        """Workspace-relative location of the JUnit report this tool asks pytest for."""
        return report_path(_REPORT_FILENAME)

    # -- Command ---------------------------------------------------------------------

    def build_argv(self, context: ToolContext) -> Sequence[str]:
        ensure_report_directory(context)
        override = context.override("test")
        if override is not None and not _invokes_pytest(override):
            # A repository-declared command that is not pytest (``make test``, a custom
            # runner) is executed verbatim. Scout cannot ask it for a JUnit report, and
            # interpret() says so rather than pretending otherwise.
            return list(override)

        argv: list[str] = list(override) if override is not None else ["pytest"]
        if not any(part in {"-q", "--quiet"} for part in argv):
            argv.append("-q")
        if _report_location(argv) is None:
            argv.append(f"--junit-xml={self.report_file}")
        if "-p" not in argv:
            argv.extend(["-p", "no:cacheprovider"])
        return argv

    # -- Interpretation --------------------------------------------------------------

    def interpret(self, outcome: CommandOutcome, context: ToolContext) -> ToolOutcome:
        broken = launch_failure(self.name, outcome)
        if broken is not None:
            return broken

        exit_code = outcome.exit_code
        assert exit_code is not None  # launch_failure() guarantees an exit code
        location = _report_location(outcome.argv) or self.report_file
        report_text = read_workspace_text(context, location)
        stored_location = location if report_text is not None else None

        if report_text is not None:
            report = self._parse_report(report_text, context)
            if report is not None:
                return self._from_report(
                    report,
                    exit_code=exit_code,
                    location=stored_location,
                )
            note = "its JUnit report could not be parsed"
        else:
            note = "no JUnit report was produced"

        return self._without_report(outcome, context, exit_code=exit_code, note=note)

    # -- JUnit parsing ---------------------------------------------------------------

    @staticmethod
    def _parse_report(text: str, context: ToolContext) -> _JUnitReport | None:
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            return None
        if _local_name(root.tag) not in {"testsuites", "testsuite"}:
            return None

        passed = failed = errored = skipped = 0
        duration = 0.0
        problems: list[_JUnitProblem] = []

        for case in root.iter("testcase"):
            duration += as_float(case.get("time"), 0.0)
            error = case.find("error")
            failure = case.find("failure")
            if error is not None:
                errored += 1
                problems.append(_problem_from_case(case, error, "error", context))
            elif failure is not None:
                failed += 1
                problems.append(_problem_from_case(case, failure, "failure", context))
            elif case.find("skipped") is not None:
                skipped += 1
            else:
                passed += 1

        if duration == 0.0:
            duration = _suite_duration(root)

        return _JUnitReport(
            passed=passed,
            failed=failed,
            errored=errored,
            skipped=skipped,
            total=passed + failed + errored + skipped,
            duration_seconds=duration,
            problems=tuple(problems),
        )

    def _from_report(
        self,
        report: _JUnitReport,
        *,
        exit_code: int,
        location: str | None,
    ) -> ToolOutcome:
        summary = _summary(report)
        problems = report.failed + report.errored

        if problems > 0 and exit_code in {_EXIT_OK, _EXIT_TESTS_FAILED}:
            findings, dropped = cap_findings(
                [_finding_from_problem(problem) for problem in report.problems]
            )
            narrative = _counts_narrative(report, action="failed")
            if dropped:
                narrative += (
                    f" Only the first {len(findings)} failures are listed; the full JUnit "
                    "report is attached as an artifact."
                )
            return ToolOutcome(
                status=ToolStatus.FAILED,
                summary=summary,
                narrative=narrative,
                findings=findings,
                report_path=location,
                report_kind=ArtifactKind.TEST_REPORT,
                report_content_type="application/xml",
            )

        if exit_code == _EXIT_NO_TESTS or (report.total == 0 and exit_code == _EXIT_OK):
            return ToolOutcome(
                status=ToolStatus.SKIPPED,
                summary=summary,
                narrative=(
                    "pytest collected no tests, so there was nothing to verify. This is "
                    "recorded as a skip, not a failure."
                ),
                report_path=location,
                report_kind=ArtifactKind.TEST_REPORT,
                report_content_type="application/xml",
            )

        if exit_code == _EXIT_OK:
            return ToolOutcome(
                status=ToolStatus.PASSED,
                summary=summary,
                narrative=_counts_narrative(report, action="passed"),
                report_path=location,
                report_kind=ArtifactKind.TEST_REPORT,
                report_content_type="application/xml",
            )

        detail = (
            f"pytest exited with status {exit_code} but its JUnit report records "
            f"{problems} failures/errors across {report.total} tests"
        )
        return ToolOutcome(
            status=ToolStatus.ERROR,
            summary=summary,
            narrative=(f"{detail}; Scout reports an infrastructure failure rather than a finding."),
            error=detail,
            infrastructure_failure=True,
            report_path=location,
            report_kind=ArtifactKind.TEST_REPORT,
            report_content_type="application/xml",
        )

    def _without_report(
        self,
        outcome: CommandOutcome,
        context: ToolContext,
        *,
        exit_code: int,
        note: str,
    ) -> ToolOutcome:
        console = outcome.read_stdout_tail(limit_bytes=40_000) or outcome.read_stderr_tail(
            limit_bytes=40_000
        )
        counts = _counts_from_console(console)
        summary = ToolSummary(values=counts)

        if exit_code == _EXIT_OK:
            return ToolOutcome(
                status=ToolStatus.PASSED,
                summary=summary,
                narrative=(
                    f"pytest exited 0 ({note}); Scout reports a pass, though per-test "
                    "counts are only as complete as the captured console summary."
                ),
            )

        if exit_code == _EXIT_NO_TESTS:
            return ToolOutcome(
                status=ToolStatus.SKIPPED,
                summary=summary,
                narrative=(
                    f"pytest collected no tests (exit code 5; {note}). Recorded as a skip, "
                    "not a failure."
                ),
            )

        if exit_code == _EXIT_TESTS_FAILED:
            source = SourceCache(context)
            findings, dropped = cap_findings(_findings_from_console(console, source))
            total = sum(as_int(counts.get(key), 0) for key in ("failed", "errored"))
            narrative = (
                f"pytest exited 1: the test suite failed ({note}). "
                f"{total} failing test(s) are reported in the console summary."
                if findings
                else (
                    f"pytest exited 1: the test suite failed ({note}). Scout could not "
                    "recover individual test names from the captured output."
                )
            )
            if dropped:
                narrative += f" Only the first {len(findings)} failures are listed."
            return ToolOutcome(
                status=ToolStatus.FAILED,
                summary=summary,
                narrative=narrative,
                findings=findings,
            )

        if exit_code in _ERROR_EXIT_CODES:
            return _collection_error(self.name, outcome, exit_code=exit_code, note=note)

        return _unexpected_exit(self.name, exit_code=exit_code, note=note)


# -- module-level helpers ------------------------------------------------------------


def _local_name(tag: object) -> str:
    """Return an element tag's local name, tolerating XML namespaces."""
    text = str(tag)
    return text.rsplit("}", 1)[-1]


def _invokes_pytest(argv: Sequence[str]) -> bool:
    """Whether an argument vector looks like a pytest invocation."""
    if not argv:
        return False
    executable = argv[0].rsplit("/", 1)[-1]
    if executable.startswith("pytest"):
        return True
    module_flag_args = 3
    return (
        len(argv) >= module_flag_args
        and executable.startswith("python")
        and list(argv[1:3]) == ["-m", "pytest"]
    )


def _report_location(argv: Sequence[str]) -> str | None:
    """Extract the ``--junit-xml`` target from an executed argument vector."""
    parts = list(argv)
    for index, part in enumerate(parts):
        for prefix in ("--junit-xml=", "--junitxml="):
            if part.startswith(prefix):
                value = part[len(prefix) :].strip()
                if value:
                    return value
        if part in {"--junit-xml", "--junitxml"} and index + 1 < len(parts):
            return parts[index + 1]
    return None


def _suite_duration(root: ET.Element) -> float:
    """Sum the ``time`` attribute of the top-level test suites."""
    suites = [root] if _local_name(root.tag) == "testsuite" else list(root)
    return sum(as_float(suite.get("time"), 0.0) for suite in suites)


def _path_from_classname(classname: str) -> str | None:
    """Best-effort file path from a JUnit ``classname`` such as ``tests.test_api``."""
    if not classname:
        return None
    parts = [part for part in classname.split(".") if part]
    if not parts:
        return None
    # pytest builds the classname from the module path; a trailing capitalised component
    # is a test class, not a module.
    if parts[-1][:1].isupper():
        parts = parts[:-1]
    if not parts:
        return None
    return "/".join(parts) + ".py"


def _problem_from_case(
    case: ET.Element,
    element: ET.Element,
    kind: str,
    context: ToolContext,
) -> _JUnitProblem:
    file_attr = element.get("file") or case.get("file")
    line_attr = element.get("line") or case.get("line")
    message = element.get("message") or ""
    if not message:
        message = (element.text or "").strip()
    return _JUnitProblem(
        name=case.get("name") or "<unnamed>",
        classname=case.get("classname") or "",
        file=relative_workspace_path(context, file_attr)
        if file_attr
        else _path_from_classname(case.get("classname") or ""),
        line=as_int(line_attr, 0) or None,
        kind=kind,
        message=message,
        duration_seconds=as_float(case.get("time"), 0.0),
    )


def _summary(report: _JUnitReport) -> ToolSummary:
    return ToolSummary(
        values={
            "passed": report.passed,
            "failed": report.failed,
            "errored": report.errored,
            "skipped": report.skipped,
            "total": report.total,
            "duration_seconds": round(report.duration_seconds, 3),
        }
    )


def _counts_narrative(report: _JUnitReport, *, action: str) -> str:
    return (
        f"pytest ran {report.total} test(s): {report.passed} passed, {report.failed} failed, "
        f"{report.errored} errored, {report.skipped} skipped in "
        f"{report.duration_seconds:.2f}s; the suite {action}."
    )


def _finding_from_problem(problem: _JUnitProblem) -> ParsedFinding:
    node = f"{problem.classname}::{problem.name}" if problem.classname else problem.name
    headline = excerpt(problem.message.splitlines()[0] if problem.message else "", limit=160)
    title = f"test failed: {node}" if problem.kind == "failure" else f"test errored: {node}"
    description = excerpt(problem.message, limit=600) or (
        "the test reported a failure without a message"
    )
    return ParsedFinding(
        severity=Severity.HIGH,
        title=title,
        description=description,
        rule_id=None,
        file=problem.file,
        line_start=problem.line,
        line_end=problem.line,
        recommendation=(
            "Fix the failing assertion or the behaviour it exercises; a failing test in the "
            "changed revision is a correctness signal."
        ),
        metadata={
            "kind": problem.kind,
            "test": node,
            "classname": problem.classname,
            "duration_seconds": round(problem.duration_seconds, 4),
            "message": headline,
        },
    )


def _counts_from_console(console: str) -> dict[str, int | float | str | bool | None]:
    """Recover pytest's console counts when no JUnit report exists."""
    tallies = {"passed": 0, "failed": 0, "errored": 0, "skipped": 0}
    for line in console.splitlines():
        if " in " not in line and "=" not in line:
            continue
        for match in _COUNT_TOKEN.finditer(line):
            number = int(match.group(1))
            label = match.group(2)
            if label == "passed":
                tallies["passed"] += number
            elif label == "failed":
                tallies["failed"] += number
            elif label in {"error", "errors"}:
                tallies["errored"] += number
            elif label == "skipped":
                tallies["skipped"] += number
    counts: dict[str, int | float | str | bool | None] = {}
    for key, value in tallies.items():
        counts[key] = value
    counts["total"] = sum(tallies.values())
    return counts


def _findings_from_console(console: str, source: SourceCache) -> list[ParsedFinding]:
    """Recover individual failures from pytest's ``-q`` short summary."""
    findings: list[ParsedFinding] = []
    seen: set[str] = set()
    for line in console.splitlines():
        match = _SHORT_SUMMARY.match(line.strip())
        if match is None:
            continue
        nodeid = match.group("nodeid")
        if nodeid in seen:
            continue
        seen.add(nodeid)
        file_part, _, _ = nodeid.partition("::")
        line_number = as_int(match.group("line"), 0) or None
        kind = match.group("kind").lower()
        message = match.group("message") or ""
        findings.append(
            ParsedFinding(
                severity=Severity.HIGH,
                title=f"test {'errored' if kind == 'error' else 'failed'}: {nodeid}",
                description=excerpt(message, limit=600)
                or "pytest reported this test as failing without a message",
                file=file_part or None,
                line_start=line_number,
                line_end=line_number,
                recommendation="Reproduce the failure locally with the same test id and fix it.",
                metadata={
                    "kind": "error" if kind == "error" else "failure",
                    "test": nodeid,
                    "excerpt": source.line(file_part or None, line_number),
                },
            )
        )
    return findings


def _collection_error(
    tool_name: str,
    outcome: CommandOutcome,
    *,
    exit_code: int,
    note: str,
) -> ToolOutcome:
    reason = {
        _EXIT_INTERRUPTED: "collection was interrupted or could not complete",
        _EXIT_INTERNAL_ERROR: "pytest hit an internal error",
        _EXIT_USAGE_ERROR: "pytest was invoked with an invalid option",
    }.get(exit_code, f"pytest exited with status {exit_code}")
    stderr_tail = excerpt(outcome.read_stderr_tail(limit_bytes=4000), limit=400)
    detail = f"{reason} (exit code {exit_code}; {note})"
    if stderr_tail:
        detail = f"{detail}: {stderr_tail}"
    return ToolOutcome(
        status=ToolStatus.ERROR,
        narrative=(
            f"{tool_name} could not produce a trustworthy test result: {reason} "
            f"(exit code {exit_code}; {note}). This is recorded as an infrastructure "
            "failure, not a failing test."
        ),
        error=detail[:4000],
        infrastructure_failure=True,
    )


def _unexpected_exit(
    tool_name: str,
    *,
    exit_code: int,
    note: str,
) -> ToolOutcome:
    return ToolOutcome(
        status=ToolStatus.ERROR,
        narrative=(
            f"{tool_name} exited with status {exit_code} ({note}), which is not one of "
            "pytest's documented outcomes; Scout reports an infrastructure failure."
        ),
        error=f"unexpected exit status {exit_code}",
        infrastructure_failure=True,
    )
