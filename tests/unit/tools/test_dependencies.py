"""Unit tests for :mod:`scout_tools.tools.dependencies`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from scout_domain.enums import Severity, ToolStatus
from scout_tools.tools.dependencies import NpmAuditTool, TrivyTool

if TYPE_CHECKING:
    from scout_sandbox import Workspace

NPM_ARGV = ("npm", "audit", "--json", "--audit-level=low")
TRIVY_REPORT = ".scout/reports/trivy.json"
TRIVY_ARGV = ("trivy", "fs", "--format", "json", "--quiet", "--scanners", "vuln,misconfig", ".")


# =====================================================================================
# npm audit
# =====================================================================================


def test_npm_audit_argv_is_a_list(make_context) -> None:
    argv = NpmAuditTool().build_argv(make_context())

    assert not isinstance(argv, str)
    assert list(argv) == list(NPM_ARGV)


def test_npm_audit_maps_severities_and_fixes(make_context, make_outcome, load_fixture) -> None:
    outcome = make_outcome(stdout=load_fixture("npm_audit.json"), exit_code=1, argv=NPM_ARGV)

    result = NpmAuditTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.FAILED
    assert result.infrastructure_failure is False
    assert len(result.findings) == 2
    assert result.summary.get_int("vulnerable_packages") == 2
    assert result.summary.get_int("critical") == 1
    assert result.summary.get_int("high") == 1

    by_package = {finding.metadata["package"]: finding for finding in result.findings}
    lodash = by_package["lodash"]
    assert lodash.severity is Severity.HIGH
    assert "4.17.21" in lodash.recommendation
    assert lodash.metadata["cvss"] == 7.4
    assert "Prototype Pollution in lodash" in lodash.description

    minimist = by_package["minimist"]
    assert minimist.severity is Severity.CRITICAL
    assert "npm audit fix" in minimist.recommendation
    assert minimist.metadata["is_direct"] is True


def test_npm_audit_clean_run_passes(make_context, make_outcome, load_fixture) -> None:
    outcome = make_outcome(stdout=load_fixture("npm_audit_empty.json"), exit_code=0, argv=NPM_ARGV)

    result = NpmAuditTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.PASSED
    assert result.findings == ()


def test_npm_audit_operational_error_is_infrastructure(
    make_context, make_outcome, load_fixture
) -> None:
    """``{"error": ...}`` means npm could not audit; it is not a clean bill of health."""
    outcome = make_outcome(stdout=load_fixture("npm_audit_error.json"), exit_code=1, argv=NPM_ARGV)

    result = NpmAuditTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True
    assert result.findings == ()
    assert "ENOLOCK" in (result.error or "") or "lockfile" in result.narrative


def test_npm_audit_exit_one_without_vulnerabilities_is_an_error(
    make_context, make_outcome, load_fixture
) -> None:
    outcome = make_outcome(stdout=load_fixture("npm_audit_empty.json"), exit_code=1, argv=NPM_ARGV)

    result = NpmAuditTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True


def test_npm_audit_crash_is_an_error(make_context, make_outcome) -> None:
    outcome = make_outcome(stdout="", exit_code=2, argv=NPM_ARGV)

    result = NpmAuditTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True


# =====================================================================================
# trivy
# =====================================================================================


def test_trivy_reports_vulnerabilities_and_misconfigurations(
    make_context, make_outcome, workspace: Workspace, write_fixture
) -> None:
    write_fixture(workspace, "trivy.json", TRIVY_REPORT)
    outcome = make_outcome(exit_code=0, argv=TRIVY_ARGV)

    result = TrivyTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.FAILED
    assert result.infrastructure_failure is False
    assert result.summary.get_int("vulnerabilities") == 2
    assert result.summary.get_int("misconfigurations") == 1
    assert len(result.findings) == 3
    assert result.report_path == TRIVY_REPORT

    by_rule = {finding.rule_id: finding for finding in result.findings}
    assert by_rule["CVE-2021-23337"].severity is Severity.HIGH
    assert by_rule["CVE-2021-23337"].file == "package-lock.json"
    assert "4.17.21" in by_rule["CVE-2021-23337"].recommendation
    assert by_rule["CVE-2022-25883"].severity is Severity.MEDIUM
    assert by_rule["DS002"].severity is Severity.HIGH
    assert by_rule["DS002"].file == "deploy/Dockerfile"
    assert by_rule["DS002"].line_start == 1
    assert by_rule["DS002"].line_end == 12
    assert "USER" in by_rule["DS002"].recommendation


def test_trivy_clean_run_passes(
    make_context, make_outcome, workspace: Workspace, write_fixture
) -> None:
    write_fixture(workspace, "trivy_empty.json", TRIVY_REPORT)
    outcome = make_outcome(exit_code=0, argv=TRIVY_ARGV)

    result = TrivyTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.PASSED
    assert result.findings == ()


def test_trivy_crash_is_an_error(make_context, make_outcome) -> None:
    outcome = make_outcome(stdout="", exit_code=2, argv=TRIVY_ARGV)

    result = TrivyTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True


def test_trivy_skip_is_explained_without_enablement(make_context) -> None:
    reason = TrivyTool().applicability_reason(make_context())

    assert reason is not None
    assert "trivy" in reason
    assert "security.tools" in reason


def test_trivy_applies_when_listed_in_security_tools(make_context) -> None:
    context = make_context(security_tools=["trivy"])

    assert TrivyTool().is_applicable(context) is True


def test_trivy_applies_with_a_marker_file(make_context) -> None:
    context = make_context(files={"trivy.yaml": "severity: HIGH\n"})

    assert TrivyTool().is_applicable(context) is True
