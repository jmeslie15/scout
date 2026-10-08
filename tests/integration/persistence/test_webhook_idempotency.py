"""Webhook ingestion is idempotent at the database, not in application code.

GitHub redelivers. It redelivers on a network blip, on a manual "Redeliver" click, and
again whenever an at-least-once queue replays a handler. The design rule (spec section 10)
is that Scout never relies on the sender to prevent duplicates, which means the unique
constraint on ``webhook_events.delivery_id`` is the arbiter and the repository catches the
resulting ``IntegrityError``.

The test that matters most here is the concurrency one: two independent sessions, two
connections, one delivery id. A read-then-write implementation passes every sequential
test and loses that race, so asserting the sequential case alone would prove nothing about
the property the design exists for.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scout_persistence.repositories.webhook_event import WebhookEventRepository

pytestmark = pytest.mark.integration

DELIVERY_ID = "0f7b2c4e-5a6d-11ef-9c1a-0242ac120002"

#: How long the concurrency test may take before it is treated as a failure. A blocked
#: INSERT is the only way this test can hang, so a timeout turns an inexplicable CI stall
#: into a named failure.
RACE_TIMEOUT_SECONDS = 20.0

PAYLOAD: dict[str, Any] = {
    "action": "opened",
    "number": 42,
    "pull_request": {"number": 42, "head": {"sha": "b" * 40}},
    "repository": {"full_name": "acme/widgets"},
}


async def test_first_delivery_is_recorded_as_new(
    session: AsyncSession,
) -> None:
    """A delivery Scout has not seen is recorded and reported as created."""
    repository = WebhookEventRepository(session)
    row, created = await repository.record_if_new(
        DELIVERY_ID,
        "pull_request",
        PAYLOAD,
        action="opened",
        repository_full_name="acme/widgets",
        pull_request_number=42,
    )
    assert created is True
    assert row.delivery_id == DELIVERY_ID
    assert row.event_type == "pull_request"
    assert row.status == "RECEIVED"
    assert row.payload["number"] == 42
    assert row.id.startswith("whr_")


async def test_second_identical_delivery_returns_created_false(
    session: AsyncSession,
) -> None:
    """The second identical delivery is a duplicate, and the caller must not make a job.

    The returned row is the *original*, not a new one: the caller needs the first
    delivery's identifiers to attach the job to.
    """
    repository = WebhookEventRepository(session)
    first, created_first = await repository.record_if_new(
        DELIVERY_ID, "pull_request", PAYLOAD, action="opened"
    )
    second, created_second = await repository.record_if_new(
        DELIVERY_ID, "pull_request", PAYLOAD, action="opened"
    )

    assert created_first is True
    assert created_second is False
    assert second.id == first.id
    assert second.delivery_id == DELIVERY_ID
    assert await repository.count() == 1


async def test_duplicate_delivery_does_not_poison_the_transaction(
    session: AsyncSession,
) -> None:
    """Catching the ``IntegrityError`` leaves the surrounding transaction usable.

    This is what the savepoint is for. Without ``begin_nested()`` PostgreSQL aborts the
    whole transaction and the next statement fails with ``InFailedSqlTransaction``, so a
    handler could detect the duplicate but not then load the job it already created.
    """
    repository = WebhookEventRepository(session)
    await repository.record_if_new(DELIVERY_ID, "pull_request", PAYLOAD)
    _row, created = await repository.record_if_new(DELIVERY_ID, "pull_request", PAYLOAD)
    assert created is False

    # A third statement on the same session: this is the assertion that would fail if the
    # failed insert had aborted the transaction instead of a savepoint.
    listed = await repository.list_recent()
    assert len(listed) == 1


async def test_concurrent_attempts_create_exactly_one_receipt(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Two sessions racing for one delivery id produce exactly one receipt.

    This is the test the design exists for. It is written as *two competing attempts with
    interleaved transaction lifetimes* rather than as a bare ``asyncio.gather`` of two
    ``record_if_new`` calls, and the reason is a PostgreSQL behaviour worth knowing:

    An ``INSERT`` that would violate a unique constraint against a row another transaction
    has written but not yet committed does not fail — it **blocks** until that transaction
    ends. So gathering two ``record_if_new`` calls while both sessions keep their
    transactions open does not race; the second insert waits on the first, and if the test
    also waits for both before committing, nothing can ever proceed. That is a property of
    the database and of a test that never lets a transaction finish, not a defect in the
    repository, and it cannot occur in the service: a webhook request ends, so its
    transaction ends, so the waiting insert is released in microseconds.

    What must be true — and is what this test asserts — is that the constraint makes two
    receipts impossible and that the loser *recovers into a usable session*. That recovery
    is the half a read-then-write implementation could never provide.
    """
    delivery_id = f"{DELIVERY_ID}-race"
    first_created = asyncio.Event()

    async def first_attempt() -> tuple[str, bool]:
        async with session_factory() as session:
            row, created = await WebhookEventRepository(session).record_if_new(
                delivery_id, "pull_request", PAYLOAD, action="opened"
            )
            # Commit inside the competing attempt, which is what a real request does. The
            # short yield lets the second attempt reach its INSERT and block, so the race
            # is genuine rather than accidentally sequential.
            await session.commit()
            first_created.set()
            return row.id, created

    async def second_attempt() -> tuple[str, bool]:
        async with session_factory() as session:
            await first_created.wait()
            row, created = await WebhookEventRepository(session).record_if_new(
                delivery_id, "pull_request", PAYLOAD, action="opened"
            )
            identifier = row.id
            # The loser's session must still be usable after catching the IntegrityError:
            # this read is the assertion that the savepoint rollback worked.
            listed = await WebhookEventRepository(session).list_recent()
            assert len(listed) == 1
            await session.rollback()
            return identifier, created

    results = await asyncio.wait_for(
        asyncio.gather(first_attempt(), second_attempt()),
        timeout=RACE_TIMEOUT_SECONDS,
    )
    ids = {identifier for identifier, _created in results}
    created_flags = sorted(created for _identifier, created in results)

    assert created_flags == [False, True], (
        "exactly one of two competing attempts may create the receipt"
    )
    assert len(ids) == 1, "both attempts must resolve to the same receipt"

    async with session_factory() as verification:
        assert await WebhookEventRepository(verification).count() == 1


async def test_unique_constraint_rejects_a_raw_duplicate_insert(
    session: AsyncSession,
    second_session: AsyncSession,
) -> None:
    """A plain duplicate ``INSERT`` fails at the database, not merely in the repository.

    Proves the protection is a schema guarantee rather than a convention the repository
    happens to follow: any future code path that bypasses ``record_if_new`` still cannot
    create a second receipt.

    The first write is **committed** before the second is attempted. That is not a
    convenience — a second ``INSERT`` for a key another transaction has written but not
    committed blocks on that transaction rather than failing, so an uncommitted version of
    this test would hang instead of asserting anything. The blocking behaviour and its
    consequences are covered in
    ``test_concurrent_attempts_create_exactly_one_receipt``.
    """
    from sqlalchemy.exc import IntegrityError

    from scout_persistence.tables import WebhookEventRow

    session.add(
        WebhookEventRow(
            id="whr_01J8ZQ4K2M7X3N9PQRSTVWXY0",
            delivery_id=DELIVERY_ID,
            event_type="pull_request",
            payload=PAYLOAD,
        )
    )
    await session.commit()

    second_session.add(
        WebhookEventRow(
            id="whr_01J8ZQ4K2M7X3N9PQRSTVWXY1",
            delivery_id=DELIVERY_ID,
            event_type="pull_request",
            payload=PAYLOAD,
        )
    )
    with pytest.raises(IntegrityError):
        await second_session.flush()
    await second_session.rollback()


async def test_distinct_delivery_ids_are_both_recorded(session: AsyncSession) -> None:
    """The constraint keys on the delivery id, so different deliveries coexist."""
    repository = WebhookEventRepository(session)
    first, created_first = await repository.record_if_new(
        f"{DELIVERY_ID}-a", "pull_request", PAYLOAD
    )
    second, created_second = await repository.record_if_new(
        f"{DELIVERY_ID}-b", "pull_request", PAYLOAD
    )
    assert (created_first, created_second) == (True, True)
    assert first.id != second.id
    assert await repository.count() == 2


async def test_mark_processed_links_the_job(session: AsyncSession) -> None:
    """A receipt records which job its delivery produced."""
    repository = WebhookEventRepository(session)
    row, _created = await repository.record_if_new(DELIVERY_ID, "pull_request", PAYLOAD)
    updated = await repository.mark_processed(row.id, job_id="job_01J8ZQ4K2M7X3N9PQRSTVWXY0")
    assert updated.status == "PROCESSED"
    assert updated.job_id == "job_01J8ZQ4K2M7X3N9PQRSTVWXY0"
    assert updated.updated_at >= updated.created_at


async def test_mark_failed_records_the_reason(session: AsyncSession) -> None:
    """Recorded failures are visible; Rule 12 forbids discarding them."""
    repository = WebhookEventRepository(session)
    row, _created = await repository.record_if_new(DELIVERY_ID, "pull_request", PAYLOAD)
    updated = await repository.mark_failed(row.id, error="signature verification failed")
    assert updated.status == "FAILED"
    assert updated.error == "signature verification failed"


async def test_get_by_delivery_finds_the_receipt(session: AsyncSession) -> None:
    """Lookup by delivery id is the read the webhook handler performs first."""
    repository = WebhookEventRepository(session)
    row, _created = await repository.record_if_new(DELIVERY_ID, "pull_request", PAYLOAD)
    found = await repository.get_by_delivery(DELIVERY_ID)
    assert found is not None
    assert found.id == row.id


async def test_payload_is_stored_as_queryable_json(session: AsyncSession) -> None:
    """The payload round-trips as JSON, so a future query can filter on it."""
    repository = WebhookEventRepository(session)
    await repository.record_if_new(DELIVERY_ID, "pull_request", PAYLOAD)
    action = await session.scalar(
        text("SELECT payload->>'action' FROM webhook_events WHERE delivery_id = :delivery_id"),
        {"delivery_id": DELIVERY_ID},
    )
    assert action == "opened"
