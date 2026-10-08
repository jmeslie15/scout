"""Unit tests for :mod:`scout_tools.tools.linters`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from scout_domain.enums import Severity, ToolStatus
from scout_tools.tools.linters import (
    EslintTool,
    MypyTool,
    RuffFormatTool,
    RuffTool,
    TscTool,
)

if TYPE_CHECKING:
    from scout_sandbox import Workspace

RUFF_ARGV = ("ruff", "check", "--output-format=json", ".")
RUFF_FORMAT_ARGV = ("ruff", "format", "--check", "--diff", ".")
MYPY_ARGV = ("mypy", "--no-color-output", "--no-error-summary", ".")
ESLINT_ARGV = ("npx", "--no-install", "eslint", "--format", "json", ".")
TSC_ARGV = ("npx", "--no-install", "tsc", "--noEmit", "--pretty", "false")


def _padded(line_number: int, content: str) -> str:
    """Return a file whose 1-based ``line_number`` contains ``content``."""
    lines = [f"# filler {index}" for index in range(1, line_number)]
    lines.append(content)
    return "\n".join(lines) + "\n"


# =====================================================================================
# ruff check
# =====================================================================================


def test_ruff_argv_is_a_list(make_context) -> None:
    argv = RuffTool().build_argv(make_context())

    assert not isinstance(argv, str)
    assert list(argv) == list(RUFF_ARGV)


def test_ruff_override_still_requests_json(make_context) -> None:
    context = make_context(command_overrides={"lint": ("ruff", "check", ".")})

    argv = list(RuffTool().build_argv(context))

    assert argv == ["ruff", "check", ".", "--output-format=json"]


def test_ruff_parses_rules_locations_and_severities(
    make_context, make_outcome, workspace: Workspace, write_fixture
) -> None:
    write_fixture(workspace, "ruff.json", ".ruff-report.json")
    stdout = (workspace.path / ".ruff-report.json").read_text(encoding="utf-8")
    workspace.write_text("src/scout_app/settings.py", _padded(41, 'DEFAULT_PASSWORD = "hunter2"'))
    outcome = make_outcome(stdout=stdout, exit_code=1, argv=RUFF_ARGV)

    result = RuffTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.FAILED
    assert result.infrastructure_failure is False
    assert len(result.findings) == 4

    by_rule = {finding.rule_id: finding for finding in result.findings}
    assert by_rule["F401"].severity is Severity.LOW
    assert by_rule["E501"].severity is Severity.LOW
    assert by_rule["S105"].severity is Severity.HIGH
    assert by_rule["E999"].severity is Severity.HIGH
    assert by_rule["F401"].file == "src/scout_app/api.py"
    assert by_rule["F401"].line_start == 3
    assert by_rule["S105"].line_start == 41
    assert by_rule["S105"].metadata["excerpt"] == 'DEFAULT_PASSWORD = "hunter2"'
    assert by_rule["F401"].metadata["has_autofix"] is True


def test_ruff_clean_run_passes(make_context, make_outcome, load_fixture) -> None:
    outcome = make_outcome(stdout=load_fixture("ruff_empty.json"), exit_code=0, argv=RUFF_ARGV)

    result = RuffTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.PASSED
    assert result.findings == ()


def test_ruff_exit_one_with_an_empty_report_is_an_error(
    make_context, make_outcome, load_fixture
) -> None:
    outcome = make_outcome(stdout=load_fixture("ruff_empty.json"), exit_code=1, argv=RUFF_ARGV)

    result = RuffTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True


def test_ruff_truncated_json_is_an_error(make_context, make_outcome, load_fixture) -> None:
    outcome = make_outcome(
        stdout=load_fixture("ruff_truncated.json"),
        exit_code=1,
        argv=RUFF_ARGV,
        stdout_truncated=True,
    )

    result = RuffTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True
    assert result.findings == ()
    assert "JSON" in result.narrative


def test_ruff_exit_two_with_empty_stdout_is_an_error(make_context, make_outcome) -> None:
    outcome = make_outcome(stdout="", stderr="", exit_code=2, argv=RUFF_ARGV)

    result = RuffTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True


def test_ruff_applicability_from_pyproject(make_context) -> None:
    context = make_context(files={"pyproject.toml": "[tool.ruff]\nline-length = 100\n"})

    assert RuffTool().is_applicable(context) is True


def test_ruff_applicability_is_explained(make_context) -> None:
    reason = RuffTool().applicability_reason(make_context(files={"pyproject.toml": "[project]\n"}))

    assert reason is not None
    assert "marker files" in reason


# =====================================================================================
# ruff format
# =====================================================================================


def test_ruff_format_reports_unformatted_files(make_context, make_outcome, load_fixture) -> None:
    outcome = make_outcome(
        stdout=load_fixture("ruff_format_check.txt"), exit_code=1, argv=RUFF_FORMAT_ARGV
    )

    result = RuffFormatTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.FAILED
    assert result.summary.get_int("unformatted_files") == 2
    assert {finding.file for finding in result.findings} == {
        "src/scout_app/api.py",
        "src/scout_app/models.py",
    }
    assert all(finding.severity is Severity.LOW for finding in result.findings)


def test_ruff_format_parses_diff_hunks(make_context, make_outcome, load_fixture) -> None:
    outcome = make_outcome(
        stdout=load_fixture("ruff_format_diff.txt"), exit_code=1, argv=RUFF_FORMAT_ARGV
    )

    result = RuffFormatTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.FAILED
    assert len(result.findings) == 2
    api = next(item for item in result.findings if item.file == "src/scout_app/api.py")
    assert "def handler(request, response):" in api.metadata["diff"]


def test_ruff_format_clean_run_passes(make_context, make_outcome) -> None:
    outcome = make_outcome(stdout="2 files already formatted\n", exit_code=0, argv=RUFF_FORMAT_ARGV)

    result = RuffFormatTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.PASSED


def test_ruff_format_exit_two_is_an_error(make_context, make_outcome) -> None:
    outcome = make_outcome(stdout="", exit_code=2, argv=RUFF_FORMAT_ARGV)

    result = RuffFormatTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True


def test_ruff_format_override_keeps_the_diff_flags(make_context) -> None:
    context = make_context(command_overrides={"format": ("ruff", "format")})

    assert list(RuffFormatTool().build_argv(context)) == [
        "ruff",
        "format",
        "--check",
        "--diff",
        ".",
    ]


# =====================================================================================
# mypy
# =====================================================================================


def test_mypy_parses_codes_and_severities(
    make_context, make_outcome, workspace: Workspace, write_fixture, load_fixture
) -> None:
    write_fixture(workspace, "mypy.txt", ".mypy-report.txt")
    workspace.write_text("src/services/billing.py", _padded(58, "charge(user_id)"))
    outcome = make_outcome(stdout=load_fixture("mypy.txt"), exit_code=1, argv=MYPY_ARGV)

    result = MypyTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.FAILED
    assert result.infrastructure_failure is False
    assert len(result.findings) == 7
    assert result.summary.get_int("notes") == 1

    by_code = {finding.rule_id: finding for finding in result.findings}
    assert by_code["syntax"].severity is Severity.HIGH
    assert by_code["return-value"].severity is Severity.MEDIUM
    assert by_code["attr-defined"].severity is Severity.MEDIUM
    assert by_code["arg-type"].severity is Severity.MEDIUM
    assert by_code["import-untyped"].severity is Severity.LOW
    assert by_code["var-annotated"].severity is Severity.LOW
    assert by_code["unreachable"].severity is Severity.LOW
    assert by_code["unreachable"].metadata["level"] == "warning"
    assert by_code["attr-defined"].file == "src/models/user.py"
    assert by_code["attr-defined"].line_start == 23
    assert by_code["arg-type"].metadata["excerpt"] == "charge(user_id)"


def test_mypy_clean_run_passes(make_context, make_outcome, load_fixture) -> None:
    outcome = make_outcome(stdout=load_fixture("mypy_clean.txt"), exit_code=0, argv=MYPY_ARGV)

    result = MypyTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.PASSED
    assert result.findings == ()


def test_mypy_exit_two_means_mypy_itself_failed(make_context, make_outcome) -> None:
    outcome = make_outcome(
        stdout="", stderr="mypy: error: can't read config file\n", exit_code=2, argv=MYPY_ARGV
    )

    result = MypyTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True
    assert "mypy itself failed" in result.narrative


def test_mypy_exit_one_without_parseable_output_is_an_error(make_context, make_outcome) -> None:
    outcome = make_outcome(stdout="", exit_code=1, argv=MYPY_ARGV)

    result = MypyTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True
    assert result.findings == ()


def test_mypy_applicability_from_pyproject(make_context) -> None:
    context = make_context(files={"pyproject.toml": "[tool.mypy]\nstrict = true\n"})

    assert MypyTool().is_applicable(context) is True


def test_mypy_applicability_is_explained(make_context) -> None:
    reason = MypyTool().applicability_reason(make_context(files={"pyproject.toml": "[project]\n"}))

    assert reason is not None
    assert "marker files" in reason


# =====================================================================================
# eslint
# =====================================================================================


def test_eslint_parses_messages(make_context, make_outcome, load_fixture) -> None:
    outcome = make_outcome(stdout=load_fixture("eslint.json"), exit_code=1, argv=ESLINT_ARGV)

    result = EslintTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.FAILED
    assert result.summary.get_int("errors") == 2
    assert result.summary.get_int("warnings") == 1
    assert len(result.findings) == 3

    by_rule = {finding.rule_id: finding for finding in result.findings}
    assert by_rule["no-unused-vars"].severity is Severity.MEDIUM
    assert by_rule["@typescript-eslint/no-explicit-any"].severity is Severity.LOW
    assert by_rule[None].severity is Severity.HIGH
    assert by_rule[None].metadata["fatal"] is True
    assert by_rule["no-unused-vars"].file == "src/index.ts"
    assert by_rule["no-unused-vars"].line_start == 12


def test_eslint_clean_run_passes(make_context, make_outcome, load_fixture) -> None:
    outcome = make_outcome(stdout=load_fixture("eslint_empty.json"), exit_code=0, argv=ESLINT_ARGV)

    result = EslintTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.PASSED


def test_eslint_exit_one_with_empty_report_is_an_error(
    make_context, make_outcome, load_fixture
) -> None:
    outcome = make_outcome(stdout=load_fixture("eslint_empty.json"), exit_code=1, argv=ESLINT_ARGV)

    result = EslintTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True


def test_eslint_exit_two_is_an_error(make_context, make_outcome) -> None:
    outcome = make_outcome(
        stdout="", stderr="Oops! Something went wrong! :(\n", exit_code=2, argv=ESLINT_ARGV
    )

    result = EslintTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True


def test_eslint_requires_package_json(make_context) -> None:
    """A config without a package.json is not something eslint can run against."""
    context = make_context(files={".eslintrc.json": "{}"})

    reason = EslintTool().applicability_reason(context)

    assert reason is not None
    assert "package.json" in reason


def test_eslint_requires_a_config(make_context) -> None:
    context = make_context(files={"package.json": "{}"})

    reason = EslintTool().applicability_reason(context)

    assert reason is not None
    assert "eslint configuration" in reason


def test_eslint_applies_with_package_json_and_config(make_context) -> None:
    context = make_context(files={"package.json": "{}", "eslint.config.js": "export default [];"})

    assert EslintTool().is_applicable(context) is True


# =====================================================================================
# tsc
# =====================================================================================


def test_tsc_parses_diagnostics(make_context, make_outcome, load_fixture) -> None:
    outcome = make_outcome(stdout=load_fixture("tsc.txt"), exit_code=1, argv=TSC_ARGV)

    result = TscTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.FAILED
    assert len(result.findings) == 4

    by_code = {finding.rule_id: finding for finding in result.findings}
    assert by_code["TS2345"].severity is Severity.MEDIUM
    assert by_code["TS2339"].severity is Severity.MEDIUM
    assert by_code["TS1005"].severity is Severity.HIGH
    assert by_code["TS5083"].file is None
    assert by_code["TS2345"].file == "src/api/client.ts"
    assert by_code["TS2345"].line_start == 42
    assert by_code["TS2345"].metadata["column"] == 17


def test_tsc_clean_run_passes(make_context, make_outcome) -> None:
    outcome = make_outcome(stdout="", exit_code=0, argv=TSC_ARGV)

    result = TscTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.PASSED


def test_tsc_exit_one_without_diagnostics_is_an_error(make_context, make_outcome) -> None:
    outcome = make_outcome(stdout="", exit_code=1, argv=TSC_ARGV)

    result = TscTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True


def test_tsc_exit_two_is_an_error(make_context, make_outcome) -> None:
    outcome = make_outcome(exit_code=2, argv=TSC_ARGV)

    result = TscTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True


def test_tsc_applies_with_a_tsconfig(make_context) -> None:
    assert TscTool().is_applicable(make_context(files={"tsconfig.json": "{}"})) is True


def test_tsc_skip_is_explained_without_a_tsconfig(make_context) -> None:
    reason = TscTool().applicability_reason(make_context())

    assert reason is not None
    assert "tsconfig.json" in reason
