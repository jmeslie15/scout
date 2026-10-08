"""Tool result contract (spec sections 20 and 21).

Agents never construct shell commands. They request a *tool*, the tool layer resolves
it from the registry, and the sandbox executes it. Every execution produces a
structured result: version, command, exit code, status, timing, and artifact ids for
stdout and stderr.

Raw output is stored as artifacts, not database columns. The database keeps the
*locator*; the artifact store keeps the *bytes*. This is what makes Rule 15
("preserve raw evidence") affordable: a failing test suite producing 40 MB of output
costs one artifact row and one content-addressed blob, not 40 MB of Postgres.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field, model_validator

from scout_domain.enums import Capability, ToolStatus
from scout_domain.models.base import ScoutModel

__all__ = ["ToolInvocation", "ToolResult", "ToolSummary"]


class ToolSummary(ScoutModel):
    """Tool-specific structured counts, e.g. ``{"passed": 183, "failed": 0}``.

    Values are restricted to JSON scalars so that summaries stay queryable and cheap
    to render; anything richer belongs in an artifact.
    """

    values: dict[str, int | float | str | bool | None] = Field(default_factory=dict)

    def get_int(self, key: str, default: int = 0) -> int:
        value = self.values.get(key)
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, (int, float)):
            return int(value)
        return default


class ToolInvocation(ScoutModel):
    """A request to run a tool. Produced before execution, recorded for audit."""

    tool: str = Field(min_length=1, max_length=100)
    capability: Capability

    #: Fully resolved argument vector. Recorded as evidence of exactly what ran.
    argv: list[str] = Field(default_factory=list)
    working_directory: str = Field(default=".", max_length=1024)
    environment: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=600, ge=1, le=86_400)

    #: Why the orchestrator chose to run this tool. Feeds the report narrative.
    rationale: str = Field(default="", max_length=2000)
    requested_by: str = Field(default="orchestrator", max_length=100)


class ToolResult(ScoutModel):
    """The structured outcome of one tool execution (spec section 21)."""

    #: ``trun_...`` identifier for the persisted tool run.
    id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    stage: str = Field(default="", max_length=100)

    tool: str = Field(min_length=1, max_length=100)
    #: Resolved tool version, when the tool can report one. Recorded so that a result
    #: can be reproduced even after the tool is upgraded.
    version: str | None = Field(default=None, max_length=100)
    command: str = Field(min_length=1, max_length=2000)
    argv: list[str] = Field(default_factory=list)

    exit_code: int | None = None
    status: ToolStatus

    started_at: datetime
    completed_at: datetime
    duration_seconds: float = Field(ge=0.0)

    stdout_artifact_id: str | None = None
    stderr_artifact_id: str | None = None
    #: Machine-readable report the tool produced, when it emits one (JUnit XML, SARIF).
    report_artifact_id: str | None = None

    summary: ToolSummary = Field(default_factory=ToolSummary)

    #: Reason the tool did not run to completion (timeout, missing binary, ...).
    error: str | None = Field(default=None, max_length=4000)
    #: True when the tool failed for infrastructure reasons rather than finding a
    #: problem in the code. Infrastructure failures must not be reported as code
    #: defects, and must not be silently swallowed either.
    infrastructure_failure: bool = False

    sandbox_run_id: str | None = None
    #: Short human-readable digest of the outcome, safe for a report.
    narrative: str = Field(default="", max_length=2000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_timing_and_exit(self) -> ToolResult:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at precedes started_at")
        if self.status is ToolStatus.PASSED and self.exit_code not in (0, None):
            raise ValueError(f"status=passed is inconsistent with exit_code={self.exit_code}")
        if self.status is ToolStatus.TIMEOUT and self.exit_code is None and not self.error:
            raise ValueError("a timed-out tool must record an error message")
        return self

    @property
    def is_failure(self) -> bool:
        """Whether this result represents a real problem in the code under test."""
        return self.status is ToolStatus.FAILED

    @property
    def is_error(self) -> bool:
        """Whether the tool itself malfunctioned, rather than finding a defect."""
        return self.status in {ToolStatus.ERROR, ToolStatus.TIMEOUT}
