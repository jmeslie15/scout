"""The tool contract (spec sections 20 and 21).

Agents never construct shell commands. They request a *tool*; the tool layer resolves it
from the registry and hands it to the sandbox. This module defines what a tool is, what it
receives, and what it must return.

The division of responsibility is deliberate:

* A **tool** knows how to run one program and how to parse its output.
* A **stage** (in the orchestrator) knows which tools to run for a given change and turns
  parsed results into domain :class:`~scout_domain.models.finding.Finding` objects with
  evidence attached.

Tools therefore never invent evidence references or confidence values. They produce
observed facts; the layer that owns the evidence model decides what those facts mean.

All plumbing that every tool needs — the version probe, timing, content-addressed capture
of stdout/stderr, exit-code reconciliation — lives in :meth:`CommandTool.run`, so a
concrete tool is a few dozen lines of *domain* logic (what to run, how to read the result)
and nothing else.
"""

from __future__ import annotations

import abc
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from scout_domain.enums import Capability, Severity, ToolStatus
from scout_domain.ids import IdKind, new_id
from scout_domain.models.artifact import Artifact as ArtifactRecord
from scout_domain.models.artifact import ArtifactKind
from scout_domain.models.base import utcnow
from scout_domain.models.tool import ToolResult, ToolSummary
from scout_observability import get_logger, instrument_span
from scout_sandbox.command import CommandOutcome, SandboxCommand
from scout_tools.artifacts import ArtifactStore

if TYPE_CHECKING:
    from collections.abc import Sequence

    from scout_domain.config import ScoutRepositoryConfig
    from scout_domain.enums import RiskLevel
    from scout_domain.models.repository import RepositorySummary
    from scout_sandbox import Sandbox, Workspace

__all__ = [
    "CommandTool",
    "ParsedFinding",
    "Tool",
    "ToolCategory",
    "ToolContext",
    "ToolOutcome",
]

_log = get_logger(__name__)


class ToolCategory(StrEnum):
    """What kind of engineering question a tool answers.

    The taxonomy matters because the report groups results by it (spec section 44), and
    because a security tool's failure carries different weight from a formatter's.
    """

    TEST = "TEST"
    LINT = "LINT"
    FORMAT = "FORMAT"
    TYPECHECK = "TYPECHECK"
    BUILD = "BUILD"
    SECRET_SCAN = "SECRET_SCAN"
    STATIC_SECURITY = "STATIC_SECURITY"
    DEPENDENCY = "DEPENDENCY"
    CONTAINER = "CONTAINER"
    COVERAGE = "COVERAGE"


@dataclass(frozen=True, slots=True)
class ToolContext:
    """Everything a tool needs, and nothing it does not.

    Note the absence of credentials. A tool runs inside the sandbox against a checked-out
    workspace; it has no database URL, no GitHub token, and no model provider key. That is
    enforced by the sandbox environment allowlist and reinforced here by simply not putting
    them on the context object.
    """

    job_id: str
    stage: str
    repository_full_name: str
    commit_sha: str

    workspace: Workspace
    sandbox: Sandbox
    artifacts: ArtifactStore
    repository_config: ScoutRepositoryConfig

    changed_paths: tuple[str, ...] = ()
    risk_level: RiskLevel | None = None
    repository_summary: RepositorySummary | None = None
    #: Command overrides declared by the repository's ``.scout/engineering.yaml``.
    #: Already tokenised and shell-metacharacter-checked by the domain config parser.
    command_overrides: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def is_changed(self, path_prefix: str) -> bool:
        """Whether any changed path starts with ``path_prefix``.

        Tools use this to decide whether to narrow scope to the change (faster, more
        relevant) or scan the whole repository (safer, slower).
        """
        return any(path.startswith(path_prefix) for path in self.changed_paths)

    def override(self, key: str) -> tuple[str, ...] | None:
        """Return a repository-declared command override, if any."""
        return self.command_overrides.get(key)


@dataclass(frozen=True, slots=True)
class ParsedFinding:
    """A tool-native finding, before it becomes a domain object.

    Tools emit these; stages attach :class:`~scout_domain.models.evidence.EvidenceReference`
    objects and construct domain findings. Keeping the tool vocabulary separate from the
    domain vocabulary means a tool cannot accidentally claim machine-verified evidence it
    does not have.
    """

    severity: Severity
    title: str
    description: str
    rule_id: str | None = None
    file: str | None = None
    line_start: int | None = None
    line_end: int | None = None
    recommendation: str = ""
    #: Tool-specific detail, retained for the evidence view in the dashboard.
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """A tool's interpretation of its own execution."""

    status: ToolStatus
    summary: ToolSummary = field(default_factory=ToolSummary)
    narrative: str = ""
    findings: tuple[ParsedFinding, ...] = ()
    #: Set when the tool itself malfunctioned. Must not be confused with "found problems".
    infrastructure_failure: bool = False
    error: str | None = None
    #: Workspace-relative path to a machine-readable report the tool produced
    #: (JUnit XML, SARIF, JSON), stored as an artifact when present.
    report_path: str | None = None
    report_kind: ArtifactKind = ArtifactKind.TOOL_REPORT
    report_content_type: str = "application/json"


class Tool(abc.ABC):
    """Base class for a deterministic engineering tool."""

    #: Registry key and the value recorded in ``ToolResult.tool``.
    name: str
    #: The capability this tool requires (spec section 54).
    capability: Capability
    category: ToolCategory
    #: Human-facing description used by the ``/tools`` dashboard page.
    description: str = ""

    #: argv that prints the tool version, e.g. ``("pytest", "--version")``.
    version_argv: Sequence[str] = ()
    #: Ecosystem markers. If none appear in the detected stack, the tool does not apply
    #: (spec section 19: "Only use tools applicable to the detected repository").
    ecosystems: frozenset[str] = frozenset()
    #: Files whose presence indicates applicability. More reliable than stack detection in
    #: practice, because a declared-but-unused dependency is common.
    marker_files: frozenset[str] = frozenset()

    def __init__(self, *, enabled: bool = True, timeout_seconds: int | None = None) -> None:
        self.enabled = enabled
        self.timeout_seconds = timeout_seconds

    # -- Applicability ---------------------------------------------------------------

    def is_applicable(self, context: ToolContext) -> bool:
        """Whether this tool should run for ``context``."""
        return self.applicability_reason(context) is None

    def applicability_reason(self, context: ToolContext) -> str | None:
        """Return why this tool does not apply, or ``None`` when it does.

        A skip must be explainable. "pytest was skipped because no test framework was
        detected" is auditable; a silent omission is not.
        """
        if not self.enabled:
            return "tool is disabled by configuration"

        if self.marker_files and not any(
            context.workspace.exists(path) for path in self.marker_files
        ):
            expected = ", ".join(sorted(self.marker_files))
            return f"none of the marker files exist: {expected}"

        if self.ecosystems:
            summary = context.repository_summary
            if summary is None:
                return "no repository summary available to confirm the technology stack"
            present = {
                *summary.stack.languages,
                *summary.stack.frameworks,
                *summary.stack.test_frameworks,
                *summary.stack.package_managers,
                *summary.stack.build_tools,
            }
            if not (self.ecosystems & present):
                expected = ", ".join(sorted(self.ecosystems))
                return f"no detected ecosystem matches this tool (expected one of: {expected})"

        return None

    # -- Execution -------------------------------------------------------------------

    @abc.abstractmethod
    async def run(self, context: ToolContext) -> ToolResult:
        """Execute the tool and return a complete, contract-valid result."""

    async def resolve_version(self, context: ToolContext) -> str | None:
        """Probe the tool's version inside the sandbox.

        Recorded on every result because reproducibility depends on it: a result produced
        by ruff 0.6 means something different from one produced by ruff 0.9, and by the time
        anyone asks, the image will have moved on.
        """
        if not self.version_argv:
            return None
        outcome: CommandOutcome = await context.sandbox.run_simple(
            *self.version_argv,
            cwd=".",
            timeout_seconds=60,
            label=f"{self.name}-version",
        )
        if not outcome.succeeded:
            return None
        text = (
            outcome.read_stdout(limit_bytes=4096).strip()
            or outcome.read_stderr(limit_bytes=4096).strip()
        )
        return extract_version(text)

    def effective_timeout(self, context: ToolContext) -> int:
        if self.timeout_seconds is not None:
            return self.timeout_seconds
        return context.sandbox.limits.command_timeout_seconds

    # -- Helpers shared by concrete tools -------------------------------------------

    def store_artifact(
        self,
        context: ToolContext,
        path: Path | None,
        *,
        kind: ArtifactKind,
        content_type: str = "text/plain",
        tool_run_id: str | None = None,
        filename: str | None = None,
    ) -> ArtifactRecord | None:
        """Persist a captured file as an artifact, returning ``None`` for empty or missing files.

        Empty captures are not stored: an artifact row pointing at zero bytes adds noise to
        the evidence view without adding information.
        """
        if path is None:
            return None
        candidate = path
        if not candidate.exists() or candidate.stat().st_size == 0:
            return None

        # `put_file` is optional on the ArtifactStore protocol so that an in-memory store can
        # omit it. Bind it with an explicit type so the call below stays type-checked.
        put_file: Callable[..., ArtifactRecord] | None = getattr(
            context.artifacts, "put_file", None
        )
        if put_file is None:
            return None
        return put_file(
            candidate,
            kind=kind,
            content_type=content_type,
            tool_run_id=tool_run_id,
            job_id=context.job_id,
            filename=filename or candidate.name,
        )


class CommandTool(Tool):
    """A tool that is one command plus a parser.

    Most tools are exactly this shape, and expressing it once means each concrete tool is
    a few dozen lines: declare the argv, declare applicability, parse the output.
    """

    @abc.abstractmethod
    def build_argv(self, context: ToolContext) -> Sequence[str]:
        """Return the argument vector to execute. Never a shell string."""

    @abc.abstractmethod
    def interpret(self, outcome: CommandOutcome, context: ToolContext) -> ToolOutcome:
        """Turn a :class:`~scout_sandbox.command.CommandOutcome` into a :class:`ToolOutcome`."""

    #: Exit codes that mean "the tool ran and found problems" rather than "the tool broke".
    #: For most linters and test runners that is a non-zero exit. Tools that use unusual
    #: codes override this.
    finding_exit_codes: frozenset[int] = frozenset()

    async def run(self, context: ToolContext) -> ToolResult:
        tool_run_id = new_id(IdKind.TOOL_RUN)
        started_at = utcnow()
        monotonic_start = time.monotonic()

        with instrument_span(
            "tool.run",
            attributes={
                "tool.name": self.name,
                "tool.category": self.category.value,
                "job.id": context.job_id,
                "job.stage": context.stage,
            },
        ):
            version = await self.resolve_version(context)
            argv = list(self.build_argv(context))
            command = SandboxCommand(
                argv=argv,
                timeout_seconds=self.effective_timeout(context),
                label=self.name,
            )

            outcome: CommandOutcome = await context.sandbox.run(command)
            duration = time.monotonic() - monotonic_start

            stdout_artifact = self.store_artifact(
                context,
                outcome.stdout_path,
                kind=ArtifactKind.TOOL_STDOUT,
                tool_run_id=tool_run_id,
                filename=f"{self.name}-stdout.log",
            )
            stderr_artifact = self.store_artifact(
                context,
                outcome.stderr_path,
                kind=ArtifactKind.TOOL_STDERR,
                tool_run_id=tool_run_id,
                filename=f"{self.name}-stderr.log",
            )

            parsed = self._interpret_safely(outcome, context)

            report_artifact = None
            if parsed.report_path:
                try:
                    report_file = context.workspace.resolve(parsed.report_path)
                except Exception:
                    report_file = None
                if report_file is not None:
                    report_artifact = self.store_artifact(
                        context,
                        report_file,
                        kind=parsed.report_kind,
                        content_type=parsed.report_content_type,
                        tool_run_id=tool_run_id,
                        filename=f"{self.name}-report",
                    )

        return ToolResult(
            id=tool_run_id,
            job_id=context.job_id,
            stage=context.stage,
            tool=self.name,
            version=version,
            command=command.display,
            argv=argv,
            exit_code=outcome.exit_code,
            status=parsed.status,
            started_at=started_at,
            completed_at=utcnow(),
            duration_seconds=duration,
            stdout_artifact_id=stdout_artifact.id if stdout_artifact else None,
            stderr_artifact_id=stderr_artifact.id if stderr_artifact else None,
            report_artifact_id=report_artifact.id if report_artifact else None,
            summary=parsed.summary,
            error=parsed.error,
            infrastructure_failure=parsed.infrastructure_failure,
            sandbox_run_id=outcome.sandbox_run_id,
            narrative=parsed.narrative,
            metadata={
                "findings_count": len(parsed.findings),
                "stdout_bytes": outcome.stdout_bytes,
                "stderr_bytes": outcome.stderr_bytes,
                "stdout_truncated": outcome.stdout_truncated,
                "stderr_truncated": outcome.stderr_truncated,
                "launch_error": outcome.launch_error,
            },
        )

    def _interpret_safely(self, outcome: CommandOutcome, context: ToolContext) -> ToolOutcome:
        """Run the tool's parser, converting a parser bug into an infrastructure failure.

        A crash while parsing ``ruff`` output is a bug in Scout, not a finding about the
        pull request. Turning it into a typed, visible error keeps Rule 12 satisfied while
        making sure a malformed report cannot fail a job with an opaque traceback.
        """
        try:
            return self.interpret(outcome, context)
        except Exception as exc:
            _log.error(
                "tool output parser raised",
                tool=self.name,
                job_id=context.job_id,
                error=str(exc),
                exc_info=True,
            )
            return ToolOutcome(
                status=ToolStatus.ERROR,
                narrative=(
                    f"{self.name} produced output that Scout could not parse; the raw output "
                    "is retained as an artifact"
                ),
                error=f"parser failure: {type(exc).__name__}: {exc}",
                infrastructure_failure=True,
            )

    def argv_for(self, context: ToolContext) -> Sequence[str] | None:
        """The argument vector this tool would use, for recording without executing."""
        try:
            return self.build_argv(context)
        except Exception:
            return None


def extract_version(text: str, *, max_length: int = 100) -> str | None:
    """Pull a version-looking token out of a tool's version banner.

    Tools are inconsistent: ``pytest 8.3.2``, ``ruff 0.7.1``, ``mypy 1.13.0 (compiled: yes)``,
    ``gitleaks version 8.21.2``. Taking the first token that starts with a digit handles
    all of those without a per-tool special case.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        for token in stripped.split():
            cleaned = token.strip("(),v")
            if cleaned and cleaned[0].isdigit():
                return cleaned[:max_length]
        return stripped[:max_length]
    return None
