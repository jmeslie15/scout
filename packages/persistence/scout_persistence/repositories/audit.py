"""Append-only audit ledger (spec section 43).

The contract is in what this class *does not have*: no ``update``, no ``delete``, no
``mark_*``, no setter of any kind. An audit log that can be edited is not an audit log,
and making the absence structural — rather than a comment asking people not to call a
method — means the integration test can assert it by reflection.

Read methods return rows ordered newest first because every real consumer (the job
timeline, the operator console, an incident review) starts from the most recent event.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from scout_domain.enums import ActorType
from scout_domain.ids import IdKind, new_id
from scout_domain.models.audit import AuditEvent
from scout_persistence.mapping import from_audit_event, to_audit_event
from scout_persistence.repositories.base import BaseRepository
from scout_persistence.tables import AuditEventRow

__all__ = ["AuditEventRepository"]


class AuditEventRepository(BaseRepository):
    """Appends to, and reads from, ``audit_events``. Never mutates.

    This docstring is a promise the class enforces by omission. If a future change needs
    to correct an audit row, the correct answer is to append a correction event that
    references the original, not to add an ``update`` here.
    """

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to ``session``."""
        super().__init__(session)

    async def append(self, event: AuditEvent) -> AuditEventRow:
        """Append one audit event and return its row."""
        row = from_audit_event(event)
        self.session.add(row)
        await self.session.flush()
        return row

    async def append_many(self, events: list[AuditEvent]) -> list[AuditEventRow]:
        """Append several events in one flush.

        The orchestrator records a batch per transition (status change, lease change,
        notification decision). Batching keeps the ledger atomic with the state change it
        describes, so a crash cannot leave a transition recorded without its audit trail.
        """
        rows = [from_audit_event(event) for event in events]
        self.session.add_all(rows)
        await self.session.flush()
        return rows

    async def record(
        self,
        *,
        actor: str,
        actor_type: str,
        action: str,
        resource: str,
        resource_id: str,
        metadata: dict[str, object] | None = None,
        job_id: str | None = None,
        correlation_id: str | None = None,
        source_ip: str | None = None,
        timestamp: datetime | None = None,
    ) -> AuditEventRow:
        """Build and append an event in one call.

        ``actor_type`` and the metadata pass through
        :class:`~scout_domain.models.audit.AuditEvent` for validation, so a caller cannot
        write an event with an unrecognised actor type even though the shortcut exists.
        """
        event = AuditEvent(
            id=new_id(IdKind.AUDIT_EVENT),
            actor=actor,
            actor_type=ActorType(actor_type),
            action=action,
            resource=resource,
            resource_id=resource_id,
            metadata=dict(metadata or {}),
            job_id=job_id,
            correlation_id=correlation_id,
            source_ip=source_ip,
            **({"timestamp": timestamp} if timestamp is not None else {}),
        )
        return await self.append(event)

    async def get(self, event_id: str) -> AuditEventRow | None:
        """Return the audit row with ``event_id``, or ``None``."""
        return await self.session.get(AuditEventRow, event_id)

    async def list_for_resource(
        self,
        resource: str,
        resource_id: str,
        *,
        limit: int = 100,
    ) -> list[AuditEventRow]:
        """Return the audit history of one resource, newest first."""
        result = await self.session.scalars(
            select(AuditEventRow)
            .where(
                AuditEventRow.resource == resource,
                AuditEventRow.resource_id == resource_id,
            )
            .order_by(AuditEventRow.occurred_at.desc(), AuditEventRow.id.desc())
            .limit(limit)
        )
        return list(result)

    async def list_for_job(self, job_id: str, *, limit: int = 500) -> list[AuditEventRow]:
        """Return every audit event correlated with a job, newest first."""
        result = await self.session.scalars(
            select(AuditEventRow)
            .where(AuditEventRow.job_id == job_id)
            .order_by(AuditEventRow.occurred_at.desc(), AuditEventRow.id.desc())
            .limit(limit)
        )
        return list(result)

    async def list_since(self, since: datetime, *, limit: int = 500) -> list[AuditEventRow]:
        """Return events at or after ``since``, oldest first, for a replay or export."""
        result = await self.session.scalars(
            select(AuditEventRow)
            .where(AuditEventRow.occurred_at >= since)
            .order_by(AuditEventRow.occurred_at.asc())
            .limit(limit)
        )
        return list(result)

    async def list_actions(self, action: str, *, limit: int = 200) -> list[AuditEventRow]:
        """Return events for one dotted action name, newest first."""
        result = await self.session.scalars(
            select(AuditEventRow)
            .where(AuditEventRow.action == action)
            .order_by(AuditEventRow.occurred_at.desc())
            .limit(limit)
        )
        return list(result)

    @staticmethod
    def to_domain(row: AuditEventRow) -> AuditEvent:
        """Convert an audit row into its domain model."""
        return to_audit_event(row)
