"""Human authorization records (spec sections 46 and 74).

Two rules are enforced structurally rather than by process:

1. **Scout cannot approve its own work.** An approval requires ``decided_by_type`` to be
   ``HUMAN``; there is no code path that constructs one with ``AGENT`` or ``SYSTEM``.
2. **Approval is bound to a commit SHA.** When the pull request moves, the approval is
   explicitly invalidated (``invalidated_at``/``invalidated_reason``) rather than being
   deleted, so the audit trail retains what was authorized and when.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from scout_domain.enums import ActorType, ApprovalDecision
from scout_domain.models.base import ScoutModel, utcnow

__all__ = ["Approval"]


class Approval(ScoutModel):
    """A human decision about one job at one commit."""

    id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)

    #: The exact commit that was authorized. Mandatory (spec section 74).
    commit_sha: str = Field(min_length=7, max_length=64)

    decision: ApprovalDecision
    decided_by: str = Field(min_length=1, max_length=200)
    decided_by_type: ActorType = ActorType.HUMAN
    decided_at: datetime = Field(default_factory=utcnow)
    comment: str = Field(default="", max_length=8000)

    #: Set when the pull request changed after this approval, or the approval was
    #: otherwise superseded. The record is retained; only its validity changes.
    invalidated_at: datetime | None = None
    invalidated_reason: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def _validate_authority(self) -> Approval:
        if self.decided_by_type is not ActorType.HUMAN:
            raise ValueError(
                "an approval must be recorded by a HUMAN; Scout may not authorize its "
                "own work (spec section 46)"
            )
        if (self.invalidated_at is None) != (self.invalidated_reason is None):
            raise ValueError("invalidated_at and invalidated_reason must be set together")
        if self.invalidated_at is not None and self.invalidated_at < self.decided_at:
            raise ValueError("invalidated_at precedes decided_at")
        return self

    @property
    def is_valid(self) -> bool:
        return self.invalidated_at is None

    def invalidate(self, *, reason: str, at: datetime | None = None) -> Approval:
        """Return a copy marked invalid, preserving the original decision."""
        return self.evolve(invalidated_at=at or utcnow(), invalidated_reason=reason)

    def applies_to(self, commit_sha: str) -> bool:
        """Whether this approval authorizes ``commit_sha``.

        Note that a valid approval for an *older* commit does not authorize a newer one.
        This is the mechanism behind "old approval = invalid; new verification =
        required" in spec section 74.
        """
        return self.is_valid and self.commit_sha == commit_sha
