"""Unit tests for :mod:`scout_tools.tools.generic`."""

from __future__ import annotations

import pytest

from scout_domain.enums import Capability, ToolStatus
from scout_tools.base import ToolCategory
from scout_tools.tools.generic import GenericCommandTool, exit_code_parser

TEMPLATE = (
    "migration-check",
    "--repo",
    "{repository}",
    "--commit",
    "{commit_sha}",
    "--report",
    "{report_path}",
    "{changed_paths}",
)


def _tool(**overrides: object) -> GenericCommandTool:
    kwargs: dict[str, object] = {
        "name": "migration-check",
        "argv_template": list(TEMPLATE),
        "capability": Capability.TEST_RUN,
        "category": ToolCategory.TEST,
        "marker_files": frozenset({"alembic.ini"}),
        "parser": exit_code_parser(finding_exit_codes=frozenset({1})),
        "finding_exit_codes": frozenset({1}),
        "report_file": ".scout/reports/migration-check.json",
    }
    kwargs.update(overrides)
    return GenericCommandTool(**kwargs)  # type: ignore[arg-type]


def test_argv_is_a_list_with_substitutions(make_context) -> None:
    context = make_context(changed_paths=("migrations/0001_init.py", "app/models.py"))

    argv = _tool().build_argv(context)

    assert not isinstance(argv, str)
    assert list(argv) == [
        "migration-check",
        "--repo",
        "acme/widgets",
        "--commit",
        "a" * 40,
        "--report",
        ".scout/reports/migration-check.json",
        "migrations/0001_init.py",
        "app/models.py",
    ]


def test_changed_paths_csv_joins_into_one_argument(make_context) -> None:
    tool = _tool(argv_template=["check", "--paths={changed_paths_csv}"])
    context = make_context(changed_paths=("a.py", "b.py"))

    argv = list(tool.build_argv(context))

    assert argv == ["check", "--paths=a.py,b.py"]


def test_changed_paths_are_never_shell_interpreted(make_context) -> None:
    """Substitution is into a single argv element; no shell ever sees it."""
    hostile = "; rm -rf / #"
    context = make_context(changed_paths=(hostile,))

    argv = list(_tool().build_argv(context))

    assert argv[-1] == hostile
    assert argv.count(hostile) == 1


def test_declared_report_path_is_used(make_context) -> None:
    tool = _tool(argv_template=["check", "--out", "{report_path}"])

    assert list(tool.build_argv(make_context())) == [
        "check",
        "--out",
        ".scout/reports/migration-check.json",
    ]


def test_default_report_path_is_derived_from_the_name(make_context) -> None:
    tool = _tool(report_file=None, argv_template=["check", "--out", "{report_path}"])

    assert list(tool.build_argv(make_context())) == [
        "check",
        "--out",
        ".scout/reports/migration-check.json",
    ]


def test_unknown_placeholder_is_rejected_at_construction() -> None:
    with pytest.raises(ValueError, match="unknown placeholder"):
        _tool(argv_template=["check", "--token", "{github_token}"])


def test_non_string_element_is_rejected() -> None:
    with pytest.raises(TypeError, match="must be strings"):
        _tool(argv_template=["check", 42])


def test_empty_template_is_rejected() -> None:
    with pytest.raises(ValueError, match="non-empty argv_template"):
        _tool(argv_template=[])


def test_exit_code_parser_passes_on_zero(make_context, make_outcome) -> None:
    result = _tool().interpret(make_outcome(exit_code=0, argv=("migration-check",)), make_context())

    assert result.status is ToolStatus.PASSED
    assert result.findings == ()


def test_exit_code_parser_fails_on_a_finding_exit_code(make_context, make_outcome) -> None:
    result = _tool().interpret(make_outcome(exit_code=1, argv=("migration-check",)), make_context())

    assert result.status is ToolStatus.FAILED
    assert result.infrastructure_failure is False
    assert result.findings == ()


def test_exit_code_parser_errors_on_an_unknown_exit_code(make_context, make_outcome) -> None:
    result = _tool().interpret(make_outcome(exit_code=2, argv=("migration-check",)), make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True


def test_exit_code_parser_handles_launch_error(make_context, make_outcome) -> None:
    outcome = make_outcome(
        exit_code=None,
        launch_error="executable not found: migration-check",
        argv=("migration-check",),
    )

    result = _tool().interpret(outcome, make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True


def test_exit_code_parser_handles_timeout(make_context, make_outcome) -> None:
    outcome = make_outcome(
        exit_code=None, timed_out=True, duration_seconds=600.0, argv=("migration-check",)
    )

    result = _tool().interpret(outcome, make_context())

    assert result.status is ToolStatus.TIMEOUT
    assert result.infrastructure_failure is True


def test_declared_override_key_replaces_the_template(make_context) -> None:
    """A declared build/test/lint command wins over the template, as it does elsewhere."""
    tool = _tool(override_key="build")

    args_without_override = list(tool.build_argv(make_context()))
    assert args_without_override[0] == "migration-check"

    context = make_context(command_overrides={"build": ("make", "dist")})
    assert list(tool.build_argv(context)) == ["make", "dist"]


def test_declared_contract_surfaces_on_the_instance() -> None:
    tool = _tool()

    assert tool.name == "migration-check"
    assert tool.capability is Capability.TEST_RUN
    assert tool.category is ToolCategory.TEST
    assert tool.marker_files == frozenset({"alembic.ini"})
    assert tool.finding_exit_codes == frozenset({1})
