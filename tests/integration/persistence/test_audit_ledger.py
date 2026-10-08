"""The audit ledger is append-only, provably.

Spec section 43 calls the audit log append-only, and this file is where that stops being a
comment. The first test asserts by *reflection* that
:class:`~scout_persistence.repositories.audit.AuditEventRepository` exposes no mutation
method — not by convention, not by documentation, but by there being no attribute to call.
A future change that adds an ``update`` or a ``delete`` fails here and has to argue its
case in review.

The behavioural tests then cover what the ledger has to do: record who did what to which
resource, keep append order, and remain queryable by resource, job, action, and time.
"""

from __future__ import annotations

import inspect
from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from scout_domain.enums import ActorType
from scout_domain.models.audit import AuditEvent
from scout_domain.models.base import utcnow
from scout_persistence.repositories.audit import AuditEventRepository
from scout_persistence.tables import AuditEventRow

pytestmark = pytest.mark.integration

#: Method names that would make the ledger mutable. Checked case-insensitively so
#: ``mark_processed`` and ``set_status`` are caught as well as a literal ``update``.
MUTATING_NAMES = (
    "update",
    "delete",
    "remove",
    "set",
    "mark",
    "clear",
    "replace",
    "overwrite",
    "amend",
    "edit",
    "correct",
    "truncate",
    "purge",
)


def test_audit_repository_exposes_no_mutation_path() -> None:
    """No public method on the repository can modify an audit row.

    Asserted over the class's own attributes rather than over a hand-written list, so a
    method added later is covered automatically. Read helpers such as ``list_for_job`` are
    fine; anything that sounds like a write is not.
    """
    public = [
        name
        for name, _value in inspect.getmembers(AuditEventRepository, predicate=callable)
        if not name.startswith("_")
    ]
    assert "append" in public
    offenders = [
        name
        for name in public
        for banned in MUTATING_NAMES
        if name == banned or name.startswith(f"{banned}_")
    ]
    assert offenders == [], (
        f"the audit ledger must stay append-only; found mutating method(s): {offenders}"
    )


def test_no_repository_method_returns_a_mutable_row_handle() -> None:
    """Every public method is either a write named ``append`` or a read.

    Keeps a rename such as ``append`` -> ``upsert`` from quietly widening the interface.
    """
    public = {
        name
        for name, _value in inspect.getmembers(AuditEventRepository, predicate=callable)
        if not name.startswith("_") and name not in {"session", "stamp"}
    }
    assert public == {
        "append",
        "append_many",
        "record",
        "get",
        "list_for_resource",
        "list_for_job",
        "list_since",
        "list_actions",
        "to_domain",
    }, f"unexpected audit repository surface: {sorted(public)}"


async def test_append_records_an_event(session: AsyncSession, event_factory: object) -> None:
    """An appended event is stored with its actor, action, and resource."""
    repository = AuditEventRepository(session)
    event = event_factory()  # type: ignore[operator]
    row = await repository.append(event)

    assert row.id == event.id
    assert row.actor == "dana"
    assert row.actor_type == ActorType.HUMAN.value
    assert row.action == "approval.record"
    assert row.resource == "verification_job"
    assert AuditEventRepository.to_domain(row) == event


async def test_record_builds_and_appends(session: AsyncSession) -> None:
    """The convenience path mints an ``aud_`` id and validates the actor type."""
    row = await AuditEventRepository(session).record(
        actor="dana",
        actor_type=ActorType.HUMAN.value,
        action="question.answer",
        resource="question",
        resource_id="qst_1",
        metadata={"answer_id": "ans_1"},
        job_id="job_1",
        correlation_id="corr-1",
        source_ip="203.0.113.7",
    )
    assert row.id.startswith("aud_")
    assert row.metadata_ == {"answer_id": "ans_1"}
    assert row.source_ip == "203.0.113.7"


async def test_record_rejects_an_unknown_actor_type(session: AsyncSession) -> None:
    """A caller cannot write an event with a made-up actor type."""
    with pytest.raises(ValueError, match="not a valid ActorType"):
        await AuditEventRepository(session).record(
            actor="dana",
            actor_type="ROBOT",
            action="job.transition",
            resource="verification_job",
            resource_id="job_1",
        )


async def test_append_many_is_one_flush(session: AsyncSession, event_factory: object) -> None:
    """A batch of events lands together, so a transition is never half-audited."""
    repository = AuditEventRepository(session)
    events = [  # type: ignore[operator]
        event_factory(action=f"job.{index}", job_id="job_batch") for index in range(5)
    ]
    rows = await repository.append_many(events)
    assert len(rows) == 5
    assert len(await repository.list_for_job("job_batch")) == 5


async def test_recorded_at_is_separate_from_occurred_at(
    session: AsyncSession,
) -> None:
    """The ledger distinguishes when an action happened from when the row was written.

    Evidence recorded after the fact (a replay, a backfill) must still say when the action
    occurred, so ``occurred_at`` is stored rather than derived from ``created_at``.
    """
    repository = AuditEventRepository(session)
    long_ago = utcnow() - timedelta(days=3)
    event = AuditEvent(
        id="aud_01J8ZQ4K2M7X3N9PQRSTVWXY0",
        actor="system",
        actor_type=ActorType.SYSTEM,
        action="job.transition",
        resource="verification_job",
        resource_id="job_1",
        timestamp=long_ago,
    )
    row = await repository.append(event)

    assert row.occurred_at == long_ago
    assert row.created_at > row.occurred_at


async def test_list_for_resource_returns_the_full_history(
    session: AsyncSession, event_factory: object
) -> None:
    """Resource history is complete and newest first, including an amendment event.

    A correction is modelled as another event about the same resource, which is the only
    way to keep a ledger honest without mutating it.
    """
    repository = AuditEventRepository(session)
    for action in ("job.transition", "job.transition", "approval.record"):
        await repository.append(  # type: ignore[operator]
            event_factory(action=action, resource="verification_job", resource_id="job_1")
        )

    rows = await repository.list_for_resource("verification_job", "job_1")

    assert len(rows) == 3
    assert rows == sorted(rows, key=lambda row: (row.occurred_at, row.id), reverse=True)


async def test_list_for_job_scopes_by_correlation(
    session: AsyncSession, event_factory: object
) -> None:
    """Events for one job are separated from another job's events."""
    repository = AuditEventRepository(session)
    await repository.append(event_factory(job_id="job_1"))  # type: ignore[operator]
    await repository.append(event_factory(job_id="job_2"))  # type: ignore[operator]

    assert {row.job_id for row in await repository.list_for_job("job_1")} == {"job_1"}


async def test_list_since_is_oldest_first(session: AsyncSession, event_factory: object) -> None:
    """An export or replay reads the ledger forward in time."""
    repository = AuditEventRepository(session)
    base = utcnow() - timedelta(hours=1)
    for index in range(3):
        await repository.append(  # type: ignore[operator]
            event_factory(timestamp=base + timedelta(minutes=index))
        )

    rows = await repository.list_since(base - timedelta(minutes=1))

    assert [row.occurred_at for row in rows] == sorted(row.occurred_at for row in rows)


async def test_list_actions_filters_by_action_name(
    session: AsyncSession, event_factory: object
) -> None:
    """One dotted action can be inspected across every resource."""
    repository = AuditEventRepository(session)
    await repository.append(event_factory(action="approval.record"))  # type: ignore[operator]
    await repository.append(event_factory(action="question.answer"))  # type: ignore[operator]

    rows = await repository.list_actions("approval.record")
    assert {row.action for row in rows} == {"approval.record"}


async def test_events_are_ordered_by_a_ulid_break(
    session: AsyncSession, event_factory: object
) -> None:
    """Two events in the same millisecond still order deterministically.

    ULIDs sort by creation time, so the ``id`` tiebreak is stable and the ledger does not
    render differently on two reads.
    """
    repository = AuditEventRepository(session)
    timestamp = utcnow()
    events = [event_factory(timestamp=timestamp) for _ in range(3)]  # type: ignore[operator]
    await repository.append_many(events)

    job_id = "job_01J8ZQ4K2M7X3N9PQRSTVWXY0"
    first_read = [row.id for row in await repository.list_for_job(job_id)]
    second_read = [row.id for row in await repository.list_for_job(job_id)]
    assert first_read == second_read
    assert len(first_read) == 3


async def test_metadata_round_trips_as_json(session: AsyncSession) -> None:
    """Structured detail survives as JSON, queryable by ``psql``.

    The metadata blob is where the interesting detail lives (old and new status, decision
    rationale), so it has to remain real JSON rather than a rendered string.
    """
    row = await AuditEventRepository(session).record(
        actor="worker-1",
        actor_type=ActorType.WORKER.value,
        action="job.transition",
        resource="verification_job",
        resource_id="job_1",
        metadata={"from": "QUEUED", "to": "PREPARING", "attempt": 2},
    )
    assert row.metadata_ == {"from": "QUEUED", "to": "PREPARING", "attempt": 2}


async def test_no_table_default_hides_a_missing_audit_row() -> None:
    """The audit table's own columns are all required where they should be.

    A nullable ``action`` or ``resource_id`` would let an event be written that cannot be
    attributed, which is worse than no event: it looks like coverage.
    """
    table = AuditEventRow.__table__
    for column in ("actor", "actor_type", "action", "resource", "resource_id", "occurred_at"):
        assert table.c[column].nullable is False, f"audit_events.{column} must not be nullable"
