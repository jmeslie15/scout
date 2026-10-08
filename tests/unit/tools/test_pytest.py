"""Unit tests for :mod:`scout_tools.tools.pytest`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from scout_domain.enums import Severity, ToolStatus
from scout_tools.tools.pytest import PytestTool

if TYPE_CHECKING:
    from scout_sandbox import Workspace

JUNIT_PATH = ".scout/reports/junit.xml"
DEFAULT_ARGV = ("pytest", "-q", f"--junit-xml={JUNIT_PATH}", "-p", "no:cacheprovider")


def test_build_argv_requests_a_junit_report_and_no_cache(
    make_context,
) -> None:
    tool = PytestTool()
    argv = tool.build_argv(make_context())

    assert not isinstance(argv, str)
    assert list(argv) == list(DEFAULT_ARGV)


def test_build_argv_creates_the_report_directory(make_context, workspace: Workspace) -> None:
    """Some report writers do not create missing parent directories; Scout does."""
    assert workspace.exists(".scout/reports") is False

    PytestTool().build_argv(make_context())

    assert workspace.exists(".scout/reports") is True


def test_build_argv_honours_a_pytest_test_override(make_context) -> None:
    tool = PytestTool()
    context = make_context(command_overrides={"test": ("pytest", "-x", "--timeout=30")})

    argv = list(tool.build_argv(context))

    assert argv[:3] == ["pytest", "-x", "--timeout=30"]
    assert f"--junit-xml={JUNIT_PATH}" in argv
    assert "-p" in argv


def test_build_argv_runs_a_non_pytest_override_verbatim(make_context) -> None:
    tool = PytestTool()
    context = make_context(command_overrides={"test": ("make", "test")})

    assert list(tool.build_argv(context)) == ["make", "test"]


def test_passing_report(make_context, make_outcome, workspace: Workspace, write_fixture) -> None:
    write_fixture(workspace, "junit_pass.xml", JUNIT_PATH)
    outcome = make_outcome(stdout="3 passed in 0.32s\n", exit_code=0, argv=DEFAULT_ARGV)

    result = PytestTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.PASSED
    assert result.infrastructure_failure is False
    assert result.findings == ()
    assert result.summary.get_int("passed") == 3
    assert result.summary.get_int("total") == 3
    assert result.summary.get_int("failed") == 0
    assert "3 test(s)" in result.narrative
    assert result.report_path == JUNIT_PATH


def test_failing_report_produces_one_finding_per_problem(
    make_context, make_outcome, workspace: Workspace, write_fixture
) -> None:
    write_fixture(workspace, "junit_mixed.xml", JUNIT_PATH)
    outcome = make_outcome(
        stdout="1 failed, 1 passed, 1 error in 0.84s\n", exit_code=1, argv=DEFAULT_ARGV
    )

    result = PytestTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.FAILED
    assert result.infrastructure_failure is False
    assert len(result.findings) == 2
    assert result.summary.get_int("passed") == 1
    assert result.summary.get_int("failed") == 1
    assert result.summary.get_int("errored") == 1
    assert result.summary.get_int("skipped") == 1
    assert result.summary.get_int("total") == 4

    failure = next(item for item in result.findings if item.metadata["kind"] == "failure")
    assert failure.severity is Severity.HIGH
    assert failure.file == "tests/test_api.py"
    assert failure.line_start == 18
    assert "assert 422 == 201" in failure.description
    assert failure.metadata["test"] == "tests.test_api::test_create_user"

    error = next(item for item in result.findings if item.metadata["kind"] == "error")
    assert error.file == "tests/test_db.py"
    assert "database is unavailable" in error.description


def test_empty_report_is_skipped_not_failed(
    make_context, make_outcome, workspace: Workspace, write_fixture
) -> None:
    write_fixture(workspace, "junit_empty.xml", JUNIT_PATH)
    outcome = make_outcome(stdout="no tests ran in 0.01s\n", exit_code=5, argv=DEFAULT_ARGV)

    result = PytestTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.SKIPPED
    assert result.findings == ()
    assert result.summary.get_int("total") == 0
    assert "no tests" in result.narrative


def test_exit_two_is_an_infrastructure_error(make_context, make_outcome) -> None:
    """A collection failure says nothing about the code under test."""
    outcome = make_outcome(
        stdout="",
        stderr="ERROR: usage: pytest [options] [file_or_dir] [file_or_dir] [...]\n",
        exit_code=2,
        argv=DEFAULT_ARGV,
    )

    result = PytestTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True
    assert result.findings == ()
    assert result.error is not None


def test_exit_four_is_a_usage_error(make_context, make_outcome) -> None:
    outcome = make_outcome(
        stderr="error: unrecognized arguments: --nope\n", exit_code=4, argv=DEFAULT_ARGV
    )

    result = PytestTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True


def test_truncated_report_falls_back_to_the_console_summary(
    make_context, make_outcome, workspace: Workspace, write_fixture, load_fixture
) -> None:
    write_fixture(workspace, "junit_truncated.xml", JUNIT_PATH)
    outcome = make_outcome(
        stdout=load_fixture("pytest_console_failure.txt"),
        exit_code=1,
        argv=DEFAULT_ARGV,
    )

    result = PytestTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.FAILED
    assert result.infrastructure_failure is False
    assert len(result.findings) == 3
    assert {item.file for item in result.findings} == {
        "tests/test_api.py",
        "tests/test_db.py",
    }
    assert "could not be parsed" in result.narrative


def test_exit_one_without_a_report_still_reports_failure(
    make_context, make_outcome, load_fixture
) -> None:
    outcome = make_outcome(
        stdout=load_fixture("pytest_console_failure.txt"),
        exit_code=1,
        argv=DEFAULT_ARGV,
    )

    result = PytestTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.FAILED
    assert len(result.findings) == 3
    assert result.summary.get_int("failed") == 2
    assert result.summary.get_int("errored") == 1


def test_exit_zero_without_a_report_is_a_pass(make_context, make_outcome) -> None:
    outcome = make_outcome(stdout="12 passed in 1.20s\n", exit_code=0, argv=DEFAULT_ARGV)

    result = PytestTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.PASSED
    assert result.summary.get_int("passed") == 12


def test_launch_error_is_reported_as_infrastructure(make_context, make_outcome) -> None:
    outcome = make_outcome(
        exit_code=None,
        launch_error="executable not found: pytest",
        argv=DEFAULT_ARGV,
    )

    result = PytestTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True
    assert "could not be started" in result.narrative


def test_timeout_is_reported_as_timeout(make_context, make_outcome) -> None:
    outcome = make_outcome(
        exit_code=None,
        timed_out=True,
        duration_seconds=900.0,
        argv=DEFAULT_ARGV,
    )

    result = PytestTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.TIMEOUT
    assert result.infrastructure_failure is True
    assert result.error is not None


def test_unexpected_exit_status_is_an_error(make_context, make_outcome) -> None:
    outcome = make_outcome(stdout="", exit_code=42, argv=DEFAULT_ARGV)

    result = PytestTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True
    assert result.findings == ()


async def test_full_run_validates_and_consumes_the_report(
    make_context, make_outcome, workspace: Workspace, write_fixture
) -> None:
    """``tool.run`` must produce a contract-valid result and store the report."""
    write_fixture(workspace, "junit_mixed.xml", JUNIT_PATH)
    outcome = make_outcome(stdout="1 failed\n", exit_code=1, argv=DEFAULT_ARGV)
    context = make_context(outcomes=[outcome])

    result = await PytestTool().run(context)

    assert result.tool == "pytest"
    assert result.status is ToolStatus.FAILED
    assert result.exit_code == 1
    assert result.argv[0] == "pytest"
    assert result.report_artifact_id is not None
    assert result.summary.get_int("failed") == 1
    # The version probe ran before the tool itself.
    assert next(iter(context.sandbox.commands)).label == "pytest-version"
