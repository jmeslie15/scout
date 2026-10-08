"""Verification job and stage models (spec sections 11, 12, and 50)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from scout_domain.enums import (
    JobStatus,
    Recommendation,
    RiskLevel,
    VerificationStatus,
)
from scout_domain.models.base import ScoutModel, utcnow
from scout_domain.models.repository import RepositoryRef

__all__ = [
    "CommitRange",
    "JobStage",
    "JobStageOutcome",
    "VerificationJob",
]


class JobStageOutcome(StrEnum):
    """Outcome of one stage attempt.

    ``SKIPPED`` is distinct from ``SUCCEEDED`` because risk-based execution
    (spec section 41) legitimately omits stages. A skip must carry a reason and is
    auditable; a stage that is neither run nor skipped leaves the job unresumable,
    which the state machine refuses to allow.
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    CANCELLED = "CANCELLED"

    @property
    def is_resolved(self) -> bool:
        return self in {JobStageOutcome.SUCCEEDED, JobStageOutcome.SKIPPED}


class CommitRange(ScoutModel):
    """The exact commit range verified (spec sections 12 and 13).

    ``head_sha`` is the commit Scout verified. Report content is meaningless without
    it, and a human approval is bound to it (spec section 74).
    """

    base_sha: str = Field(min_length=7, max_length=64)
    head_sha: str = Field(min_length=7, max_length=64)
    #: ``git merge-base base head``, needed for a correct three-dot diff.
    merge_base_sha: str | None = Field(default=None, min_length=7, max_length=64)

    @model_validator(mode="after")
    def _validate(self) -> CommitRange:
        if self.base_sha == self.head_sha:
            raise ValueError("base_sha and head_sha are identical; nothing to verify")
        return self


class JobStage(ScoutModel):
    """One attempt at one pipeline stage.

    Stages are append-only rows rather than a mutable field on the job, so retries and
    skips leave a complete history. This is what makes "retries are recorded"
    (spec section 51) and post-incident debugging possible.
    """

    id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)
    stage: JobStatus

    outcome: JobStageOutcome = JobStageOutcome.PENDING
    #: 1-based attempt counter for this stage.
    attempt: int = Field(default=1, ge=1)

    started_at: datetime | None = None
    completed_at: datetime | None = None
    duration_seconds: float | None = Field(default=None, ge=0.0)

    error: str | None = Field(default=None, max_length=8000)
    #: Required when ``outcome`` is SKIPPED; explains the risk-based omission.
    skip_reason: str | None = Field(default=None, max_length=2000)

    #: Artifacts produced by this stage, for the job timeline view.
    artifact_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _validate_outcome_consistency(self) -> JobStage:
        if self.outcome is JobStageOutcome.SKIPPED and not self.skip_reason:
            raise ValueError("a skipped stage must record skip_reason")
        if self.outcome is JobStageOutcome.FAILED and not self.error:
            raise ValueError("a failed stage must record an error")
        if self.completed_at is not None and self.started_at is not None:
            if self.completed_at < self.started_at:
                raise ValueError("completed_at precedes started_at")
        if self.outcome.is_resolved and self.completed_at is None:
            raise ValueError(f"a {self.outcome.value} stage must record completed_at")
        return self


class VerificationJob(ScoutModel):
    """One verification of one pull-request revision (spec section 12).

    A job is immutable in identity: ``repository``, ``pull_request_number`` and
    ``commits.head_sha`` never change after creation. A new commit produces a new job
    (spec section 62), which is what keeps reports and approvals meaningful.
    """

    id: str = Field(min_length=1)

    repository: RepositoryRef
    #: Denormalised ``owner/name`` for cheap filtering and logging.
    repository_full_name: str = Field(min_length=3, max_length=200)

    pull_request_number: int = Field(ge=1)
    pull_request_title: str = Field(default="", max_length=1000)
    pull_request_author: str = Field(default="", max_length=200)
    #: Web URL of the PR, surfaced in notifications.
    pull_request_url: str | None = Field(default=None, max_length=2000)

    commits: CommitRange
    #: GitHub webhook delivery id that created this job. The idempotency key.
    github_delivery_id: str | None = Field(default=None, max_length=100)

    status: JobStatus = JobStatus.RECEIVED
    risk_level: RiskLevel = RiskLevel.MEDIUM
    verification_status: VerificationStatus = VerificationStatus.PENDING
    recommendation: Recommendation | None = None

    #: Retry accounting (spec section 51).
    attempt: int = Field(default=1, ge=1)
    max_attempts: int = Field(default=3, ge=1)

    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    completed_at: datetime | None = None

    #: Worker lease used for crash recovery. A job whose lease has expired is
    #: reclaimed and resumed at its first unresolved stage (spec section 50).
    lease_owner: str | None = Field(default=None, max_length=200)
    lease_expires_at: datetime | None = None

    #: Correlation id shared by every log line, span, and audit event for this job.
    correlation_id: str = Field(default="", max_length=100)

    failure_reason: str | None = Field(default=None, max_length=8000)
    cancel_reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _validate_job(self) -> VerificationJob:
        if self.repository.full_name != self.repository_full_name:
            raise ValueError(
                "repository_full_name must match repository.full_name "
                f"({self.repository_full_name!r} != {self.repository.full_name!r})"
            )
        if self.attempt > self.max_attempts:
            raise ValueError("attempt exceeds max_attempts")
        if self.completed_at is not None and self.started_at is not None:
            if self.completed_at < self.started_at:
                raise ValueError("completed_at precedes started_at")
        if self.status is JobStatus.FAILED and not self.failure_reason:
            raise ValueError("a FAILED job must record failure_reason")
        if self.status is JobStatus.CANCELLED and not self.cancel_reason:
            raise ValueError("a CANCELLED job must record cancel_reason")
        return self

    # -- Convenience accessors -------------------------------------------------------

    @property
    def head_sha(self) -> str:
        return self.commits.head_sha

    @property
    def base_sha(self) -> str:
        return self.commits.base_sha

    @property
    def retries_remaining(self) -> int:
        return max(0, self.max_attempts - self.attempt)

    @property
    def is_resumable(self) -> bool:
        """Whether the orchestrator may pick this job back up."""
        if self.status.is_terminal:
            return False
        if self.status is JobStatus.FAILED:
            return self.retries_remaining > 0
        return True

    def lease_is_expired(self, *, now: datetime) -> bool:
        if self.lease_expires_at is None:
            return True
        return self.lease_expires_at <= now
