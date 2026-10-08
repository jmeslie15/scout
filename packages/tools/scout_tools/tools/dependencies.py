"""Dependency and filesystem vulnerability scanners.

Two tools:

* :class:`NpmAuditTool` — advisories affecting the packages in a ``package-lock.json``.
* :class:`TrivyTool` — CVEs in dependencies plus misconfigurations in infrastructure
  files, from a single filesystem scan.

Both are *advisory readers*: they report what a database says about the versions a
repository pins, which is why the recommendation always names the fixed version when one
is known. The status distinction is the same one every tool in this package makes —
"the scanner found advisories" is ``FAILED``; "the scanner could not run" is ``ERROR``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from scout_domain.enums import Capability, Severity, ToolStatus
from scout_domain.models.tool import ToolSummary
from scout_tools.base import CommandTool, ParsedFinding, ToolCategory, ToolOutcome
from scout_tools.tools._common import (
    as_float,
    as_int,
    as_mapping,
    as_sequence,
    as_str,
    cap_findings,
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

__all__ = ["NpmAuditTool", "TrivyTool"]

_EXIT_OK: Final[int] = 0
_EXIT_FINDINGS: Final[int] = 1

_MISSING_RECOMMENDATION: Final[str] = (
    "No fixed version is published yet; evaluate whether the affected code path is "
    "reachable and whether the package can be replaced or removed."
)


# =====================================================================================
# npm audit
# =====================================================================================


class NpmAuditTool(CommandTool):
    """``npm audit --json`` over a lockfile.

    npm exits non-zero when vulnerabilities exist, which is the "found problems" exit
    code rather than a failure of npm itself; an *operational* failure (missing
    lockfile, unreachable registry) is also non-zero but is reported by npm as
    ``{"error": ...}`` and is treated as an infrastructure failure.
    """

    name = "npm-audit"
    capability = Capability.DEPENDENCY_SCAN
    category = ToolCategory.DEPENDENCY
    description = (
        "Runs npm audit with JSON output and converts each vulnerable package into a "
        "finding carrying the advisory title, severity, and available fix."
    )
    version_argv: tuple[str, ...] = ("npm", "--version")
    ecosystems = frozenset({"npm", "javascript", "typescript"})
    marker_files = frozenset(
        {"package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml"}
    )
    finding_exit_codes = frozenset({_EXIT_FINDINGS})

    def build_argv(self, context: ToolContext) -> Sequence[str]:
        override = context.override(f"security.custom.{self.name}")
        if override is not None:
            return list(override)
        return ["npm", "audit", "--json", "--audit-level=low"]

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
                        "npm audit exited 0 but produced no parseable JSON report; an exit "
                        "of 0 means it found no vulnerabilities."
                    ),
                )
            return nonzero_without_evidence(self.name, outcome, what="npm audit JSON report")

        root = as_mapping(value)
        failure = _npm_operational_error(root)
        if failure is not None:
            return ToolOutcome(
                status=ToolStatus.ERROR,
                narrative=(
                    f"npm audit could not complete: {failure}. This is an infrastructure "
                    "failure, not a finding about the change."
                ),
                error=excerpt(failure, limit=1000),
                infrastructure_failure=True,
            )

        if exit_code not in {_EXIT_OK, _EXIT_FINDINGS}:
            return nonzero_without_evidence(self.name, outcome, what="npm audit JSON report")

        vulnerabilities = as_mapping(root.get("vulnerabilities"))
        findings, dropped = cap_findings(
            [
                _npm_finding(name, as_mapping(detail))
                for name, detail in sorted(vulnerabilities.items())
            ]
        )
        summary = ToolSummary(
            values={
                "vulnerable_packages": len(vulnerabilities),
                "reported_findings": len(findings),
                "dropped_findings": dropped,
                "critical": _count_severity(vulnerabilities, "critical"),
                "high": _count_severity(vulnerabilities, "high"),
            }
        )
        failed_narrative = (
            f"npm audit reported {len(vulnerabilities)} vulnerable package(s); "
            f"{len(findings)} became findings."
        )
        if dropped:
            failed_narrative += f" Only the first {len(findings)} are listed."
        return scanner_outcome(
            self.name,
            findings=findings,
            summary=summary,
            exit_code=exit_code,
            finding_exit_codes=self.finding_exit_codes,
            passed_narrative="npm audit found no known vulnerabilities.",
            failed_narrative=failed_narrative,
        )


def _npm_operational_error(root: dict[str, object]) -> str | None:
    """Return npm's own error text, when the run failed for an operational reason.

    ``npm audit --json`` reports a missing lockfile, an unreachable registry, or an
    unsupported project as ``{"error": {...}}`` with a non-zero exit. That status is not
    a vulnerability report and must not be turned into one.
    """
    error = root.get("error")
    if not error:
        return None
    if isinstance(error, str):
        return error
    detail = as_mapping(error)
    summary = as_str(detail.get("summary"))
    message = as_str(detail.get("detail")) or as_str(detail.get("message"))
    return excerpt(": ".join(part for part in (summary, message) if part), limit=1000)


def _count_severity(vulnerabilities: dict[str, object], wanted: str) -> int:
    return sum(
        1
        for detail in vulnerabilities.values()
        if as_str(as_mapping(detail).get("severity")).lower() == wanted
    )


def _npm_finding(package: str, detail: dict[str, object]) -> ParsedFinding:
    severity_name = as_str(detail.get("severity"))
    titles: list[str] = []
    advisory_url = ""
    cwe: list[str] = []
    cvss = 0.0
    for item in as_sequence(detail.get("via")):
        if isinstance(item, str):
            titles.append(item)
            continue
        advisory = as_mapping(item)
        title = as_str(advisory.get("title"))
        if title:
            titles.append(title)
        advisory_url = advisory_url or as_str(advisory.get("url"))
        cwe.extend(as_str(value) for value in as_sequence(advisory.get("cwe")))
        cvss = cvss or _cvss_score(advisory.get("cvss"))

    fix = detail.get("fixAvailable")
    recommendation = _npm_recommendation(package, fix)
    description = "; ".join(titles) or f"{package} is affected by a known advisory."

    return ParsedFinding(
        severity=severity_from_name(severity_name, default=Severity.MEDIUM),
        title=f"{package}: {severity_name or 'unknown'} severity advisory",
        description=excerpt(description, limit=600),
        rule_id=", ".join(cwe) or None,
        recommendation=recommendation,
        metadata={
            "tool": "npm-audit",
            "package": package,
            "range": as_str(detail.get("range")),
            "is_direct": bool(detail.get("isDirect")),
            "severity": severity_name,
            "advisories": excerpt("; ".join(titles), limit=600),
            "url": advisory_url,
            "cwe": ",".join(cwe),
            "cvss": round(cvss, 2),
            "fix_available": bool(fix),
        },
    )


def _cvss_score(value: object) -> float:
    """npm reports CVSS as ``{"score": 7.4, "vectorString": ...}``; older versions as a number."""
    if isinstance(value, (dict, list, tuple)):
        return as_float(as_mapping(value).get("score"), 0.0)
    return as_float(value, 0.0)


def _npm_recommendation(package: str, fix: object) -> str:
    if isinstance(fix, dict):
        version = as_str(fix.get("version")) or "a fixed version"
        target = as_str(fix.get("name")) or package
        return f"Upgrade {target} to {version} (``npm audit fix`` applies this automatically)."
    if fix is True:
        return f"A fix is available for {package}; run ``npm audit fix``."
    return f"No fix is available for {package} yet; {_MISSING_RECOMMENDATION}"


# =====================================================================================
# trivy
# =====================================================================================


class TrivyTool(CommandTool):
    """``trivy fs``: dependency CVEs plus infrastructure misconfigurations."""

    name = "trivy"
    capability = Capability.SECURITY_SCAN
    category = ToolCategory.STATIC_SECURITY
    description = (
        "Runs a trivy filesystem scan for vulnerabilities and misconfigurations and turns "
        "both result kinds into findings. Requires the trivy binary and either a trivy "
        "configuration in the repository or 'trivy' listed in security.tools."
    )
    version_argv: tuple[str, ...] = ("trivy", "--version")
    ecosystems = frozenset()
    marker_files = frozenset({"trivy.yaml", "trivy.yml", ".trivyignore"})

    @property
    def report_file(self) -> str:
        """Workspace-relative path trivy writes its JSON report to."""
        return report_path("trivy.json")

    def applicability_reason(self, context: ToolContext) -> str | None:
        if not self.enabled:
            return "tool is disabled by configuration"
        if any(context.workspace.exists(marker) for marker in self.marker_files):
            return None
        configured = {name.strip().lower() for name in context.repository_config.security.tools}
        if self.name in configured:
            return None
        return (
            "trivy is not enabled: neither a trivy configuration file nor an entry for "
            "'trivy' in security.tools was found. Note that the trivy binary and its "
            "vulnerability database are checked inside the sandbox at execution time, "
            "because applicability is decided before the sandbox is asked anything."
        )

    def build_argv(self, context: ToolContext) -> Sequence[str]:
        override = context.override(f"security.custom.{self.name}")
        if override is not None:
            return list(override)
        ensure_report_directory(context)
        return [
            "trivy",
            "fs",
            "--format",
            "json",
            "--quiet",
            "--scanners",
            "vuln,misconfig",
            "--output",
            self.report_file,
            ".",
        ]

    def interpret(self, outcome: CommandOutcome, context: ToolContext) -> ToolOutcome:
        broken = launch_failure(self.name, outcome)
        if broken is not None:
            return broken

        exit_code = outcome.exit_code
        assert exit_code is not None  # launch_failure() guarantees an exit code
        text = read_workspace_text(context, self.report_file)
        stored_report = self.report_file if text is not None else None
        if text is None:
            text = outcome.read_stdout(limit_bytes=8_000_000)
        value, _error = parse_json(text)
        if value is None:
            if exit_code == _EXIT_OK:
                return ToolOutcome(
                    status=ToolStatus.PASSED,
                    narrative=(
                        "trivy exited 0 but produced no parseable JSON report; an exit of 0 "
                        "means it reported no vulnerabilities or misconfigurations."
                    ),
                )
            return nonzero_without_evidence(self.name, outcome, what="trivy JSON report")

        results = as_sequence(as_mapping(value).get("Results"))
        findings, dropped, counts = _trivy_findings(results, context)
        summary = ToolSummary(
            values={
                "results": len(results),
                "vulnerabilities": counts[0],
                "misconfigurations": counts[1],
                "reported_findings": len(findings),
                "dropped_findings": dropped,
            }
        )

        if exit_code not in {_EXIT_OK, _EXIT_FINDINGS}:
            return nonzero_without_evidence(self.name, outcome, what="trivy JSON report")

        failed_narrative = (
            f"trivy reported {counts[0]} vulnerabilit(ies) and {counts[1]} "
            f"misconfiguration(s); {len(findings)} became findings."
        )
        if dropped:
            failed_narrative += f" Only the first {len(findings)} are listed."
        # trivy exits 0 by default even when it finds something, so the presence of
        # results — not the exit code — determines the status.
        return scanner_outcome(
            self.name,
            findings=findings,
            summary=summary,
            exit_code=exit_code,
            finding_exit_codes=self.finding_exit_codes,
            passed_narrative="trivy reported no vulnerabilities or misconfigurations.",
            failed_narrative=failed_narrative,
            report_path=stored_report,
        )


def _trivy_findings(
    results: Sequence[object],
    context: ToolContext,
) -> tuple[tuple[ParsedFinding, ...], int, tuple[int, int]]:
    findings: list[ParsedFinding] = []
    vulnerabilities = 0
    misconfigurations = 0
    for raw_result in results:
        result = as_mapping(raw_result)
        target = relative_workspace_path(context, as_str(result.get("Target")))
        for raw_vulnerability in as_sequence(result.get("Vulnerabilities")):
            vulnerabilities += 1
            findings.append(_trivy_vulnerability(raw_vulnerability, target))
        for raw_misconfiguration in as_sequence(result.get("Misconfigurations")):
            misconfigurations += 1
            findings.append(_trivy_misconfiguration(raw_misconfiguration, target))
    capped, dropped = cap_findings(findings)
    return capped, dropped, (vulnerabilities, misconfigurations)


def _trivy_vulnerability(item: object, target: str) -> ParsedFinding:
    entry = as_mapping(item)
    vuln_id = as_str(entry.get("VulnerabilityID")) or "trivy"
    package = as_str(entry.get("PkgName")) or "<unknown package>"
    installed = as_str(entry.get("InstalledVersion"))
    fixed = as_str(entry.get("FixedVersion"))
    title = as_str(entry.get("Title")) or f"{vuln_id} affects {package}"

    return ParsedFinding(
        severity=severity_from_name(entry.get("Severity"), default=Severity.MEDIUM),
        title=excerpt(f"{vuln_id}: {title}", limit=200),
        description=excerpt(as_str(entry.get("Description")) or title, limit=600),
        rule_id=vuln_id,
        file=target or None,
        recommendation=(
            f"Upgrade {package} from {installed or 'the pinned version'} to {fixed} or later."
            if fixed
            else f"{package} has no fixed version in the advisory database; "
            f"{_MISSING_RECOMMENDATION}"
        ),
        metadata={
            "tool": "trivy",
            "kind": "vulnerability",
            "package": package,
            "installed_version": installed,
            "fixed_version": fixed,
            "primary_url": as_str(entry.get("PrimaryURL")),
            "cwe": ",".join(as_str(value) for value in as_sequence(entry.get("CweIDs"))),
        },
    )


def _trivy_misconfiguration(item: object, target: str) -> ParsedFinding:
    entry = as_mapping(item)
    cause = as_mapping(entry.get("CauseMetadata"))
    start = as_int(cause.get("StartLine"), 0) or None
    end = as_int(cause.get("EndLine"), 0) or start
    rule_id = as_str(entry.get("ID")) or as_str(entry.get("AVDID")) or "trivy-misconfig"
    title = as_str(entry.get("Title")) or "misconfiguration detected"

    return ParsedFinding(
        severity=severity_from_name(entry.get("Severity"), default=Severity.MEDIUM),
        title=excerpt(f"{rule_id}: {title}", limit=200),
        description=excerpt(
            as_str(entry.get("Message")) or as_str(entry.get("Description")) or title,
            limit=600,
        ),
        rule_id=rule_id,
        file=target or None,
        line_start=start,
        line_end=end,
        recommendation=as_str(entry.get("Resolution"))
        or "Apply the hardening described by the rule and re-scan.",
        metadata={
            "tool": "trivy",
            "kind": "misconfiguration",
            "avd_id": as_str(entry.get("AVDID")),
            "status": as_str(entry.get("Status")),
            "references": excerpt(
                "; ".join(as_str(value) for value in as_sequence(entry.get("References"))),
                limit=400,
            ),
        },
    )
