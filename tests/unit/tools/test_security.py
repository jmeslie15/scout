"""Unit tests for :mod:`scout_tools.tools.security`."""

from __future__ import annotations

from typing import TYPE_CHECKING

from scout_domain.enums import Severity, ToolStatus
from scout_domain.models.sandbox import NetworkMode, SandboxPolicy
from scout_tools.tools.security import (
    BanditTool,
    GitleaksTool,
    PipAuditTool,
    SemgrepTool,
)

if TYPE_CHECKING:
    from scout_sandbox import Workspace

GITLEAKS_REPORT = ".scout/reports/gitleaks.json"
GITLEAKS_ARGV = ("gitleaks", "detect", "--report-format", "json", "--report-path", GITLEAKS_REPORT)
BANDIT_ARGV = ("bandit", "-r", ".", "-f", "json", "-q")
PIP_AUDIT_ARGV = ("pip-audit", "--format", "json", "--progress-spinner", "off")
SEMGREP_ARGV = ("semgrep", "scan", "--json", "--quiet", "--config", "auto")


# =====================================================================================
# gitleaks
# =====================================================================================


def test_gitleaks_argv_always_redacts(make_context) -> None:
    argv = list(GitleaksTool().build_argv(make_context()))

    assert not isinstance(argv, str)
    assert "--redact" in argv
    assert argv[-1] == "--redact"
    assert GITLEAKS_REPORT in argv


def test_gitleaks_reports_every_leak_as_critical(
    make_context, make_outcome, workspace: Workspace, write_fixture
) -> None:
    write_fixture(workspace, "gitleaks.json", GITLEAKS_REPORT)
    outcome = make_outcome(exit_code=1, argv=GITLEAKS_ARGV)

    result = GitleaksTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.FAILED
    assert result.infrastructure_failure is False
    assert len(result.findings) == 2
    assert all(finding.severity is Severity.CRITICAL for finding in result.findings)
    assert result.report_path == GITLEAKS_REPORT

    aws = next(item for item in result.findings if item.rule_id == "aws-access-token")
    assert aws.file == "config/settings.py"
    assert aws.line_start == 12
    assert aws.metadata["redacted"] is True
    assert "secret value" in str(aws.metadata["note"])


def test_gitleaks_never_reproduces_the_secret(
    make_context, make_outcome, workspace: Workspace, write_fixture
) -> None:
    """Even an unredacted report must not put the value into a finding.

    The fixture simulates the failure mode the ``--redact`` flag exists to prevent: a
    report that contains the raw value. The parser reads only descriptive fields, so the
    value cannot reach a finding, its metadata, or any narrative text.
    """
    secret = "AKIAIOSFODNN7EXAMPLE"
    write_fixture(workspace, "gitleaks_unredacted.json", GITLEAKS_REPORT)
    assert secret in (workspace.path / GITLEAKS_REPORT).read_text(encoding="utf-8")
    outcome = make_outcome(exit_code=1, argv=GITLEAKS_ARGV)

    result = GitleaksTool().interpret(outcome, make_context())

    rendered = " ".join(
        [
            repr(result.findings),
            result.narrative,
            str(result.summary.values),
            str([finding.metadata for finding in result.findings]),
            str(result.error),
        ]
    )
    assert secret not in rendered
    assert len(result.findings) == 1
    assert result.findings[0].severity is Severity.CRITICAL


def test_gitleaks_clean_run_passes(
    make_context, make_outcome, workspace: Workspace, write_fixture
) -> None:
    write_fixture(workspace, "gitleaks_empty.json", GITLEAKS_REPORT)
    outcome = make_outcome(exit_code=0, argv=GITLEAKS_ARGV)

    result = GitleaksTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.PASSED
    assert result.findings == ()


def test_gitleaks_exit_one_with_empty_report_is_an_error(
    make_context, make_outcome, workspace: Workspace, write_fixture
) -> None:
    write_fixture(workspace, "gitleaks_empty.json", GITLEAKS_REPORT)
    outcome = make_outcome(exit_code=1, argv=GITLEAKS_ARGV)

    result = GitleaksTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True


def test_gitleaks_exit_one_without_a_report_is_an_error(make_context, make_outcome) -> None:
    outcome = make_outcome(exit_code=1, argv=GITLEAKS_ARGV)

    result = GitleaksTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True
    assert result.findings == ()


def test_gitleaks_launch_error_is_infrastructure(make_context, make_outcome) -> None:
    outcome = make_outcome(
        exit_code=None, launch_error="executable not found: gitleaks", argv=GITLEAKS_ARGV
    )

    result = GitleaksTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True


def test_gitleaks_honours_a_custom_override(make_context) -> None:
    context = make_context(
        command_overrides={
            "security.custom.gitleaks": ("gitleaks", "detect", "--redact", "--no-banner")
        }
    )

    assert list(GitleaksTool().build_argv(context)) == [
        "gitleaks",
        "detect",
        "--redact",
        "--no-banner",
    ]


# =====================================================================================
# bandit
# =====================================================================================


def test_bandit_parses_issues(make_context, make_outcome, load_fixture) -> None:
    outcome = make_outcome(stdout=load_fixture("bandit.json"), exit_code=1, argv=BANDIT_ARGV)

    result = BanditTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.FAILED
    assert result.infrastructure_failure is False
    assert len(result.findings) == 2

    by_rule = {finding.rule_id: finding for finding in result.findings}
    assert by_rule["B602"].severity is Severity.MEDIUM
    assert by_rule["B602"].file == "src/runner.py"
    assert by_rule["B602"].line_start == 27
    assert by_rule["B602"].metadata["confidence"] == "HIGH"
    assert by_rule["B105"].severity is Severity.LOW
    assert by_rule["B105"].line_end == 41


def test_bandit_clean_run_passes(make_context, make_outcome, load_fixture) -> None:
    outcome = make_outcome(stdout=load_fixture("bandit_empty.json"), exit_code=0, argv=BANDIT_ARGV)

    result = BanditTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.PASSED
    assert result.findings == ()


def test_bandit_scan_errors_are_infrastructure(make_context, make_outcome, load_fixture) -> None:
    outcome = make_outcome(
        stdout=load_fixture("bandit_scan_errors.json"), exit_code=0, argv=BANDIT_ARGV
    )

    result = BanditTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True
    assert "failed to scan" in result.narrative


def test_bandit_exit_one_with_no_results_is_an_error(
    make_context, make_outcome, load_fixture
) -> None:
    outcome = make_outcome(stdout=load_fixture("bandit_empty.json"), exit_code=1, argv=BANDIT_ARGV)

    result = BanditTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True


def test_bandit_broken_json_is_an_error(make_context, make_outcome) -> None:
    outcome = make_outcome(stdout='{"results": [', exit_code=1, argv=BANDIT_ARGV)

    result = BanditTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True


def test_bandit_applicability_from_pyproject(make_context) -> None:
    context = make_context(files={"pyproject.toml": "[tool.bandit]\nskips = []\n"})

    assert BanditTool().is_applicable(context) is True


# =====================================================================================
# pip-audit
# =====================================================================================


def test_pip_audit_reports_each_vulnerability(make_context, make_outcome, load_fixture) -> None:
    outcome = make_outcome(stdout=load_fixture("pip_audit.json"), exit_code=1, argv=PIP_AUDIT_ARGV)

    result = PipAuditTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.FAILED
    assert result.infrastructure_failure is False
    assert len(result.findings) == 2
    assert result.summary.get_int("dependencies") == 4

    jinja = next(item for item in result.findings if item.rule_id == "GHSA-h5c8-rqwp-cp95")
    assert jinja.severity is Severity.HIGH
    assert "3.1.3" in jinja.recommendation
    assert jinja.metadata["aliases"] == "CVE-2024-22195"
    assert jinja.metadata["package"] == "jinja2"

    urllib3 = next(item for item in result.findings if item.metadata["package"] == "urllib3")
    assert "1.26.18" in urllib3.recommendation


def test_pip_audit_clean_run_passes(make_context, make_outcome, load_fixture) -> None:
    outcome = make_outcome(
        stdout=load_fixture("pip_audit_empty.json"), exit_code=0, argv=PIP_AUDIT_ARGV
    )

    result = PipAuditTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.PASSED
    assert result.findings == ()


def test_pip_audit_exit_one_without_findings_is_an_error(
    make_context, make_outcome, load_fixture
) -> None:
    outcome = make_outcome(
        stdout=load_fixture("pip_audit_empty.json"), exit_code=1, argv=PIP_AUDIT_ARGV
    )

    result = PipAuditTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True


def test_pip_audit_crash_is_an_error(make_context, make_outcome) -> None:
    outcome = make_outcome(stdout="", stderr="Traceback...\n", exit_code=2, argv=PIP_AUDIT_ARGV)

    result = PipAuditTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True


# =====================================================================================
# semgrep
# =====================================================================================


def test_semgrep_matches_are_findings_even_though_it_exits_zero(
    make_context, make_outcome, load_fixture
) -> None:
    outcome = make_outcome(stdout=load_fixture("semgrep.json"), exit_code=0, argv=SEMGREP_ARGV)

    result = SemgrepTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.FAILED
    assert result.infrastructure_failure is False
    assert len(result.findings) == 2

    by_rule = {finding.rule_id: finding for finding in result.findings}
    eval_finding = by_rule["python.lang.security.audit.eval-detected.eval-detected"]
    assert eval_finding.severity is Severity.HIGH
    assert eval_finding.file == "src/scout_app/expr.py"
    assert eval_finding.line_start == 15
    assert eval_finding.metadata["category"] == "security"
    assert eval_finding.metadata["excerpt"] == "return eval(user_input)"

    correctness = by_rule["python.lang.correctness.common-mistakes.is-comparison-string"]
    assert correctness.severity is Severity.MEDIUM
    assert correctness.line_start == 42


def test_semgrep_clean_run_passes(make_context, make_outcome) -> None:
    outcome = make_outcome(
        stdout='{"results": [], "errors": [], "paths": {"scanned": []}}',
        exit_code=0,
        argv=SEMGREP_ARGV,
    )

    result = SemgrepTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.PASSED


def test_semgrep_nonzero_exit_without_matches_is_an_error(make_context, make_outcome) -> None:
    outcome = make_outcome(
        stdout='{"results": [], "errors": [{"message": "invalid config"}]}',
        exit_code=2,
        argv=SEMGREP_ARGV,
    )

    result = SemgrepTool().interpret(outcome, make_context())

    assert result.status is ToolStatus.ERROR
    assert result.infrastructure_failure is True


def test_semgrep_is_skipped_when_the_network_is_disabled(make_context) -> None:
    """``--config auto`` downloads rules, so a no-network sandbox must explain the skip."""
    context = make_context(policy_mode=NetworkMode.NONE)

    reason = SemgrepTool().applicability_reason(context)

    assert reason is not None
    assert "semgrep.dev" in reason
    assert "network mode is 'none'" in reason


def test_semgrep_requires_semgrep_dev_in_the_allowlist(make_context) -> None:
    context = make_context(policy_mode=NetworkMode.ALLOWLIST, allowed_hosts=["registry.npmjs.org"])

    reason = SemgrepTool().applicability_reason(context)

    assert reason is not None
    assert "allowlist" in reason


def test_semgrep_applies_when_the_allowlist_permits_it(make_context) -> None:
    context = make_context(policy_mode=NetworkMode.ALLOWLIST, allowed_hosts=["semgrep.dev"])

    assert SemgrepTool().is_applicable(context) is True


def test_semgrep_applies_offline_with_a_local_ruleset(make_context) -> None:
    context = make_context(files={".semgrep.yml": "rules: []\n"}, policy_mode=NetworkMode.NONE)

    assert SemgrepTool().is_applicable(context) is True
    argv = list(SemgrepTool().build_argv(context))
    assert argv[-2:] == ["--config", ".semgrep.yml"]


def test_semgrep_network_policy_is_read_from_the_sandbox(make_context) -> None:
    context = make_context(
        policy=SandboxPolicy(network_mode=NetworkMode.ALLOWLIST, allowed_hosts=["semgrep.dev"])
    )

    assert SemgrepTool().is_applicable(context) is True
