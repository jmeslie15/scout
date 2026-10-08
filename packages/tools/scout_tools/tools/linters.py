"""Linters, formatters, and type checkers (spec section 65).

Five tools live here because they share one shape: run a program that already knows how
to report problems machine-readably, then translate its vocabulary into
:class:`~scout_tools.base.ParsedFinding`. Two rules are applied consistently:

* **Structured output over prose.** ruff and eslint are asked for JSON; mypy and tsc are
  parsed with anchored regular expressions because neither offers a stable machine format
  (mypy has ``--output=json`` only in recent versions and it is not universally
  available, so the text format is the portable choice).
* **"Found problems" is not "broke".** Exit statuses that a tool documents as "problems
  were reported" produce ``FAILED``; anything else non-zero produces ``ERROR`` with
  ``infrastructure_failure=True``. A tool that exits non-zero without emitting anything
  Scout can parse is an infrastructure failure, never a finding.

Each finding carries the tool's own rule identifier and, where the workspace is
readable, the offending source line, so the dashboard can render something better than a
file name and a line number.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Final

from scout_domain.enums import Capability, Severity, ToolStatus
from scout_domain.models.tool import ToolSummary
from scout_tools.base import CommandTool, ParsedFinding, ToolCategory, ToolOutcome
from scout_tools.tools._common import (
    SourceCache,
    as_int,
    as_mapping,
    as_sequence,
    as_str,
    cap_findings,
    declares_any,
    excerpt,
    launch_failure,
    missing_marker_reason,
    nonzero_without_evidence,
    parse_json,
    relative_workspace_path,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from scout_sandbox.command import CommandOutcome
    from scout_tools.base import ToolContext

__all__ = [
    "EslintTool",
    "MypyTool",
    "RuffFormatTool",
    "RuffTool",
    "TscTool",
]

# -- Shared exit codes ---------------------------------------------------------------
_EXIT_OK: Final[int] = 0
_EXIT_FINDINGS: Final[int] = 1
_EXIT_TOOL_ERROR: Final[int] = 2

_PYTHON_MANIFESTS: Final[tuple[str, ...]] = (
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-dev.in",
    "setup.cfg",
    "tox.ini",
)

#: How far into an argument vector ``_invokes`` will look for the program name (covers
#: ``npx --no-install eslint`` and ``python -m mypy``).
_MAX_PROGRAM_LOOKAHEAD: Final[int] = 4

#: How many file names are named in a narrative before it is summarised.
_MAX_LISTED_FILES: Final[int] = 5

_ESLINT_CONFIG_MARKERS: Final[frozenset[str]] = frozenset(
    {
        ".eslintrc",
        ".eslintrc.js",
        ".eslintrc.cjs",
        ".eslintrc.mjs",
        ".eslintrc.json",
        ".eslintrc.yaml",
        ".eslintrc.yml",
        "eslint.config.js",
        "eslint.config.cjs",
        "eslint.config.mjs",
        "eslint.config.ts",
        "eslint.config.mts",
    }
)


# =====================================================================================
# ruff check
# =====================================================================================

#: ruff rule prefixes that indicate a security problem (the bandit-derived ``S`` rules).
_SECURITY_RULE_CODE: Final[re.Pattern[str]] = re.compile(r"^S\d")

#: ``S`` rules that describe an embedded secret rather than a risky pattern.
_HIGH_SECURITY_RULES: Final[frozenset[str]] = frozenset({"S105", "S106", "S107"})


class RuffTool(CommandTool):
    """``ruff check`` with JSON output."""

    name = "ruff"
    capability = Capability.LINT_RUN
    category = ToolCategory.LINT
    description = (
        "Runs ruff check with JSON output and turns each diagnostic into a finding "
        "carrying its rule code, location, and the offending source line."
    )
    version_argv: tuple[str, ...] = ("ruff", "--version")
    ecosystems = frozenset({"python", "ruff"})
    marker_files = frozenset({"ruff.toml", ".ruff.toml"})
    finding_exit_codes = frozenset({_EXIT_FINDINGS})

    def applicability_reason(self, context: ToolContext) -> str | None:
        if not self.enabled:
            return "tool is disabled by configuration"
        if declares_any(context, _PYTHON_MANIFESTS, ("[tool.ruff", "ruff")):
            return None
        return super().applicability_reason(context)

    def build_argv(self, context: ToolContext) -> Sequence[str]:
        override = context.override("lint")
        if override is None:
            return ["ruff", "check", "--output-format=json", "."]
        argv = list(override)
        if _invokes(argv, "ruff") and not any(part.startswith("--output-format") for part in argv):
            # Scout needs the JSON form to attribute findings to rules; a repository that
            # declared ``ruff check .`` still gets parsed output.
            argv.append("--output-format=json")
        return argv

    def interpret(self, outcome: CommandOutcome, context: ToolContext) -> ToolOutcome:
        broken = launch_failure(self.name, outcome)
        if broken is not None:
            return broken

        exit_code = outcome.exit_code
        assert exit_code is not None  # launch_failure() guarantees an exit code
        value, error = parse_json(outcome.read_stdout(limit_bytes=8_000_000))
        if value is None:
            return _unreadable_json_outcome(self.name, outcome, error=error, exit_code=exit_code)

        entries = as_sequence(value)
        source = SourceCache(context)
        parsed: list[ParsedFinding] = []
        for item in entries:
            finding = _ruff_finding(item, context, source)
            if finding is not None:
                parsed.append(finding)
        findings, dropped = cap_findings(parsed)
        summary = ToolSummary(
            values={
                "violations": len(entries),
                "reported_findings": len(findings),
                "dropped_findings": dropped,
            }
        )

        if exit_code not in {_EXIT_OK, _EXIT_FINDINGS}:
            return nonzero_without_evidence(self.name, outcome, what="ruff JSON output")

        if findings:
            narrative = _lint_narrative("ruff", len(entries), len(findings), dropped)
            return ToolOutcome(
                status=ToolStatus.FAILED,
                summary=summary,
                narrative=narrative,
                findings=findings,
            )

        if exit_code == _EXIT_FINDINGS:
            return ToolOutcome(
                status=ToolStatus.ERROR,
                summary=summary,
                narrative=(
                    "ruff exited 1 but its JSON output contained no violations, so Scout "
                    "cannot attribute the failure to the change."
                ),
                error="ruff exited with status 1 and an empty violation list",
                infrastructure_failure=True,
            )

        return ToolOutcome(
            status=ToolStatus.PASSED,
            summary=summary,
            narrative="ruff reported no lint violations.",
        )


def _ruff_severity(code: str) -> Severity:
    """Map a ruff rule code onto a Scout severity.

    ``E9``/``F7``/``F8`` are syntax and parse errors (the file does not even compile), so
    they outrank ordinary lint. ``S`` rules are the bandit-derived security set; the ones
    about embedded secrets are treated as high, the rest as medium. Everything else is a
    maintainability signal.
    """
    upper = code.upper()
    if upper.startswith(("E9", "F7", "F8")):
        return Severity.HIGH
    if _SECURITY_RULE_CODE.match(upper):
        return Severity.HIGH if upper in _HIGH_SECURITY_RULES else Severity.MEDIUM
    if upper.startswith(("E", "F")):
        return Severity.LOW
    return Severity.INFO


def _ruff_finding(
    item: object,
    context: ToolContext,
    source: SourceCache,
) -> ParsedFinding | None:
    entry = as_mapping(item)
    code = as_str(entry.get("code"))
    message = as_str(entry.get("message"))
    if not message:
        return None

    location = as_mapping(entry.get("location"))
    end_location = as_mapping(entry.get("end_location"))
    row = as_int(location.get("row"), 0) or None
    end_row = as_int(end_location.get("row"), 0) or row
    raw_filename = as_str(entry.get("filename"))
    path = relative_workspace_path(context, raw_filename) if raw_filename else None

    return ParsedFinding(
        severity=_ruff_severity(code),
        title=excerpt(f"{code or 'ruff'}: {message}", limit=200),
        description=excerpt(message, limit=600),
        rule_id=code or None,
        file=path,
        line_start=row,
        line_end=end_row,
        recommendation=(
            "Run ``ruff check --fix`` for mechanical fixes, or adjust the code to satisfy the rule."
        ),
        metadata={
            "tool": "ruff",
            "code": code,
            "column": as_int(location.get("column"), 0),
            "end_column": as_int(end_location.get("column"), 0),
            "end_row": end_row,
            "has_autofix": entry.get("fix") is not None,
            "url": as_str(entry.get("url")),
            "excerpt": source.line(path, row),
        },
    )


# =====================================================================================
# ruff format
# =====================================================================================

_WOULD_REFORMAT: Final[re.Pattern[str]] = re.compile(r"^Would reformat:\s+(?P<path>\S.*?)\s*$")
#: ``--- path`` / ``+++ path`` headers in the unified diff ruff prints with ``--diff``.
_DIFF_HEADER: Final[re.Pattern[str]] = re.compile(r"^(?:---|\+\+\+)\s+(?P<path>[^\t\n]+?)\s*$")


class RuffFormatTool(CommandTool):
    """``ruff format --check --diff``: which files the formatter would rewrite."""

    name = "ruff-format"
    capability = Capability.LINT_RUN
    category = ToolCategory.FORMAT
    description = (
        "Runs ruff format in check mode and reports every file that is not formatted, "
        "with the diff hunk as evidence."
    )
    version_argv: tuple[str, ...] = ("ruff", "--version")
    ecosystems = frozenset({"python", "ruff"})
    marker_files = RuffTool.marker_files
    finding_exit_codes = frozenset({_EXIT_FINDINGS})

    def applicability_reason(self, context: ToolContext) -> str | None:
        if not self.enabled:
            return "tool is disabled by configuration"
        if declares_any(context, _PYTHON_MANIFESTS, ("[tool.ruff", "[tool.ruff.format", "ruff")):
            return None
        return super().applicability_reason(context)

    def build_argv(self, context: ToolContext) -> Sequence[str]:
        override = context.override("format")
        if override is None:
            return ["ruff", "format", "--check", "--diff", "."]
        argv = list(override)
        if _invokes(argv, "ruff"):
            # Ask the declared runner for a checkable, diffable run without changing what
            # the repository asked to format.
            for flag in ("--check", "--diff"):
                if flag not in argv:
                    argv.append(flag)
            if "." not in argv:
                argv.append(".")
        return argv

    def interpret(self, outcome: CommandOutcome, context: ToolContext) -> ToolOutcome:
        broken = launch_failure(self.name, outcome)
        if broken is not None:
            return broken

        exit_code = outcome.exit_code
        assert exit_code is not None  # launch_failure() guarantees an exit code
        if exit_code not in {_EXIT_OK, _EXIT_FINDINGS}:
            return nonzero_without_evidence(self.name, outcome, what="ruff format output")

        stdout = outcome.read_stdout(limit_bytes=8_000_000)
        stderr = outcome.read_stderr(limit_bytes=1_000_000)
        unformatted, diffs = _parse_format_output(f"{stdout}\n{stderr}", context)

        summary = ToolSummary(values={"unformatted_files": len(unformatted)})
        if exit_code == _EXIT_FINDINGS and not unformatted:
            return ToolOutcome(
                status=ToolStatus.ERROR,
                summary=summary,
                narrative=(
                    "ruff format exited 1 but named no unformatted file, so Scout cannot "
                    "tell which file the repository wants reformatted."
                ),
                error="ruff format exited with status 1 and reported no files",
                infrastructure_failure=True,
            )

        if not unformatted:
            return ToolOutcome(
                status=ToolStatus.PASSED,
                summary=summary,
                narrative="ruff format found no formatting differences.",
            )

        findings, dropped = cap_findings(
            [_format_finding(path, diffs.get(path, "")) for path in unformatted]
        )
        narrative = (
            f"ruff format would reformat {len(unformatted)} file(s): "
            f"{', '.join(unformatted[:_MAX_LISTED_FILES])}"
        )
        if len(unformatted) > _MAX_LISTED_FILES:
            narrative += f" and {len(unformatted) - _MAX_LISTED_FILES} more"
        narrative += "."
        if dropped:
            narrative += f" Only the first {len(findings)} are listed."
        return ToolOutcome(
            status=ToolStatus.FAILED,
            summary=summary,
            narrative=narrative,
            findings=findings,
        )


def _parse_format_output(text: str, context: ToolContext) -> tuple[list[str], dict[str, str]]:
    """Return the unformatted files and, where available, their diff hunks."""
    files: dict[str, None] = {}
    diffs: dict[str, list[str]] = {}
    current: str | None = None
    for line in text.splitlines():
        reformatted = _WOULD_REFORMAT.match(line)
        if reformatted is not None:
            path = relative_workspace_path(context, reformatted.group("path"))
            files.setdefault(path, None)
            current = None
            continue
        header = _DIFF_HEADER.match(line)
        if header is not None:
            raw = header.group("path")
            if raw == "/dev/null":
                continue
            path = relative_workspace_path(context, raw)
            files.setdefault(path, None)
            current = path
            diffs.setdefault(path, []).append(line)
            continue
        if current is not None:
            diffs[current].append(line)
    rendered = {path: excerpt("\n".join(lines), limit=600) for path, lines in diffs.items()}
    return list(files), rendered


def _format_finding(path: str, diff: str) -> ParsedFinding:
    return ParsedFinding(
        severity=Severity.LOW,
        title=f"file is not formatted: {path}",
        description=(
            f"{path} is not formatted according to the repository's ruff format settings."
        ),
        rule_id="ruff-format",
        file=path,
        recommendation=f"Run ``ruff format {path}`` (or ``--check`` locally to inspect).",
        metadata={"tool": "ruff-format", "diff": diff},
    )


# =====================================================================================
# mypy
# =====================================================================================

#: ``path:line:col: error: message  [code]`` and mypy's column-less variant.
_MYPY_LINE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<path>[^\s:][^:]*?):(?P<line>\d+)(?::(?P<column>\d+))?:\s*"
    r"(?P<level>error|warning|note):\s*(?P<message>.*)$"
)
_MYPY_CODE: Final[re.Pattern[str]] = re.compile(r"\[(?P<code>[a-z][a-z0-9-]*)\]\s*$")

#: mypy error codes that describe a concrete type mismatch rather than a stylistic one.
_MEDIUM_MYPY_CODES: Final[tuple[str, ...]] = (
    "attr-defined",
    "arg-type",
    "assignment",
    "return-value",
)


class MypyTool(CommandTool):
    """``mypy`` over the whole checkout, parsed from its text output."""

    name = "mypy"
    capability = Capability.TYPECHECK_RUN
    category = ToolCategory.TYPECHECK
    description = (
        "Runs mypy with deterministic output flags and turns each type error into a "
        "finding with its error code and the offending source line."
    )
    version_argv: tuple[str, ...] = ("mypy", "--version")
    ecosystems = frozenset({"python", "mypy"})
    marker_files = frozenset({"mypy.ini", ".mypy.ini"})
    finding_exit_codes = frozenset({_EXIT_FINDINGS})

    def applicability_reason(self, context: ToolContext) -> str | None:
        if not self.enabled:
            return "tool is disabled by configuration"
        if declares_any(context, _PYTHON_MANIFESTS, ("[tool.mypy", "[mypy]", "mypy")):
            return None
        return super().applicability_reason(context)

    def build_argv(self, context: ToolContext) -> Sequence[str]:
        override = context.override("typecheck")
        if override is None:
            return ["mypy", "--no-color-output", "--no-error-summary", "."]
        argv = list(override)
        if _invokes(argv, "mypy"):
            for flag in ("--no-color-output", "--no-error-summary"):
                if flag not in argv:
                    argv.append(flag)
        return argv

    def interpret(self, outcome: CommandOutcome, context: ToolContext) -> ToolOutcome:
        broken = launch_failure(self.name, outcome)
        if broken is not None:
            return broken

        exit_code = outcome.exit_code
        assert exit_code is not None  # launch_failure() guarantees an exit code
        text = outcome.read_stdout(limit_bytes=8_000_000)
        if not text.strip():
            text = outcome.read_stderr(limit_bytes=8_000_000)

        source = SourceCache(context)
        findings, dropped, notes = _parse_mypy(text, context, source)
        summary = ToolSummary(
            values={
                "errors": len(findings),
                "notes": notes,
                "dropped_findings": dropped,
            }
        )

        if exit_code == _EXIT_OK:
            return ToolOutcome(
                status=ToolStatus.PASSED,
                summary=summary,
                narrative="mypy reported no type errors.",
            )

        if exit_code == _EXIT_FINDINGS:
            if not findings:
                return ToolOutcome(
                    status=ToolStatus.ERROR,
                    summary=summary,
                    narrative=(
                        "mypy exited 1 but Scout could not parse any error from its output; "
                        "the raw output is retained as an artifact."
                    ),
                    error="mypy exited with status 1 and produced no parseable error lines",
                    infrastructure_failure=True,
                )
            narrative = f"mypy reported {len(findings)} type error(s)."
            if notes:
                narrative += f" {notes} note line(s) were ignored as informational."
            if dropped:
                narrative += f" Only the first {len(findings)} are listed."
            return ToolOutcome(
                status=ToolStatus.FAILED,
                summary=summary,
                narrative=narrative,
                findings=findings,
            )

        if exit_code == _EXIT_TOOL_ERROR:
            return ToolOutcome(
                status=ToolStatus.ERROR,
                summary=summary,
                narrative=(
                    "mypy exited with status 2, which means mypy itself failed (bad "
                    "configuration, a crash, or an unusable target) rather than reporting "
                    "type errors."
                ),
                error=excerpt(outcome.read_stderr(limit_bytes=4000), limit=400)
                or "mypy exited with status 2",
                infrastructure_failure=True,
            )

        return nonzero_without_evidence(self.name, outcome, what="mypy diagnostics")


def _parse_mypy(
    text: str,
    context: ToolContext,
    source: SourceCache,
) -> tuple[tuple[ParsedFinding, ...], int, int]:
    findings: list[ParsedFinding] = []
    notes = 0
    for line in text.splitlines():
        match = _MYPY_LINE.match(line.strip())
        if match is None:
            continue
        level = match.group("level")
        if level == "note":
            notes += 1
            continue
        message = match.group("message")
        code_match = _MYPY_CODE.search(message)
        code = code_match.group("code") if code_match is not None else ""
        if code_match is not None:
            message = message[: code_match.start()].strip()
        path = relative_workspace_path(context, match.group("path"))
        line_number = as_int(match.group("line"), 0) or None
        findings.append(
            ParsedFinding(
                severity=_mypy_severity(code, level),
                title=excerpt(f"{code or 'mypy'}: {message}" if code else message, limit=200),
                description=excerpt(message, limit=600),
                rule_id=code or None,
                file=path or None,
                line_start=line_number,
                line_end=line_number,
                recommendation=(
                    "Fix the type error, or annotate the boundary with a narrow ignore "
                    "comment explaining why the type is correct."
                ),
                metadata={
                    "tool": "mypy",
                    "code": code,
                    "level": level,
                    "column": as_int(match.group("column"), 0),
                    "excerpt": source.line(path or None, line_number),
                },
            )
        )
    capped, dropped = cap_findings(findings)
    return capped, dropped, notes


def _mypy_severity(code: str, level: str) -> Severity:
    """Mypy errors that name a real mismatch outrank stylistic complaints."""
    if level == "warning":
        return Severity.LOW
    if code.startswith("syntax"):
        return Severity.HIGH
    if code.startswith(_MEDIUM_MYPY_CODES):
        return Severity.MEDIUM
    return Severity.LOW


# =====================================================================================
# eslint
# =====================================================================================

_ESLINT_FATAL_SEVERITY: Final[int] = 2


class EslintTool(CommandTool):
    """``eslint --format json``, applicable only where package.json and a config exist."""

    name = "eslint"
    capability = Capability.LINT_RUN
    category = ToolCategory.LINT
    description = (
        "Runs eslint with JSON output and converts each message into a finding with its "
        "rule id, severity, and the offending source line."
    )
    version_argv: tuple[str, ...] = ("npx", "--no-install", "eslint", "--version")
    ecosystems = frozenset({"javascript", "typescript", "npm", "pnpm", "yarn"})
    marker_files = _ESLINT_CONFIG_MARKERS
    finding_exit_codes = frozenset({_EXIT_FINDINGS})

    def applicability_reason(self, context: ToolContext) -> str | None:
        if not self.enabled:
            return "tool is disabled by configuration"
        config_reason = missing_marker_reason(
            context, self.marker_files, "eslint configuration (.eslintrc* or eslint.config.*)"
        )
        if config_reason is not None:
            return config_reason
        if not context.workspace.exists("package.json"):
            return (
                "eslint requires a package.json declaring the dependency; none was found "
                "in the workspace"
            )
        return super().applicability_reason(context)

    def build_argv(self, context: ToolContext) -> Sequence[str]:
        override = context.override("lint")
        if override is None or not _invokes(override, "eslint"):
            return ["npx", "--no-install", "eslint", "--format", "json", "."]
        argv = list(override)
        if not any(part == "--format" or part.startswith("--format=") for part in argv):
            argv.extend(["--format", "json"])
        return argv

    def interpret(self, outcome: CommandOutcome, context: ToolContext) -> ToolOutcome:
        broken = launch_failure(self.name, outcome)
        if broken is not None:
            return broken

        exit_code = outcome.exit_code
        assert exit_code is not None  # launch_failure() guarantees an exit code
        value, error = parse_json(outcome.read_stdout(limit_bytes=8_000_000))
        if value is None:
            # eslint writes its diagnostics to stdout, but a configuration failure can
            # arrive on stderr with an empty stdout; both are infrastructure problems.
            return _unreadable_json_outcome(self.name, outcome, error=error, exit_code=exit_code)

        source = SourceCache(context)
        findings: list[ParsedFinding] = []
        errors = warnings = 0
        for file_entry in as_sequence(value):
            mapping = as_mapping(file_entry)
            errors += as_int(mapping.get("errorCount"), 0)
            warnings += as_int(mapping.get("warningCount"), 0)
            findings.extend(_eslint_findings(mapping, context, source))

        capped, dropped = cap_findings(findings)
        summary = ToolSummary(
            values={
                "errors": errors,
                "warnings": warnings,
                "reported_findings": len(capped),
                "dropped_findings": dropped,
            }
        )

        if exit_code not in {_EXIT_OK, _EXIT_FINDINGS}:
            return nonzero_without_evidence(self.name, outcome, what="eslint JSON output")

        if not capped:
            if exit_code == _EXIT_FINDINGS:
                return ToolOutcome(
                    status=ToolStatus.ERROR,
                    summary=summary,
                    narrative=("eslint exited 1 but its JSON report contained no messages."),
                    error="eslint exited with status 1 and reported no messages",
                    infrastructure_failure=True,
                )
            return ToolOutcome(
                status=ToolStatus.PASSED,
                summary=summary,
                narrative="eslint reported no lint messages.",
            )

        narrative = (
            f"eslint reported {errors} error(s) and {warnings} warning(s) across "
            f"{len(capped)} message(s)."
        )
        if dropped:
            narrative += f" Only the first {len(capped)} are listed."
        return ToolOutcome(
            status=ToolStatus.FAILED if errors else ToolStatus.PASSED,
            summary=summary,
            narrative=narrative,
            findings=capped,
        )


def _eslint_findings(
    file_entry: dict[str, object],
    context: ToolContext,
    source: SourceCache,
) -> list[ParsedFinding]:
    raw_path = as_str(file_entry.get("filePath"))
    path = relative_workspace_path(context, raw_path) if raw_path else ""
    findings: list[ParsedFinding] = []
    for message_entry in as_sequence(file_entry.get("messages")):
        message = as_mapping(message_entry)
        rule_id = as_str(message.get("ruleId"))
        severity_number = as_int(message.get("severity"), 0)
        fatal = message.get("fatal") is True
        text = as_str(message.get("message")) or "eslint reported a problem without a message"
        line_number = as_int(message.get("line"), 0) or None
        end_line = as_int(message.get("endLine"), 0) or line_number
        findings.append(
            ParsedFinding(
                severity=_eslint_severity(severity_number, fatal),
                title=excerpt(f"{rule_id or 'eslint'}: {text}", limit=200),
                description=excerpt(text, limit=600),
                rule_id=rule_id or None,
                file=path or None,
                line_start=line_number,
                line_end=end_line,
                recommendation="Fix the reported problem or configure the rule deliberately.",
                metadata={
                    "tool": "eslint",
                    "rule_id": rule_id,
                    "severity": severity_number,
                    "fatal": fatal,
                    "column": as_int(message.get("column"), 0),
                    "end_column": as_int(message.get("endColumn"), 0),
                    "excerpt": source.line(path or None, line_number),
                },
            )
        )
    return findings


def _eslint_severity(severity_number: int, fatal: bool) -> Severity:
    if fatal:
        return Severity.HIGH
    return Severity.MEDIUM if severity_number >= _ESLINT_FATAL_SEVERITY else Severity.LOW


# =====================================================================================
# tsc
# =====================================================================================

#: ``src/app.ts(12,5): error TS2322: Type 'string' is not assignable to type 'number'.``
_TSC_LINE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<path>.+?)\((?P<line>\d+),(?P<column>\d+)\):\s+error\s+"
    r"(?P<code>TS\d+):\s*(?P<message>.*)$"
)
#: ``error TS5083: Cannot read file 'tsconfig.json'.``
_TSC_BARE: Final[re.Pattern[str]] = re.compile(r"^error\s+(?P<code>TS\d+):\s*(?P<message>.*)$")


class TscTool(CommandTool):
    """``tsc --noEmit --pretty false``: TypeScript compiler diagnostics."""

    name = "tsc"
    capability = Capability.TYPECHECK_RUN
    category = ToolCategory.TYPECHECK
    description = (
        "Runs the TypeScript compiler in no-emit mode and parses each TS diagnostics "
        "line into a finding."
    )
    version_argv: tuple[str, ...] = ("npx", "--no-install", "tsc", "--version")
    ecosystems = frozenset({"typescript", "javascript", "npm", "pnpm", "yarn"})
    marker_files = frozenset({"tsconfig.json"})
    finding_exit_codes = frozenset({_EXIT_FINDINGS})

    def build_argv(self, context: ToolContext) -> Sequence[str]:
        override = context.override("typecheck")
        if override is None or not _invokes(override, "tsc"):
            return ["npx", "--no-install", "tsc", "--noEmit", "--pretty", "false"]
        argv = list(override)
        if "--noEmit" not in argv:
            argv.append("--noEmit")
        if not any(part.startswith("--pretty") for part in argv):
            argv.extend(["--pretty", "false"])
        return argv

    def interpret(self, outcome: CommandOutcome, context: ToolContext) -> ToolOutcome:
        broken = launch_failure(self.name, outcome)
        if broken is not None:
            return broken

        exit_code = outcome.exit_code
        assert exit_code is not None  # launch_failure() guarantees an exit code
        text = outcome.read_stdout(limit_bytes=8_000_000)
        if not text.strip():
            text = outcome.read_stderr(limit_bytes=8_000_000)

        source = SourceCache(context)
        findings, dropped = cap_findings(_parse_tsc(text, context, source))
        summary = ToolSummary(values={"errors": len(findings), "dropped_findings": dropped})

        if exit_code == _EXIT_OK:
            return ToolOutcome(
                status=ToolStatus.PASSED,
                summary=summary,
                narrative="tsc reported no type errors.",
            )

        if exit_code == _EXIT_FINDINGS:
            if not findings:
                return ToolOutcome(
                    status=ToolStatus.ERROR,
                    summary=summary,
                    narrative=(
                        "tsc exited 1 but Scout could not parse any diagnostic; the raw "
                        "output is retained as an artifact."
                    ),
                    error="tsc exited with status 1 and produced no parseable diagnostics",
                    infrastructure_failure=True,
                )
            narrative = f"tsc reported {len(findings)} TypeScript error(s)."
            if dropped:
                narrative += f" Only the first {len(findings)} are listed."
            return ToolOutcome(
                status=ToolStatus.FAILED,
                summary=summary,
                narrative=narrative,
                findings=findings,
            )

        return nonzero_without_evidence(self.name, outcome, what="tsc diagnostics")


def _parse_tsc(
    text: str,
    context: ToolContext,
    source: SourceCache,
) -> list[ParsedFinding]:
    findings: list[ParsedFinding] = []
    for line in text.splitlines():
        stripped = line.strip()
        located = _TSC_LINE.match(stripped)
        bare = _TSC_BARE.match(stripped)
        if located is not None:
            raw_path = located.group("path")
            path = relative_workspace_path(context, raw_path)
            line_number = as_int(located.group("line"), 0) or None
            column = as_int(located.group("column"), 0)
        elif bare is not None:
            path = ""
            line_number = None
            column = 0
        else:
            continue
        match = located or bare
        assert match is not None  # exactly one of the two patterns matched
        code = match.group("code")
        message = match.group("message")
        findings.append(
            ParsedFinding(
                severity=_tsc_severity(code),
                title=excerpt(f"{code}: {message}", limit=200),
                description=excerpt(message, limit=600),
                rule_id=code,
                file=path or None,
                line_start=line_number,
                line_end=line_number,
                recommendation=(
                    "Fix the type error, or narrow the type at the boundary that the "
                    "compiler is complaining about."
                ),
                metadata={
                    "tool": "tsc",
                    "code": code,
                    "column": column,
                    "excerpt": source.line(path or None, line_number),
                },
            )
        )
    return findings


def _tsc_severity(code: str) -> Severity:
    """``TS1xxx`` is syntax and configuration; ``TS2xxx``/``TS7xxx`` are type errors."""
    digits = code.removeprefix("TS")
    if digits.startswith("1"):
        return Severity.HIGH
    if digits.startswith(("2", "7")):
        return Severity.MEDIUM
    return Severity.LOW


# =====================================================================================
# shared helpers
# =====================================================================================


def _invokes(argv: Sequence[str], program: str) -> bool:
    """Whether an argument vector invokes ``program`` (directly, via npx, or via python)."""
    parts = list(argv)
    if not parts:
        return False
    for index, part in enumerate(parts):
        name = part.rsplit("/", 1)[-1]
        if name == program or name.startswith(f"{program}."):
            return True
        if name in {"npx", "pnpm", "yarn", "npm", "python", "python3", "uv", "poetry"}:
            continue
        if index >= _MAX_PROGRAM_LOOKAHEAD:
            break
    return False


def _lint_narrative(tool: str, reported: int, findings: int, dropped: int) -> str:
    narrative = f"{tool} reported {reported} violation(s); {findings} became findings."
    if dropped:
        narrative += (
            f" {dropped} more were suppressed after the {findings}-finding ceiling; the "
            "complete output is attached as an artifact."
        )
    return narrative


def _unreadable_json_outcome(
    tool_name: str,
    outcome: CommandOutcome,
    *,
    error: str | None,
    exit_code: int,
) -> ToolOutcome:
    """Handle a JSON-reporting tool whose output could not be read as JSON.

    Exit 0 with unreadable output still means "no problems" for a linter (its contract is
    that complaints are printed), so it is reported as a pass with a caveat. Any other
    exit status is an infrastructure failure.
    """
    if exit_code == _EXIT_OK:
        return ToolOutcome(
            status=ToolStatus.PASSED,
            narrative=(
                f"{tool_name} exited 0 but produced no parseable JSON "
                f"({error or 'unknown reason'}); an exit of 0 means it reported no problems."
            ),
        )
    return nonzero_without_evidence(tool_name, outcome, what="JSON output")
