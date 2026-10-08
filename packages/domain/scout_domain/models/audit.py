"""Audit events (spec section 43).

Every privileged action is audited. The audit log is append-only by contract: there is
no update or delete path in the repository layer.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from scout_domain.enums import ActorType
from scout_domain.models.base import ScoutModel, utcnow

__all__ = ["AuditEvent"]


class AuditEvent(ScoutModel):
    """A single audited action.

    ``actor`` is a human username, an agent role, or a system component name. The
    ``metadata`` blob is for structured detail (old/new status, decision rationale) and
    must never contain secrets.
    """

    id: str = Field(min_length=1)

    actor: str = Field(min_length=1, max_length=200)
    actor_type: ActorType
    #: Dotted verb, e.g. ``job.transition``, ``approval.record``, ``question.answer``.
    action: str = Field(min_length=1, max_length=200)
    #: Resource type, e.g. ``verification_job``.
    resource: str = Field(min_length=1, max_length=100)
    resource_id: str = Field(min_length=1, max_length=200)

    timestamp: datetime = Field(default_factory=utcnow)
    metadata: dict[str, Any] = Field(default_factory=dict)

    #: Correlation identifiers that make an incident reconstructable.
    job_id: str | None = Field(default=None, max_length=100)
    correlation_id: str | None = Field(default=None, max_length=100)
    #: Source address for human-initiated actions; absent for machine actors.
    source_ip: str | None = Field(default=None, max_length=64)

    @property
    def is_privileged(self) -> bool:
        """Whether this action is one the policies require to be audited."""
        return self.action.split(".", 1)[0] in _PRIVILEGED_PREFIXES


#: Action namespaces that must always be audited (spec section 43).
_PRIVILEGED_PREFIXES: frozenset[str] = frozenset(
    {
        "approval",
        "job",
        "question",
        "knowledge",
        "repository",
        "sandbox",
        "secret",
        "policy",
        "user",
        "notification",
    }
)
