"""Base model configuration shared by every Scout domain model."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, TypeVar

from pydantic import BaseModel, ConfigDict

__all__ = ["ScoutModel", "TimestampedModel", "utcnow"]

ModelT = TypeVar("ModelT", bound="ScoutModel")


def utcnow() -> datetime:
    """Return the current time as a timezone-aware UTC datetime.

    Timezone-aware datetimes are mandatory throughout Scout. Naive timestamps in an
    audit trail are a correctness bug, not a style preference.
    """
    return datetime.now(UTC)


class ScoutModel(BaseModel):
    """Strict base model for domain objects.

    ``extra="forbid"`` is deliberate: agent output and webhook payloads are untrusted
    input, and silently dropping unexpected fields is how contract drift goes
    unnoticed (spec section 56, contract tests).
    """

    model_config = ConfigDict(
        extra="forbid",
        validate_assignment=True,
        str_strip_whitespace=True,
        validate_default=True,
        ser_json_timedelta="float",
    )

    def evolve(self: ModelT, **changes: Any) -> ModelT:
        """Return a validated copy with ``changes`` applied.

        Domain objects are treated as immutable values at their boundaries; mutating a
        job in place is how status and audit rows drift apart.
        """
        return type(self).model_validate({**self.model_dump(), **changes})


class TimestampedModel(ScoutModel):
    """A domain object that records when it was created and last modified."""

    created_at: datetime
    updated_at: datetime
