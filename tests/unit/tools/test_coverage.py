"""Unit tests for :mod:`scout_tools.tools.coverage`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from scout_domain.enums import ToolStatus
from scout_tools.tools.coverage import CoverageTool

if TYPE_CHECKING:
    from scout_sandbox import Workspace

REPORT_PATH = ".scout/reports/coverage.json"
DEFAULT_ARGV = ("coverage", "json", "-o", REPORT_PATH)


def test_build_argv_is_a_list(make_context) -> None:
    argv = CoverageTool().build_argv(make_context())

    assert not isinstance(argv, str)
    assert list(argv) == list(DEFAULT_ARGV)


def test_report_from_a_previous_step_is_used(
    make_context, make_outcome, workspace: Workspace, write_fixture
) -> None:
    """``pytest --cov-report=json`` may have written coverage.json before this runs."""
    write_fixture(workspace, "coverage.json", "coverage.json")
    context = make_context(
        changed_paths=("packages/app/service.py", "packages/app/util.py", "docs/readme.md")
    )
    outcome = make_outcome(exit_code=0, argv=DEFAULT_ARGV)

    result = CoverageTool().interpret(outcome, context)

    assert result.status is ToolStatus.FAILED
    assert result.infrastructure_failure is False
    assert result.summary.values["percent_covered"] == 63.16
    assert result.summary.values["files_below_100"] == 2
    assert result.summary.values["uncovered_changed_paths"] == "packages/app/service.py"
    assert len(result.findings) == 1

    finding = result.findings[0]
    assert finding.file == "packages/app/service.py"
    assert finding.metadata["missing_lines"] == "4, 9, 12"
    assert finding.metadata["percent_covered"] == 72.73


def test_fully_covered_change_passes(
    make_context, make_outcome, workspace: Workspace, write_fixture
) -> None:
    write_fixture(workspace, "coverage.json", "coverage.json")
    context = make_context(changed_paths=("packages/app/util.py",))

    result = CoverageTool().interpret(make_outcome(exit_code=0, argv=DEFAULT_ARGV), context)

    assert result.status is ToolStatus.PASSED
    assert result.findings == ()
    assert result.summary.values["uncovered_changed_paths"] == ""


def test_no_changed_paths_is_a_repository_wide_measurement(
    make_context, make_outcome, workspace: Workspace, write_fixture
) -> None:
    write_fixture(workspace, "coverage.json", "coverage.json")

    result = CoverageTool().interpret(make_outcome(exit_code=0, argv=DEFAULT_ARGV), make_context())

    assert result.status is ToolStatus.PASSED
    assert "no changed paths" in result.narrative


def test_no_coverage_data_is_a_skip(make_context, make_outcome) -> None:
    outcome = make_outcome(
        stdout="",
        stderr="No data to report.\n",
        exit_code=1,
        argv=DEFAULT_ARGV,
    )

    result = CoverageTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.SKIPPED
    assert result.infrastructure_failure is False


def test_broken_coverage_run_is_an_error(make_context, make_outcome) -> None:
    outcome = make_outcome(stderr="", exit_code=2, argv=DEFAULT_ARGV)

    result = CoverageTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True
    assert result.findings == ()


def test_truncated_report_is_an_error(make_context, make_outcome, workspace: Workspace) -> None:
    workspace.write_text("coverage.json", '{"files": {"a.py": ')

    result = CoverageTool().interpret(make_outcome(exit_code=0, argv=DEFAULT_ARGV), make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True


def test_launch_error_is_infrastructure(make_context, make_outcome) -> None:
    outcome = make_outcome(
        exit_code=None, launch_error="executable not found: coverage", argv=DEFAULT_ARGV
    )

    result = CoverageTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True


def test_applicability_from_pyproject_declaration(make_context) -> None:
    context = make_context(files={"pyproject.toml": "[tool.coverage.run]\nbranch = true\n"})

    assert CoverageTool().is_applicable(context) is True


def test_applicability_is_explained_when_absent(make_context) -> None:
    reason = CoverageTool().applicability_reason(make_context())

    assert reason is not None
    assert "marker files" in reason
