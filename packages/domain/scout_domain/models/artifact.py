"""Artifacts: the store of raw evidence (Rule 15, spec section 21).

Logs, diffs, generated tests, and tool reports are stored as artifacts rather than
database columns. An artifact is content-addressed by SHA-256, so identical output
stored twice costs one blob, and a finding can always be traced to the exact bytes that
produced it.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import Field, model_validator

from scout_domain.models.base import ScoutModel, utcnow

__all__ = ["DEFAULT_RETENTION", "Artifact", "ArtifactKind"]


class ArtifactKind(StrEnum):
    """Categories of stored evidence."""

    DIFF = "DIFF"
    CHANGED_PATHS = "CHANGED_PATHS"
    TOOL_STDOUT = "TOOL_STDOUT"
    TOOL_STDERR = "TOOL_STDERR"
    TOOL_REPORT = "TOOL_REPORT"
    TEST_REPORT = "TEST_REPORT"
    SECURITY_REPORT = "SECURITY_REPORT"
    AGENT_RESULT = "AGENT_RESULT"
    AGENT_PROMPT = "AGENT_PROMPT"
    PR_DESCRIPTION = "PR_DESCRIPTION"
    REQUIREMENTS = "REQUIREMENTS"
    REPOSITORY_SUMMARY = "REPOSITORY_SUMMARY"
    CHANGE_GRAPH = "CHANGE_GRAPH"
    GENERATED_TEST = "GENERATED_TEST"
    VERIFICATION_REPORT = "VERIFICATION_REPORT"
    CONFIG_SNAPSHOT = "CONFIG_SNAPSHOT"
    LOG = "LOG"


#: Default retention. Evidence is retained long enough to explain a decision made
#: months earlier during an incident review, then expired.
DEFAULT_RETENTION: dict[ArtifactKind, timedelta] = dict.fromkeys(ArtifactKind, timedelta(days=365))
DEFAULT_RETENTION[ArtifactKind.TOOL_STDOUT] = timedelta(days=90)
DEFAULT_RETENTION[ArtifactKind.TOOL_STDERR] = timedelta(days=90)
DEFAULT_RETENTION[ArtifactKind.LOG] = timedelta(days=30)
#: Verification reports are the durable record of what Scout concluded and why.
DEFAULT_RETENTION[ArtifactKind.VERIFICATION_REPORT] = timedelta(days=2555)


class Artifact(ScoutModel):
    """Metadata for one stored blob.

    The bytes live in the artifact store; this row holds the locator, the digest, and
    the ownership links needed for retention and for the dashboard's evidence view.
    """

    id: str = Field(min_length=1)
    job_id: str | None = Field(default=None, max_length=100)

    kind: ArtifactKind
    #: IANA media type. ``text/plain`` for most tool output.
    content_type: str = Field(default="text/plain", max_length=200)
    size_bytes: int = Field(ge=0)
    #: Hex SHA-256 of the stored bytes. Also the content address.
    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")

    #: Backend-specific locator, e.g. ``sha256/ab/cd/<digest>`` or an S3 key.
    storage_key: str = Field(min_length=1, max_length=1024)
    #: Human-facing name, e.g. ``pytest-stdout.txt``.
    filename: str | None = Field(default=None, max_length=300)

    #: Producing entity, for the evidence graph.
    tool_run_id: str | None = Field(default=None, max_length=100)
    agent_run_id: str | None = Field(default=None, max_length=100)
    sandbox_run_id: str | None = Field(default=None, max_length=100)

    created_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def _validate(self) -> Artifact:
        if self.expires_at is None:
            object.__setattr__(
                self,
                "expires_at",
                self.created_at + DEFAULT_RETENTION.get(self.kind, timedelta(days=365)),
            )
        elif self.expires_at <= self.created_at:
            raise ValueError("expires_at must be after created_at")
        return self

    @property
    def human_size(self) -> str:
        """Render the size for display, e.g. ``1.4 MiB``."""
        size = float(self.size_bytes)
        for unit in ("B", "KiB", "MiB", "GiB"):
            if size < 1024 or unit == "GiB":
                return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} GiB"  # pragma: no cover - unreachable
