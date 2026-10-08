"""Security scanners: secrets, static analysis, and dependency vulnerabilities.

Four tools, three different questions:

* :class:`GitleaksTool` — did anyone commit a credential?
* :class:`BanditTool` — does the Python code use a dangerous pattern?
* :class:`SemgrepTool` — does a rule set match anything in the change?
* :class:`PipAuditTool` — does a pinned dependency have a known advisory?

Two properties are enforced here rather than left to the caller:

**Secrets are recorded, never reproduced.** gitleaks is always invoked with ``--redact``,
and the parser reads only the descriptive fields of its report (rule, file, line, commit).
The ``Secret``, ``Match``, and ``Line`` fields are never copied into a finding, its
metadata, or the narrative, so a value that leaked into a repository cannot leak again
into a Scout report, log, or database row. ``tests/unit/tools/test_security.py`` asserts
this directly.

**Network-dependent scanners say so instead of failing mysteriously.**
``semgrep --config auto`` downloads a rule set from ``semgrep.dev``. Under the default
``SCOUT_SANDBOX_NETWORK=none`` that can only fail, so applicability returns an explicit
reason rather than letting the tool die inside the sandbox.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from scout_domain.enums import Capability, Severity, ToolStatus
from scout_domain.models.sandbox import NetworkMode
from scout_domain.models.tool import ToolSummary
from scout_tools.base import CommandTool, ParsedFinding, ToolCategory, ToolOutcome
from scout_tools.tools._common import (
    SourceCache,
    as_float,
    as_int,
    as_mapping,
    as_sequence,
    as_str,
    cap_findings,
    declares_any,
    ensure_report_directory,
    excerpt,
    launch_failure,
    nonzero_without_evidence,
    parse_json,
    read_workspace_text,
    relative_workspace_path,
    report_path,
    scanner_outcome,
    severity_from_name,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from scout_sandbox.command import CommandOutcome
    from scout_tools.base import ToolContext

__all__ = [
    "BanditTool",
    "GitleaksTool",
    "PipAuditTool",
    "SemgrepTool",
]

_EXIT_OK: Final[int] = 0
_EXIT_FINDINGS: Final[int] = 1

_PYTHON_MANIFESTS: Final[tuple[str, ...]] = (
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "setup.cfg",
)

#: Hosts semgrep must reach when it downloads the ``auto`` community rule set.
_SEMGREP_HOSTS: Final[frozenset[str]] = frozenset({"semgrep.dev", "metrics.semgrep.dev"})


# =====================================================================================
# gitleaks
# =====================================================================================


class GitleaksTool(CommandTool):
    """Secret detection over the checkout, always with redaction enabled."""

    name = "gitleaks"
    capability = Capability.SECURITY_SCAN
    category = ToolCategory.SECRET_SCAN
    description = (
        "Scans the checkout for committed secrets with gitleaks --redact and records the "
        "rule, file, and line of every match without ever recording the secret itself."
    )
    version_argv: tuple[str, ...] = ("gitleaks", "version")
    #: A secret can be committed anywhere, in any language, so this tool has no ecosystem
    #: or marker-file restriction: it is applicable to every repository.
    ecosystems = frozenset()
    marker_files = frozenset()
    finding_exit_codes = frozenset({_EXIT_FINDINGS})

    @property
    def report_file(self) -> str:
        """Workspace-relative path gitleaks writes its JSON report to."""
        return report_path("gitleaks.json")

    def build_argv(self, context: ToolContext) -> Sequence[str]:
        override = context.override(f"security.custom.{self.name}")
        if override is not None:
            return list(override)
        ensure_report_directory(context)
        # ``--redact`` is not optional: Scout records that a secret exists and where, and
        # never the value.
        return [
            "gitleaks",
            "detect",
            "--no-banner",
            "--report-format",
            "json",
            "--report-path",
            self.report_file,
            "--redact",
        ]

    def interpret(self, outcome: CommandOutcome, context: ToolContext) -> ToolOutcome:
        broken = launch_failure(self.name, outcome)
        if broken is not None:
            return broken

        exit_code = outcome.exit_code
        assert exit_code is not None  # launch_failure() guarantees an exit code
        if exit_code not in {_EXIT_OK, _EXIT_FINDINGS}:
            return nonzero_without_evidence(self.name, outcome, what="gitleaks JSON report")

        text = read_workspace_text(context, self.report_file)
        if text is None:
            if exit_code == _EXIT_OK:
                return ToolOutcome(
                    status=ToolStatus.PASSED,
                    narrative="gitleaks reported no leaks.",
                )
            return nonzero_without_evidence(self.name, outcome, what="gitleaks JSON report")

        value, _error = parse_json(text)
        if value is None or not isinstance(value, list):
            return nonzero_without_evidence(self.name, outcome, what="gitleaks JSON report")

        findings, dropped = cap_findings([_leak_finding(item, context) for item in value])
        summary = ToolSummary(
            values={"leaks": len(value), "reported_findings": len(findings), "dropped": dropped}
        )
        failed_narrative = (
            f"gitleaks found {len(value)} potential secret(s) in the checkout. Values are "
            "redacted; the raw report is attached as an artifact."
        )
        if dropped:
            failed_narrative += f" Only the first {len(findings)} are listed as findings."
        return scanner_outcome(
            self.name,
            findings=findings,
            summary=summary,
            exit_code=exit_code,
            finding_exit_codes=self.finding_exit_codes,
            passed_narrative="gitleaks reported no leaks.",
            failed_narrative=failed_narrative,
            report_path=self.report_file,
        )


def _leak_finding(item: object, context: ToolContext) -> ParsedFinding:
    """Build a finding from a gitleaks leak *without* touching the secret fields.

    gitleaks reports ``Secret``, ``Match``, and ``Line``. None of them are read here:
    redaction at the source plus omission at the parser means the value cannot reach a
    finding even if ``--redact`` is ever removed from the command by mistake.
    """
    entry = as_mapping(item)
    rule = as_str(entry.get("RuleID")) or "gitleaks"
    description = as_str(entry.get("Description")) or "a potential secret was detected"
    raw_path = as_str(entry.get("File"))
    path = relative_workspace_path(context, raw_path) if raw_path else ""
    start = as_int(entry.get("StartLine"), 0) or None
    end = as_int(entry.get("EndLine"), 0) or start

    return ParsedFinding(
        severity=Severity.CRITICAL,
        title=f"{rule}: potential secret committed in {path or 'the repository'}",
        description=excerpt(description, limit=400),
        rule_id=rule,
        file=path or None,
        line_start=start,
        line_end=end,
        recommendation=(
            "Treat the credential as compromised: rotate it, remove it from history, and "
            "load it from the environment or a secret manager instead."
        ),
        metadata={
            "tool": "gitleaks",
            "rule_id": rule,
            "commit": as_str(entry.get("Commit")),
            "author": as_str(entry.get("Author")),
            "date": as_str(entry.get("Date")),
            "fingerprint": as_str(entry.get("Fingerprint")),
            "entropy": round(as_float(entry.get("Entropy"), 0.0), 3),
            "redacted": True,
            "note": "gitleaks ran with --redact; the secret value is deliberately not stored",
        },
    )


# =====================================================================================
# bandit
# =====================================================================================


class BanditTool(CommandTool):
    """``bandit`` static analysis for Python security patterns."""

    name = "bandit"
    capability = Capability.SECURITY_SCAN
    category = ToolCategory.STATIC_SECURITY
    description = (
        "Runs bandit recursively over the checkout and maps each issue to a finding using "
        "bandit's own severity and confidence fields."
    )
    version_argv: tuple[str, ...] = ("bandit", "--version")
    ecosystems = frozenset({"python", "bandit"})
    marker_files = frozenset({"bandit.yaml", "bandit.yml", ".bandit"})
    finding_exit_codes = frozenset({_EXIT_FINDINGS})

    def applicability_reason(self, context: ToolContext) -> str | None:
        if not self.enabled:
            return "tool is disabled by configuration"
        if declares_any(context, _PYTHON_MANIFESTS, ("[tool.bandit", "bandit")):
            return None
        return super().applicability_reason(context)

    def build_argv(self, context: ToolContext) -> Sequence[str]:
        override = context.override(f"security.custom.{self.name}")
        if override is not None:
            return list(override)
        return ["bandit", "-r", ".", "-f", "json", "-q"]

    def interpret(self, outcome: CommandOutcome, context: ToolContext) -> ToolOutcome:
        broken = launch_failure(self.name, outcome)
        if broken is not None:
            return broken

        exit_code = outcome.exit_code
        assert exit_code is not None  # launch_failure() guarantees an exit code
        value, _error = parse_json(outcome.read_stdout(limit_bytes=8_000_000))
        if value is None:
            if exit_code == _EXIT_OK:
                return ToolOutcome(
                    status=ToolStatus.PASSED,
                    narrative=(
                        "bandit exited 0 but produced no parseable JSON report; an exit of 0 "
                        "means it found no issues."
                    ),
                )
            return nonzero_without_evidence(self.name, outcome, what="bandit JSON report")

        if exit_code not in {_EXIT_OK, _EXIT_FINDINGS}:
            return nonzero_without_evidence(self.name, outcome, what="bandit JSON report")

        root = as_mapping(value)
        entries = as_sequence(root.get("results"))
        scan_errors = as_sequence(root.get("errors"))
        source = SourceCache(context)
        findings, dropped = cap_findings(
            [_bandit_finding(item, context, source) for item in entries]
        )
        summary = ToolSummary(
            values={
                "issues": len(entries),
                "scan_errors": len(scan_errors),
                "reported_findings": len(findings),
                "dropped_findings": dropped,
            }
        )

        if not findings and scan_errors:
            return ToolOutcome(
                status=ToolStatus.ERROR,
                summary=summary,
                narrative=(
                    f"bandit failed to scan {len(scan_errors)} file(s), so it produced no "
                    "trustworthy result."
                ),
                error=_bandit_error_detail(scan_errors),
                infrastructure_failure=True,
            )

        failed_narrative = f"bandit reported {len(findings)} security issue(s)."
        if dropped:
            failed_narrative += f" Only the first {len(findings)} are listed."
        return scanner_outcome(
            self.name,
            findings=findings,
            summary=summary,
            exit_code=exit_code,
            finding_exit_codes=self.finding_exit_codes,
            passed_narrative="bandit reported no security issues.",
            failed_narrative=failed_narrative,
        )


def _bandit_error_detail(scan_errors: Sequence[object]) -> str:
    if not scan_errors:
        return "bandit reported no detail"
    rendered = "; ".join(
        excerpt(as_str(as_mapping(item).get("reason")) or as_str(item), limit=120)
        for item in scan_errors[:3]
    )
    return excerpt(rendered, limit=1000) or "bandit reported scan errors without detail"


def _bandit_finding(
    item: object,
    context: ToolContext,
    source: SourceCache,
) -> ParsedFinding:
    entry = as_mapping(item)
    test_id = as_str(entry.get("test_id")) or "bandit"
    test_name = as_str(entry.get("test_name"))
    text = as_str(entry.get("issue_text")) or test_name or "bandit reported an issue"
    raw_path = as_str(entry.get("filename"))
    path = relative_workspace_path(context, raw_path) if raw_path else ""
    line_number = as_int(entry.get("line_number"), 0) or None
    line_range = [as_int(value, 0) for value in as_sequence(entry.get("line_range"))]
    end_line = max(line_range) if line_range else line_number

    return ParsedFinding(
        severity=severity_from_name(entry.get("issue_severity"), default=Severity.MEDIUM),
        title=excerpt(f"{test_id}: {test_name or text}", limit=200),
        description=excerpt(text, limit=600),
        rule_id=test_id,
        file=path or None,
        line_start=line_number,
        line_end=end_line,
        recommendation=(
            f"Review the pattern; bandit documents it at {as_str(entry.get('more_info'))}"
            if as_str(entry.get("more_info"))
            else "Review the pattern and either fix it or suppress it with a justification."
        ),
        metadata={
            "tool": "bandit",
            "test_id": test_id,
            "test_name": test_name,
            "confidence": as_str(entry.get("issue_confidence")),
            "line_range": ",".join(str(number) for number in line_range),
            "more_info": as_str(entry.get("more_info")),
            "excerpt": source.line(path or None, line_number),
        },
    )


# =====================================================================================
# semgrep
# =====================================================================================


class SemgrepTool(CommandTool):
    """``semgrep scan`` with either the community rule set or a local configuration."""

    name = "semgrep"
    capability = Capability.SECURITY_SCAN
    category = ToolCategory.STATIC_SECURITY
    description = (
        "Runs semgrep and maps every rule match to a finding. The default ``--config auto`` "
        "requires network access to semgrep.dev, so the tool is skipped when the sandbox "
        "network is disabled."
    )
    version_argv: tuple[str, ...] = ("semgrep", "--version")
    ecosystems = frozenset()
    marker_files = frozenset({".semgrep.yml", ".semgrep.yaml", "semgrep.yml", "semgrep.yaml"})

    def _local_config(self, context: ToolContext) -> str | None:
        for candidate in sorted(self.marker_files):
            if context.workspace.exists(candidate):
                return candidate
        return None

    def applicability_reason(self, context: ToolContext) -> str | None:
        if not self.enabled:
            return "tool is disabled by configuration"
        local = self._local_config(context)
        if local is not None:
            # A repository-provided rule set is scanned offline; no network is required.
            return None

        mode = context.sandbox.policy.network_mode
        if mode is NetworkMode.NONE:
            return (
                "semgrep --config auto downloads the community rule set from semgrep.dev, "
                "but the sandbox network mode is 'none'. Add semgrep.dev to the sandbox "
                "allowlist and run with SCOUT_SANDBOX_NETWORK=allowlist to enable it, or "
                "commit a local .semgrep.yml."
            )
        if mode is NetworkMode.ALLOWLIST:
            allowed = set(context.sandbox.policy.allowed_hosts)
            if not allowed & _SEMGREP_HOSTS:
                expected = ", ".join(sorted(_SEMGREP_HOSTS))
                return (
                    "semgrep --config auto requires network access to semgrep.dev, which is "
                    f"not in the sandbox allowlist (expected one of: {expected})"
                )
        return None

    def build_argv(self, context: ToolContext) -> Sequence[str]:
        override = context.override(f"security.custom.{self.name}")
        if override is not None:
            return list(override)
        config = self._local_config(context) or "auto"
        return ["semgrep", "scan", "--json", "--quiet", "--config", config]

    def interpret(self, outcome: CommandOutcome, context: ToolContext) -> ToolOutcome:
        broken = launch_failure(self.name, outcome)
        if broken is not None:
            return broken

        exit_code = outcome.exit_code
        assert exit_code is not None  # launch_failure() guarantees an exit code
        value, _error = parse_json(outcome.read_stdout(limit_bytes=8_000_000))
        if value is None:
            return nonzero_without_evidence(self.name, outcome, what="semgrep JSON report")

        root = as_mapping(value)
        entries = as_sequence(root.get("results"))
        scan_errors = as_sequence(root.get("errors"))
        source = SourceCache(context)
        findings, dropped = cap_findings(
            [_semgrep_finding(item, context, source) for item in entries]
        )
        summary = ToolSummary(
            values={
                "matches": len(entries),
                "scan_errors": len(scan_errors),
                "reported_findings": len(findings),
                "dropped_findings": dropped,
            }
        )

        if findings:
            narrative = f"semgrep matched {len(findings)} rule(s)."
            if dropped:
                narrative += f" Only the first {len(findings)} are listed."
            # semgrep exits 0 even when rules match unless --error is passed, so the
            # presence of matches — not the exit code — determines the status.
            return ToolOutcome(
                status=ToolStatus.FAILED,
                summary=summary,
                narrative=narrative,
                findings=findings,
            )

        if exit_code != _EXIT_OK:
            return ToolOutcome(
                status=ToolStatus.ERROR,
                summary=summary,
                narrative=(
                    f"semgrep exited with status {exit_code} and reported no matches; the "
                    "scan did not complete."
                ),
                error=excerpt(
                    "; ".join(
                        as_str(as_mapping(item).get("message")) or as_str(item)
                        for item in scan_errors[:3]
                    ),
                    limit=1000,
                )
                or f"semgrep exited with status {exit_code}",
                infrastructure_failure=True,
            )

        if scan_errors:
            return ToolOutcome(
                status=ToolStatus.ERROR,
                summary=summary,
                narrative=(
                    f"semgrep reported no matches but recorded {len(scan_errors)} scan "
                    "error(s), so the result is incomplete."
                ),
                error=_semgrep_error_detail(scan_errors),
                infrastructure_failure=True,
            )

        return ToolOutcome(
            status=ToolStatus.PASSED,
            summary=summary,
            narrative="semgrep reported no matches.",
        )


def _semgrep_error_detail(scan_errors: Sequence[object]) -> str:
    rendered = "; ".join(
        as_str(as_mapping(item).get("message")) or as_str(item) for item in scan_errors[:3]
    )
    return excerpt(rendered, limit=1000) or "semgrep reported scan errors without detail"


def _semgrep_finding(
    item: object,
    context: ToolContext,
    source: SourceCache,
) -> ParsedFinding:
    entry = as_mapping(item)
    check_id = as_str(entry.get("check_id")) or "semgrep"
    extra = as_mapping(entry.get("extra"))
    message = as_str(extra.get("message")) or check_id
    start = as_mapping(entry.get("start"))
    end = as_mapping(entry.get("end"))
    raw_path = as_str(entry.get("path"))
    path = relative_workspace_path(context, raw_path) if raw_path else ""
    line_number = as_int(start.get("line"), 0) or None
    end_line = as_int(end.get("line"), 0) or line_number
    metadata = as_mapping(extra.get("metadata"))

    return ParsedFinding(
        severity=severity_from_name(extra.get("severity"), default=Severity.MEDIUM),
        title=excerpt(f"{check_id}: {message}", limit=200),
        description=excerpt(message, limit=600),
        rule_id=check_id,
        file=path or None,
        line_start=line_number,
        line_end=end_line,
        recommendation=(
            "Review the matched pattern against the rule's guidance and either fix the code "
            "or record an explicit suppression."
        ),
        metadata={
            "tool": "semgrep",
            "check_id": check_id,
            "category": as_str(metadata.get("category")),
            "confidence": as_str(metadata.get("confidence")),
            "impact": as_str(metadata.get("impact")),
            "owasp": ",".join(as_str(value) for value in as_sequence(metadata.get("owasp"))),
            "cwe": ",".join(as_str(value) for value in as_sequence(metadata.get("cwe"))),
            "fingerprint": as_str(extra.get("fingerprint")),
            "excerpt": excerpt(as_str(extra.get("lines")), limit=400)
            or source.line(path or None, line_number),
        },
    )


# =====================================================================================
# pip-audit
# =====================================================================================


class PipAuditTool(CommandTool):
    """Known-vulnerability scanning for Python dependencies."""

    name = "pip-audit"
    capability = Capability.DEPENDENCY_SCAN
    category = ToolCategory.DEPENDENCY
    description = (
        "Runs pip-audit over the installed environment and the declared requirements, "
        "emitting one finding per advisory with the fixed version in the recommendation."
    )
    version_argv: tuple[str, ...] = ("pip-audit", "--version")
    ecosystems = frozenset({"python", "pip", "poetry", "uv"})
    marker_files = frozenset(
        {"requirements.txt", "pyproject.toml", "Pipfile", "poetry.lock", "uv.lock", "setup.py"}
    )
    finding_exit_codes = frozenset({_EXIT_FINDINGS})

    def build_argv(self, context: ToolContext) -> Sequence[str]:
        override = context.override(f"security.custom.{self.name}")
        if override is not None:
            return list(override)
        return ["pip-audit", "--format", "json", "--progress-spinner", "off"]

    def interpret(self, outcome: CommandOutcome, _context: ToolContext) -> ToolOutcome:
        broken = launch_failure(self.name, outcome)
        if broken is not None:
            return broken

        exit_code = outcome.exit_code
        assert exit_code is not None  # launch_failure() guarantees an exit code
        value, _error = parse_json(outcome.read_stdout(limit_bytes=8_000_000))
        if value is None:
            if exit_code == _EXIT_OK:
                return ToolOutcome(
                    status=ToolStatus.PASSED,
                    narrative=(
                        "pip-audit exited 0 but produced no parseable JSON report; an exit "
                        "of 0 means it found no vulnerabilities."
                    ),
                )
            return nonzero_without_evidence(self.name, outcome, what="pip-audit JSON report")

        if exit_code not in {_EXIT_OK, _EXIT_FINDINGS}:
            return nonzero_without_evidence(self.name, outcome, what="pip-audit JSON report")

        dependencies = _pip_audit_dependencies(value)
        findings, dropped = cap_findings(
            [finding for dependency in dependencies for finding in _pip_audit_findings(dependency)]
        )
        summary = ToolSummary(
            values={
                "dependencies": len(dependencies),
                "vulnerabilities": len(findings) + dropped,
                "reported_findings": len(findings),
                "dropped_findings": dropped,
            }
        )
        failed_narrative = (
            f"pip-audit found {len(findings) + dropped} vulnerability/advisory pair(s) "
            f"across {len(dependencies)} dependencies."
        )
        if dropped:
            failed_narrative += f" Only the first {len(findings)} are listed."
        return scanner_outcome(
            self.name,
            findings=findings,
            summary=summary,
            exit_code=exit_code,
            finding_exit_codes=self.finding_exit_codes,
            passed_narrative=(
                f"pip-audit found no known vulnerabilities in {len(dependencies)} dependencies."
            ),
            failed_narrative=failed_narrative,
        )


def _pip_audit_dependencies(value: object) -> list[dict[str, object]]:
    """Return the dependency entries from either pip-audit JSON layout.

    Modern pip-audit emits ``{"dependencies": [...], "fixes": [...]}``; older releases
    emitted a bare list. Both are supported because the version in a sandbox image is not
    something Scout controls.
    """
    if isinstance(value, list):
        return [as_mapping(item) for item in value]
    return [as_mapping(item) for item in as_sequence(as_mapping(value).get("dependencies"))]


def _pip_audit_findings(dependency: dict[str, object]) -> list[ParsedFinding]:
    name = as_str(dependency.get("name")) or "<unknown>"
    version = as_str(dependency.get("version")) or "unknown"
    findings: list[ParsedFinding] = []
    for item in as_sequence(dependency.get("vulns")):
        entry = as_mapping(item)
        vuln_id = as_str(entry.get("id")) or "advisory"
        aliases = [as_str(value) for value in as_sequence(entry.get("aliases"))]
        fixed = [as_str(value) for value in as_sequence(entry.get("fix_versions"))]
        recommendation = (
            f"Upgrade {name} to {', '.join(fixed)} or later."
            if fixed
            else (
                f"No fixed version is published for {name} yet; evaluate a replacement "
                "or a mitigation."
            )
        )
        findings.append(
            ParsedFinding(
                severity=Severity.HIGH,
                title=f"{vuln_id} affects {name} {version}",
                description=excerpt(as_str(entry.get("description")), limit=600)
                or f"{vuln_id} affects the pinned version of {name}.",
                rule_id=vuln_id,
                recommendation=recommendation,
                metadata={
                    "tool": "pip-audit",
                    "package": name,
                    "installed_version": version,
                    "vulnerability_id": vuln_id,
                    "aliases": ",".join(aliases),
                    "fix_versions": ",".join(fixed),
                    # pip-audit does not publish a severity, so Scout records the
                    # uncertainty rather than inventing a number.
                    "severity_source": "unreported; Scout classifies known CVEs as HIGH",
                },
            )
        )
    return findings
