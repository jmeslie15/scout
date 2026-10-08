"""Webhook delivery ingestion (spec section 10).

The single most important method in this module is :meth:`WebhookEventRepository.record_if_new`.
It must be race-safe, which rules out the obvious implementation::

    existing = await repo.find_by_delivery(delivery_id)   # WRONG
    if existing is not None:
        return existing, False
    return await repo.insert(...), True

Two workers processing the same redelivery run that select concurrently, both see
nothing, and both insert. The database's unique constraint on ``delivery_id`` is the
only arbiter that actually holds under concurrency, so this repository inserts and
catches the resulting ``IntegrityError`` instead.

The savepoint is what makes catching it safe. Without ``begin_nested()`` the failed
insert poisons the surrounding transaction: PostgreSQL aborts it and every subsequent
statement on the same session fails with ``InFailedSqlTransaction``. Rolling back to a
savepoint discards only the losing insert, leaving the caller's unit of work usable so
it can go on to fetch the winning row and decide *not* to create a second job.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from scout_domain.ids import IdKind, new_id
from scout_persistence.repositories.base import BaseRepository
from scout_persistence.tables import WebhookEventRow

__all__ = ["WebhookEventRepository"]


class WebhookEventRepository(BaseRepository):
    """Reads and writes ``webhook_events`` rows."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to ``session``."""
        super().__init__(session)

    async def record_if_new(
        self,
        delivery_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        action: str | None = None,
        repository_full_name: str | None = None,
        pull_request_number: int | None = None,
    ) -> tuple[WebhookEventRow, bool]:
        """Insert a delivery receipt, or return the existing one.

        Returns ``(row, created)``. ``created=False`` means GitHub has delivered this
        event before and the caller must *not* create a second verification job.

        The insert is attempted unconditionally. A duplicate delivery therefore costs
        one failed insert plus one select, which is cheaper and far more obviously
        correct than a read-then-write check that loses under concurrency.

        Note the ``populate_existing=True`` on the recovery read. Rolling back the
        savepoint expires every attribute on the row the losing insert added, so without
        it the returned object is a shell: the caller's first attribute access — usually
        ``row.id``, to attach the job to the delivery — would emit a lazy refresh, and in
        async SQLAlchemy a lazy refresh outside a greenlet context raises
        ``MissingGreenlet`` rather than quietly issuing I/O. Re-reading with
        ``populate_existing`` also refreshes the winner's row from the database, so the
        caller sees committed values rather than whatever was in the identity map.
        """
        row = WebhookEventRow(
            id=new_id(IdKind.WEBHOOK_EVENT),
            delivery_id=delivery_id,
            event_type=event_type,
            action=action,
            repository_full_name=repository_full_name,
            pull_request_number=pull_request_number,
            payload=payload,
            status="RECEIVED",
        )
        try:
            async with self.session.begin_nested():
                self.session.add(row)
                await self.session.flush()
        except IntegrityError:
            # The savepoint has already been rolled back, so the surrounding
            # transaction is still usable and the winner's row is visible.
            existing = await self.session.scalar(
                select(WebhookEventRow)
                .where(WebhookEventRow.delivery_id == delivery_id)
                .execution_options(populate_existing=True)
            )
            if existing is None:  # pragma: no cover - the constraint guarantees a row
                raise
            return existing, False
        return row, True

    async def get_by_delivery(self, delivery_id: str) -> WebhookEventRow | None:
        """Return the receipt for ``delivery_id``, or ``None``."""
        return await self.session.scalar(
            select(WebhookEventRow).where(WebhookEventRow.delivery_id == delivery_id)
        )

    async def get(self, event_id: str) -> WebhookEventRow | None:
        """Return the receipt with ``event_id``, or ``None``."""
        return await self.session.get(WebhookEventRow, event_id)

    async def list_recent(self, *, limit: int = 50, offset: int = 0) -> list[WebhookEventRow]:
        """Return recent receipts, newest first, for the operator view."""
        result = await self.session.scalars(
            select(WebhookEventRow)
            .order_by(WebhookEventRow.created_at.desc(), WebhookEventRow.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result)

    async def mark_processed(self, event_id: str, *, job_id: str) -> WebhookEventRow:
        """Mark a receipt as turned into ``job_id``.

        Recording the job id on the receipt is what makes "which delivery produced this
        job?" answerable from either direction, which is the first question asked when a
        webhook in production behaves unexpectedly.
        """
        row = await self.session.get(WebhookEventRow, event_id)
        if row is None:
            raise LookupError(f"no webhook event {event_id!r}")
        row.status = "PROCESSED"
        row.job_id = job_id
        row.error = None
        self.stamp(row)
        await self.session.flush()
        return row

    async def mark_failed(self, event_id: str, *, error: str) -> WebhookEventRow:
        """Record that a receipt could not be turned into a job."""
        row = await self.session.get(WebhookEventRow, event_id)
        if row is None:
            raise LookupError(f"no webhook event {event_id!r}")
        row.status = "FAILED"
        row.error = error
        self.stamp(row)
        await self.session.flush()
        return row

    async def count(self) -> int:
        """Return the number of stored receipts."""
        total = await self.session.scalar(select(func.count()).select_from(WebhookEventRow))
        return int(total or 0)
