"""Contract tests for the deterministic tool set (spec sections 20, 21, and 65).

These assert the properties the rest of Scout relies on:

* every registered tool declares the fields the dashboard and the registry need;
* an argument vector is always a list, and never carries anything a shell would act on;
* every tool's full ``run`` produces a :class:`ToolResult` that validates against
  ``scout_domain.models.tool.ToolResult``;
* the registry explains every skip, including marker files, disabled tools, and the
  network-dependent semgrep case;
* every tool's parser is *total* and keeps "found problems" (``FAILED``) distinct from
  "the tool broke" (``ERROR`` with ``infrastructure_failure=True``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from scout_domain.enums import Capability, ToolStatus
from scout_domain.models.tool import ToolResult
from scout_tools.base import ToolCategory, ToolOutcome
from scout_tools.registry import ToolRegistry
from scout_tools.tools import build_default_tools
from scout_tools.tools.pytest import PytestTool

if TYPE_CHECKING:
    from scout_sandbox import Workspace

JUNIT_PATH = ".scout/reports/junit.xml"
JUNIT_XML = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" errors="1" failures="1" tests="3" time="0.5">
    <testcase classname="tests.test_api" name="test_ok" time="0.1"
              file="tests/test_api.py" line="4" />
    <testcase classname="tests.test_api" name="test_bad" time="0.2"
              file="tests/test_api.py" line="10">
      <failure message="AssertionError: assert 1 == 2">assert 1 == 2</failure>
    </testcase>
    <testcase classname="tests.test_db" name="test_db" time="0.2" file="tests/test_db.py" line="7">
      <error message="RuntimeError: unavailable">RuntimeError: unavailable</error>
    </testcase>
  </testsuite>
</testsuites>
"""
JUNIT_ARGV = ("pytest", "-q", f"--junit-xml={JUNIT_PATH}", "-p", "no:cacheprovider")

#: Sequences a shell would interpret. None may appear in an argv element.
SHELL_METACHARACTERS = (";", "|", "&", ">", "<", "`", "$(", "${", "\n", "\r")


# =====================================================================================
# declarations
# =====================================================================================


def test_every_tool_declares_its_contract() -> None:
    tools = build_default_tools()

    assert tools, "the default tool set must not be empty"
    for tool in tools:
        assert tool.name, "every tool must be named"
        assert isinstance(tool.name, str)
        assert isinstance(tool.capability, Capability), tool.name
        assert isinstance(tool.category, ToolCategory), tool.name
        assert tool.description.strip(), f"{tool.name} needs a description"
        assert tool.version_argv, f"{tool.name} must declare version_argv"
        assert all(isinstance(part, str) for part in tool.version_argv), tool.name
        assert isinstance(tool.marker_files, frozenset), tool.name
        assert isinstance(tool.ecosystems, frozenset), tool.name
        assert all(isinstance(path, str) for path in tool.marker_files), tool.name


def test_tool_names_are_unique() -> None:
    names = [tool.name for tool in build_default_tools()]

    assert len(names) == len(set(names))
    assert set(names) == {
        "pytest",
        "coverage",
        "ruff",
        "ruff-format",
        "mypy",
        "eslint",
        "tsc",
        "gitleaks",
        "bandit",
        "semgrep",
        "pip-audit",
        "npm-audit",
        "trivy",
    }


def test_registry_accepts_the_default_tools() -> None:
    registry = ToolRegistry(build_default_tools())

    assert len(registry) == len(build_default_tools())
    assert registry.get("pytest").category is ToolCategory.TEST


def test_only_filter_restricts_the_tool_set() -> None:
    tools = build_default_tools(only={"pytest", "ruff"})

    assert {tool.name for tool in tools} == {"pytest", "ruff"}


# =====================================================================================
# argument vectors
# =====================================================================================


def test_build_argv_is_an_argv_list_without_shell_metacharacters(make_context) -> None:
    context = make_context(changed_paths=("src/app.py", "migrations/0001_init.py"))

    for tool in build_default_tools():
        argv = tool.build_argv(context)

        assert not isinstance(argv, str), tool.name
        assert isinstance(argv, (list, tuple)), tool.name
        assert argv, f"{tool.name} produced an empty argv"
        for part in argv:
            assert isinstance(part, str), tool.name
            for metacharacter in SHELL_METACHARACTERS:
                assert metacharacter not in part, (
                    f"{tool.name} argv element {part!r} contains {metacharacter!r}"
                )


def test_argv_for_never_raises(make_context) -> None:
    context = make_context()

    for tool in build_default_tools():
        recorded = tool.argv_for(context)

        assert recorded is not None, tool.name
        assert not isinstance(recorded, str), tool.name


# =====================================================================================
# ToolResult contract
# =====================================================================================


async def test_run_produces_a_result_that_validates(
    make_context, make_outcome, workspace: Workspace
) -> None:
    workspace.write_text(JUNIT_PATH, JUNIT_XML)
    outcome = make_outcome(stdout="1 failed, 1 error\n", exit_code=1, argv=JUNIT_ARGV)
    context = make_context(outcomes=[outcome])

    result = await PytestTool().run(context)

    assert isinstance(result, ToolResult)
    validated = ToolResult.model_validate(result.model_dump())
    assert validated.tool == "pytest"
    assert validated.status is ToolStatus.FAILED
    assert validated.exit_code == 1
    assert validated.report_artifact_id is not None
    assert validated.stdout_artifact_id is not None
    assert validated.summary.get_int("failed") == 1
    assert validated.summary.get_int("errored") == 1
    # A failing test is a finding about the change, not an error in Scout.
    assert validated.is_failure is True
    assert validated.is_error is False


async def test_every_tool_run_produces_a_valid_result(make_context, make_outcome) -> None:
    outcome = make_outcome(exit_code=3, stdout="", stderr="kaput", argv=("tool",))

    for tool in build_default_tools():
        context = make_context(outcomes=[outcome])
        result = await tool.run(context)

        assert isinstance(result, ToolResult), tool.name
        ToolResult.model_validate(result.model_dump())
        assert result.tool == tool.name
        assert result.status is ToolStatus.ERROR, tool.name
        assert result.is_error is True, tool.name
        assert result.is_failure is False, tool.name
        assert result.infrastructure_failure is True, tool.name
        assert result.metadata["findings_count"] == 0, tool.name


# =====================================================================================
# registry planning
# =====================================================================================


def test_plan_explains_a_marker_file_skip(make_context) -> None:
    context = make_context(files={"pyproject.toml": "[project]\nname = 'widgets'\n"})

    plan = ToolRegistry(build_default_tools()).plan(context)

    skipped = dict(plan.skipped)
    assert "mypy" in skipped
    assert "marker files" in skipped["mypy"]
    assert "mypy.ini" in skipped["mypy"]
    assert "mypy: " in plan.explain()
    # pytest declares pyproject.toml as a marker, so it is selected here even though the
    # file says nothing about pytest; the skip explanation is the point, not the choice.
    assert "pytest" in plan.names()


def test_plan_explains_a_disabled_tool(make_context) -> None:
    context = make_context(files={"pyproject.toml": "pytest"})
    registry = ToolRegistry(build_default_tools(disabled={"pytest"}))

    plan = registry.plan(context)

    assert dict(plan.skipped)["pytest"] == "tool is disabled by configuration"
    assert "pytest" not in plan.names()


def test_plan_explains_the_semgrep_network_skip(make_context) -> None:
    context = make_context(files={"pyproject.toml": "pytest"})

    skipped = dict(ToolRegistry(build_default_tools()).plan(context).skipped)

    assert "semgrep" in skipped
    assert "semgrep.dev" in skipped["semgrep"]
    assert "allowlist" in skipped["semgrep"]


def test_plan_explains_missing_repository_summary(make_context) -> None:
    context = make_context(files={"pyproject.toml": "pytest"}, with_summary=False)

    skipped = dict(ToolRegistry(build_default_tools()).plan(context).skipped)

    assert "pytest" in skipped
    assert "repository summary" in skipped["pytest"]


# =====================================================================================
# totality and the failure/error distinction
# =====================================================================================


def test_interpret_is_total_for_empty_garbage_and_timeout(make_context, make_outcome) -> None:
    outcomes = (
        make_outcome(exit_code=0, stdout="", stderr=""),
        make_outcome(exit_code=1, stdout="not json at all {{{", stderr="boom"),
        make_outcome(exit_code=2, stdout="", stderr=""),
        make_outcome(exit_code=None, launch_error="executable not found"),
        make_outcome(exit_code=None, timed_out=True, duration_seconds=900.0),
        make_outcome(exit_code=0, stdout="\x00\x01 binary noise", stderr=""),
    )

    for tool in build_default_tools():
        for outcome in outcomes:
            context = make_context()
            result = tool.interpret(outcome, context)

            assert isinstance(result, ToolOutcome), tool.name
            assert isinstance(result.status, ToolStatus), tool.name
            assert isinstance(result.findings, tuple), tool.name


def test_a_nonzero_exit_without_evidence_is_an_error_not_a_failure(
    make_context, make_outcome
) -> None:
    """Exit code 3 with empty output tells Scout nothing about the change."""
    for tool in build_default_tools():
        context = make_context()
        result = tool.interpret(
            make_outcome(exit_code=3, stdout="", stderr="", argv=("tool",)), context
        )

        assert result.status is ToolStatus.ERROR, tool.name
        assert result.infrastructure_failure is True, tool.name
        assert result.findings == (), tool.name
        assert result.narrative, tool.name


def test_launch_errors_and_timeouts_are_never_findings(make_context, make_outcome) -> None:
    for tool in build_default_tools():
        for outcome in (
            make_outcome(exit_code=None, launch_error="executable not found"),
            make_outcome(exit_code=None, timed_out=True, duration_seconds=60.0),
        ):
            result = tool.interpret(outcome, make_context())

            assert result.status in {ToolStatus.ERROR, ToolStatus.TIMEOUT}, tool.name
            assert result.infrastructure_failure is True, tool.name
            assert result.findings == (), tool.name
