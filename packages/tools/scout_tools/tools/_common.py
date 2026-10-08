"""Shared parsing helpers for Scout's deterministic tools.

Every tool in this package is one program plus a parser, and the programs are
inconsistent: some print JSON, some print line-oriented text, some print nothing at all
and exit non-zero. This module holds the small set of decisions that must be made the
same way by every parser:

* **Read what is there, tolerate what is not.** :func:`read_workspace_text` and
  :func:`parse_json` return ``None``/an error instead of raising, so a truncated report
  becomes an honest ``ERROR`` rather than a traceback.
* **Never turn a broken tool into a finding.** :func:`launch_failure` and
  :func:`nonzero_without_evidence` produce ``ERROR``/``TIMEOUT`` outcomes with
  ``infrastructure_failure=True``. A finding about a pull request is only ever emitted
  from output the tool actually produced.
* **Bound everything.** Find indices and excerpts are capped so that a scanner which
  emits a hundred thousand results cannot exhaust the worker's memory.

Nothing here knows about a specific tool.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final

from scout_domain.enums import Severity, ToolStatus
from scout_domain.models.artifact import ArtifactKind
from scout_domain.models.tool import ToolSummary
from scout_tools.base import ParsedFinding, ToolOutcome

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from scout_sandbox.command import CommandOutcome
    from scout_tools.base import ToolContext

__all__ = [
    "MAX_EXCERPT_CHARS",
    "MAX_FINDINGS",
    "REPORT_DIRECTORY",
    "SourceCache",
    "as_float",
    "as_int",
    "as_mapping",
    "as_sequence",
    "as_str",
    "cap_findings",
    "declares_any",
    "ensure_report_directory",
    "excerpt",
    "launch_failure",
    "missing_marker_reason",
    "nonzero_without_evidence",
    "parse_json",
    "read_workspace_text",
    "relative_workspace_path",
    "report_path",
    "scanner_outcome",
    "severity_from_name",
    "skipped_outcome",
]

#: Where tools write machine-readable reports, relative to the workspace root.
#: The sandbox runs commands with the workspace as the working directory, so a relative
#: path is written inside the ephemeral checkout and never touches the host.
REPORT_DIRECTORY: Final[str] = ".scout/reports"

#: Ceiling on parsed findings retained per tool run. The raw output is still stored as
#: an artifact, so nothing is lost; the cap only keeps one pathological repository from
#: filling the worker's memory.
MAX_FINDINGS: Final[int] = 500

#: Ceiling on a single human-facing excerpt (a code line, a message, a diff hunk).
MAX_EXCERPT_CHARS: Final[int] = 600

#: Largest workspace file a parser will read into memory (8 MiB). Reports are expected
#: to be far smaller; anything larger is truncated and the parse fails honestly.
_MAX_REPORT_BYTES: Final[int] = 8 * 1024 * 1024

_WHITESPACE: Final[re.Pattern[str]] = re.compile(r"\s+")

_SEVERITY_BY_NAME: Final[dict[str, Severity]] = {
    "critical": Severity.CRITICAL,
    "high": Severity.HIGH,
    "error": Severity.HIGH,
    "medium": Severity.MEDIUM,
    "moderate": Severity.MEDIUM,
    "warning": Severity.MEDIUM,
    "low": Severity.LOW,
    "info": Severity.INFO,
    "informational": Severity.INFO,
    "unknown": Severity.INFO,
    "undefined": Severity.INFO,
}


def report_path(filename: str) -> str:
    """Return the workspace-relative path a tool should write ``filename`` to."""
    return f"{REPORT_DIRECTORY}/{filename}"


def ensure_report_directory(context: ToolContext) -> None:
    """Create the report directory inside the workspace before a tool writes to it.

    Tools that take a report path (pytest's ``--junit-xml``, gitleaks's
    ``--report-path``) differ in whether they create missing parent directories; several
    do not. Creating ``.scout/reports`` up front turns "the scanner exited 1 with no
    report" into a report Scout can read. Failure is ignored deliberately: a workspace
    that will not accept a directory will make the tool fail honestly, and ``build_argv``
    must not raise.
    """
    try:
        target = context.workspace.resolve(REPORT_DIRECTORY)
        target.mkdir(parents=True, exist_ok=True)
    except Exception:  # an unwritable workspace is reported by the tool, not here
        return


def excerpt(text: str | None, *, limit: int = MAX_EXCERPT_CHARS) -> str:
    """Collapse whitespace and truncate ``text`` into a one-paragraph excerpt."""
    if not text:
        return ""
    collapsed = _WHITESPACE.sub(" ", text).strip()
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 3].rstrip() + "..."


def read_workspace_text(
    context: ToolContext,
    relative_path: str,
    *,
    max_bytes: int = _MAX_REPORT_BYTES,
) -> str | None:
    """Read a file from the workspace, returning ``None`` when it is absent or unreadable.

    Path resolution goes through :class:`~scout_sandbox.Workspace`, so a report path that
    tries to escape the checkout is refused rather than followed.
    """
    try:
        target = context.workspace.resolve(relative_path)
    except Exception:  # an unsafe or malformed path is simply unreadable
        return None
    try:
        if not target.is_file():
            return None
        if target.stat().st_size > max_bytes:
            with target.open("r", encoding="utf-8", errors="replace") as handle:
                return handle.read(max_bytes)
        return target.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def parse_json(text: str | None) -> tuple[object | None, str | None]:
    """Parse ``text`` as JSON.

    Returns:
        ``(value, None)`` on success, or ``(None, reason)`` when the text is empty,
        truncated, or otherwise not JSON. Callers must treat the latter as "the tool's
        output could not be read", never as "the tool found nothing".
    """
    if text is None:
        return None, "no output was captured"
    stripped = text.strip()
    if not stripped:
        return None, "output was empty"
    try:
        value: object = json.loads(stripped)
    except (json.JSONDecodeError, ValueError) as exc:
        return None, f"output was not valid JSON: {exc}"
    return value, None


def as_mapping(value: object) -> dict[str, Any]:
    """Return ``value`` as a string-keyed mapping, or an empty mapping."""
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return {}


def as_sequence(value: object) -> list[Any]:
    """Return ``value`` as a list, or an empty list when it is not a sequence."""
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    return []


def as_str(value: object, default: str = "") -> str:
    """Return ``value`` as a string; JSON scalars are rendered, containers are not."""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return default


def as_int(value: object, default: int = 0) -> int:
    """Return ``value`` as an integer, tolerating numeric strings and floats."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return default
    return default


def as_float(value: object, default: float = 0.0) -> float:
    """Return ``value`` as a float, tolerating numeric strings."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return default
    return default


def severity_from_name(value: object, *, default: Severity = Severity.MEDIUM) -> Severity:
    """Map a tool's own severity vocabulary onto Scout's.

    Tools disagree about names (``moderate`` vs ``medium``, ``error`` vs ``high``), so
    the mapping is table-driven and deliberately conservative: an unrecognised severity
    becomes ``default`` rather than being silently dropped to ``INFO``.
    """
    name = as_str(value).strip().lower()
    if not name:
        return default
    return _SEVERITY_BY_NAME.get(name, default)


def relative_workspace_path(context: ToolContext, raw: str) -> str:
    """Normalise a tool-reported path into a workspace-relative POSIX path.

    Tools are inconsistent: ruff reports absolute paths, bandit reports relative ones,
    and coverage may report either depending on how it was invoked. Findings must carry
    a path a reviewer can open, so an absolute path inside the workspace is made
    relative, and anything else is returned cleaned but unchanged.
    """
    cleaned = raw.strip().replace("\\", "/")
    if not cleaned:
        return ""
    candidate = Path(cleaned)
    if candidate.is_absolute():
        try:
            return candidate.relative_to(context.workspace.path).as_posix()
        except ValueError:
            return cleaned
    while cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def missing_marker_reason(
    context: ToolContext,
    markers: Iterable[str],
    description: str,
) -> str | None:
    """Return why none of ``markers`` exist, or ``None`` when at least one does.

    Used by tools whose applicability is a conjunction (``package.json`` *and* an eslint
    config) that ``marker_files`` alone cannot express.
    """
    if any(context.workspace.exists(marker) for marker in markers):
        return None
    expected = ", ".join(sorted(markers))
    return f"no {description} found (expected one of: {expected})"


def declares_any(
    context: ToolContext,
    candidates: Iterable[str],
    needles: Iterable[str],
    *,
    max_bytes: int = 200_000,
) -> bool:
    """Whether a manifest in the workspace mentions one of ``needles``.

    Some tools advertise themselves only inside a shared manifest (``[tool.ruff]`` in
    ``pyproject.toml``, ``bandit`` in ``requirements.txt``). ``marker_files`` can say
    "this file exists"; this says "this file claims the tool is used", which is the check
    that keeps a tool from being run on a repository that never opted into it.
    """
    lowered_needles = [needle.lower() for needle in needles]
    for candidate in candidates:
        if not context.workspace.exists(candidate):
            continue
        try:
            content = context.workspace.read_text(candidate, max_bytes=max_bytes).lower()
        except Exception:  # an unreadable manifest is not fatal
            continue
        if any(needle in content for needle in lowered_needles):
            return True
    return False


def cap_findings(
    findings: Sequence[ParsedFinding],
) -> tuple[tuple[ParsedFinding, ...], int]:
    """Cap a finding list, returning ``(kept, dropped_count)``."""
    if len(findings) <= MAX_FINDINGS:
        return tuple(findings), 0
    return tuple(findings[:MAX_FINDINGS]), len(findings) - MAX_FINDINGS


def launch_failure(tool_name: str, outcome: CommandOutcome) -> ToolOutcome | None:
    """Return an infrastructure outcome when the command never produced a result.

    Returns ``None`` when the command ran to completion, at which point the caller owns
    the interpretation of its exit code and output.
    """
    if outcome.launch_error:
        return ToolOutcome(
            status=ToolStatus.ERROR,
            narrative=(
                f"{tool_name} could not be started inside the sandbox: {outcome.launch_error}. "
                "This is an infrastructure failure, not a finding about the change."
            ),
            error=f"launch error: {outcome.launch_error}"[:4000],
            infrastructure_failure=True,
        )
    if outcome.timed_out:
        return ToolOutcome(
            status=ToolStatus.TIMEOUT,
            narrative=(
                f"{tool_name} exceeded its time limit after "
                f"{outcome.duration_seconds:.1f}s and was terminated; no result is available."
            ),
            error=f"timed out after {outcome.duration_seconds:.1f}s",
            infrastructure_failure=True,
        )
    if outcome.likely_memory_kill:
        return ToolOutcome(
            status=ToolStatus.ERROR,
            narrative=(
                f"{tool_name} was killed, most likely for exceeding the sandbox memory "
                "limit. Re-run with a larger limit or a narrower scope."
            ),
            error="killed by SIGKILL with no timeout, consistent with an out-of-memory kill",
            infrastructure_failure=True,
        )
    if outcome.was_killed_by_signal:
        return ToolOutcome(
            status=ToolStatus.ERROR,
            narrative=f"{tool_name} was terminated by signal {outcome.signal}.",
            error=f"terminated by signal {outcome.signal}",
            infrastructure_failure=True,
        )
    if outcome.exit_code is None:
        return ToolOutcome(
            status=ToolStatus.ERROR,
            narrative=(
                f"{tool_name} finished without reporting an exit code, so Scout cannot "
                "interpret the run."
            ),
            error="no exit code recorded",
            infrastructure_failure=True,
        )
    return None


def nonzero_without_evidence(
    tool_name: str,
    outcome: CommandOutcome,
    *,
    what: str = "output",
) -> ToolOutcome:
    """Build the ``ERROR`` outcome for a tool that failed without producing evidence.

    This is the deliberate counterpart to a ``FAILED`` result: an unreadable report or an
    unexpected exit status says something about the tool, not about the pull request, and
    must never become a finding.
    """
    stderr_tail = excerpt(outcome.read_stderr_tail(limit_bytes=4000), limit=400)
    detail = f"exit status {outcome.exit_code} with no parseable {what}"
    if stderr_tail:
        detail = f"{detail}; stderr: {stderr_tail}"
    return ToolOutcome(
        status=ToolStatus.ERROR,
        narrative=(
            f"{tool_name} exited with status {outcome.exit_code} but produced no parseable "
            f"{what}, so Scout reports an infrastructure failure rather than a finding. "
            f"Raw output is retained as an artifact."
        ),
        error=detail[:4000],
        infrastructure_failure=True,
    )


def skipped_outcome(tool_name: str, narrative: str) -> ToolOutcome:
    """Build a ``SKIPPED`` outcome: the tool ran but there was nothing to verify."""
    return ToolOutcome(status=ToolStatus.SKIPPED, narrative=f"{tool_name}: {narrative}")


def scanner_outcome(
    tool_name: str,
    *,
    findings: tuple[ParsedFinding, ...],
    summary: ToolSummary,
    exit_code: int,
    finding_exit_codes: frozenset[int],
    passed_narrative: str,
    failed_narrative: str,
    report_path: str | None = None,
    report_kind: ArtifactKind = ArtifactKind.SECURITY_REPORT,
) -> ToolOutcome:
    """Build the outcome shared by scanners whose findings come from a report.

    The three cases are the load-bearing distinction of the tool package: findings mean
    ``FAILED`` (a real result about the change), a "found problems" exit code with no
    findings means the scanner broke, and anything else is a pass.
    """
    if findings:
        return ToolOutcome(
            status=ToolStatus.FAILED,
            summary=summary,
            narrative=failed_narrative,
            findings=findings,
            report_path=report_path,
            report_kind=report_kind,
        )
    if exit_code in finding_exit_codes:
        return ToolOutcome(
            status=ToolStatus.ERROR,
            summary=summary,
            narrative=(
                f"{tool_name} exited with status {exit_code}, which means it found problems, "
                "but it reported none that Scout could read. The raw output is retained as "
                "an artifact."
            ),
            error=f"{tool_name} exited with status {exit_code} and reported nothing",
            infrastructure_failure=True,
            report_path=report_path,
            report_kind=report_kind,
        )
    return ToolOutcome(
        status=ToolStatus.PASSED,
        summary=summary,
        narrative=passed_narrative,
        report_path=report_path,
        report_kind=report_kind,
    )


@dataclass(slots=True)
class SourceCache:
    """Bounded, on-demand access to workspace source lines for finding excerpts.

    A scanner can emit hundreds of findings against one file. Reading that file once and
    remembering only its lines keeps the cost linear in distinct files rather than in
    findings, without holding the whole checkout in memory.
    """

    context: ToolContext
    _lines: dict[str, list[str] | None]

    def __init__(self, context: ToolContext) -> None:
        self.context = context
        self._lines = {}

    def line(self, path: str | None, line_number: int | None, *, limit: int = 200) -> str | None:
        """Return the source text of a 1-based line, or ``None`` when unavailable."""
        if not path or not line_number or line_number < 1:
            return None
        lines = self._for(path)
        if lines is None or line_number > len(lines):
            return None
        return excerpt(lines[line_number - 1], limit=limit)

    def _for(self, path: str) -> list[str] | None:
        if path in self._lines:
            return self._lines[path]
        text = read_workspace_text(self.context, path, max_bytes=400_000)
        lines = text.splitlines() if text is not None else None
        self._lines[path] = lines
        return lines
