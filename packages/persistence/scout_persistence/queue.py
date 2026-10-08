"""The job queue: the transport between the control plane and the execution plane.

Spec section 50 requires that jobs survive an API restart, an orchestrator restart, and a
worker restart. A Redis list cannot express that: once a worker pops a message it is gone,
and a crash mid-stage loses the work silently. **Redis Streams with a consumer group** can,
because every delivered message stays in the group's pending list until it is explicitly
acknowledged. A crashed consumer's messages are then reclaimed by
:meth:`JobQueue.reclaim_stale`.

Why the queue lives in this package rather than its own: the API produces, the orchestrator
consumes, and the worker executes. All three already depend on ``scout_persistence``, and
Rule 14 ("avoid premature microservices") makes a separate transport service the wrong call
at this stage. The :class:`JobQueue` protocol is the seam — swapping Redis Streams for SQS
or RabbitMQ is one new class, not a refactor.

Delivery semantics: **at-least-once**. A job may be delivered twice after a crash, so every
stage must be idempotent with respect to the state it writes. The stage rows and the
``(repository, pull_request, head_sha)`` uniqueness constraint on jobs are what make that
safe.
"""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import Field

from scout_domain.enums import JobStatus
from scout_domain.models.base import ScoutModel, utcnow
from scout_observability import get_logger, instrument_span

__all__ = [
    "DEFAULT_CONSUMER_GROUP",
    "DEFAULT_STREAM",
    "InMemoryJobQueue",
    "JobMessage",
    "JobQueue",
    "QueueError",
    "RedisJobQueue",
    "consumer_name",
]

_log = get_logger(__name__)

#: Stream and consumer-group names. Constants so the API and the orchestrator cannot drift.
DEFAULT_STREAM = "scout:jobs"
DEFAULT_CONSUMER_GROUP = "orchestrator"

#: Redis Streams' "beginning of the stream" sentinel. Named because reading ``0-0`` inline
#: looks like a typo.
_STREAM_BEGINNING = "0-0"

#: A stream entry is ``(message_id, fields)``: a 2-tuple. Named so the length checks below
#: read as structure rather than as magic numbers.
_STREAM_ENTRY_ARITY = 2

#: Default idle time before a pending message is treated as abandoned. Comfortably longer
#: than the longest expected stage, because reclaiming a message whose worker is still alive
#: causes duplicate work.
DEFAULT_MIN_IDLE_MS = 300_000


class QueueError(Exception):
    """Raised when the queue is unavailable or returns an unusable response."""


class JobMessage(ScoutModel):
    """A unit of work handed from the control plane to the execution plane.

    Deliberately small: identifiers and the resume point only. Everything else is read from
    the database, so a message can never carry stale state that contradicts the job row.
    """

    job_id: str = Field(min_length=1)
    repository_full_name: str = Field(min_length=3, max_length=200)
    head_sha: str = Field(min_length=7, max_length=64)
    #: 1-based attempt number, mirroring ``VerificationJob.attempt``.
    attempt: int = Field(default=1, ge=1)
    #: Stage to resume from. ``None`` means "start at the first unresolved stage", which is
    #: the normal path after a crash.
    resume_stage: JobStatus | None = None
    enqueued_at: datetime = Field(default_factory=utcnow)

    def to_stream_fields(self) -> dict[str, str]:
        """Serialise for storage in a stream entry.

        Redis stream fields must be flat strings, so the payload is JSON encoded. Field
        names are explicit rather than a single ``payload`` blob so that an operator can
        read a stream entry with ``XRANGE`` and understand it without a decoder.
        """
        return {
            "job_id": self.job_id,
            "repository_full_name": self.repository_full_name,
            "head_sha": self.head_sha,
            "attempt": str(self.attempt),
            "resume_stage": self.resume_stage.value if self.resume_stage else "",
            "enqueued_at": self.enqueued_at.isoformat(),
        }

    @classmethod
    def from_stream_fields(cls, fields: dict[str, object]) -> JobMessage:
        """Deserialise a stream entry, tolerating the missing-``resume_stage`` case."""
        resume = fields.get("resume_stage")
        resume_stage: JobStatus | None = None
        if isinstance(resume, str) and resume:
            try:
                resume_stage = JobStatus(resume)
            except ValueError as exc:
                raise QueueError(f"unknown resume_stage in queue message: {resume!r}") from exc

        enqueued = fields.get("enqueued_at")
        try:
            enqueued_at = datetime.fromisoformat(str(enqueued)) if enqueued else utcnow()
        except ValueError:
            enqueued_at = utcnow()

        return cls(
            job_id=str(fields["job_id"]),
            repository_full_name=str(fields["repository_full_name"]),
            head_sha=str(fields["head_sha"]),
            attempt=int(str(fields.get("attempt", "1"))),
            resume_stage=resume_stage,
            enqueued_at=enqueued_at,
        )


@runtime_checkable
class JobQueue(Protocol):
    """Transport for verification work.

    A protocol so that tests use an in-memory implementation and a future deployment can
    use a managed broker without touching the orchestrator or the API.
    """

    async def enqueue(self, message: JobMessage) -> str:
        """Add a job. Returns the transport message id."""
        ...

    async def reserve(
        self, consumer: str, *, block_ms: int = 5000, count: int = 1
    ) -> list[tuple[str, JobMessage]]:
        """Claim up to ``count`` messages for ``consumer``.

        Blocks for at most ``block_ms``. Must not remove the messages: they remain pending
        until :meth:`ack`, which is what makes crash recovery possible.
        """
        ...

    async def ack(self, message_id: str) -> None:
        """Mark a message as successfully handled."""
        ...

    async def nack(self, message_id: str, *, reason: str) -> None:
        """Leave a message unacknowledged so it is redelivered.

        Deliberately not a "dead letter" operation: a failed stage must remain visible in
        the pending list rather than disappearing.
        """
        ...

    async def reclaim_stale(
        self, consumer: str, *, min_idle_ms: int = DEFAULT_MIN_IDLE_MS, count: int = 10
    ) -> list[tuple[str, JobMessage]]:
        """Take over messages abandoned by a dead consumer."""
        ...

    async def pending_count(self) -> int:
        """Number of delivered-but-unacknowledged messages, for the readiness probe."""
        ...

    async def aclose(self) -> None:
        """Release the connection."""
        ...


def consumer_name(prefix: str = "scout") -> str:
    """Build a unique consumer name for this process.

    Unique per host and PID, so two workers on one host are distinguishable in
    ``XPENDING`` output and the orchestrator can tell which process abandoned a message.
    """
    return f"{prefix}:{socket.gethostname()}:{os.getpid()}"


class RedisJobQueue:
    """Redis Streams implementation with at-least-once delivery.

    Uses ``XADD`` / ``XREADGROUP`` / ``XACK`` / ``XAUTOCLAIM``. The consumer group is
    created lazily and ``BUSYGROUP`` is tolerated, so two processes starting simultaneously
    is not an error.
    """

    def __init__(
        self,
        redis_url: str,
        *,
        stream: str = DEFAULT_STREAM,
        group: str = DEFAULT_CONSUMER_GROUP,
        client: object | None = None,
    ) -> None:
        self.redis_url = redis_url
        self.stream = stream
        self.group = group
        self._client = client
        self._group_ready = False

    # -- Connection ------------------------------------------------------------------

    async def _redis(self) -> object:
        """Return the Redis client, creating it on first use.

        Lazy so that constructing the queue never performs I/O: the API can build one at
        import time even if Redis is briefly unavailable.
        """
        if self._client is None:
            try:
                # Imported lazily on purpose: `scout_persistence` is also imported by the
                # Alembic migration environment, which needs the database and nothing else.
                # A top-level redis import would make every migration path depend on Redis
                # being installed for no benefit.
                from redis.asyncio import Redis
            except ImportError as exc:  # pragma: no cover - dependency is declared
                raise QueueError("the redis package is required for RedisJobQueue") from exc
            self._client = Redis.from_url(self.redis_url, decode_responses=True)
        return self._client

    async def _ensure_group(self) -> None:
        if self._group_ready:
            return
        client = await self._redis()
        try:
            await client.xgroup_create(self.stream, self.group, id="0", mkstream=True)  # type: ignore[attr-defined]
        except _redis_errors() as exc:
            # BUSYGROUP means the group already exists, which is the normal case when a
            # second process starts. Anything else is a real failure and is re-raised as a
            # typed error rather than swallowed.
            if "BUSYGROUP" not in str(exc):
                raise QueueError(
                    f"could not create the consumer group {self.group!r} on {self.stream!r}: {exc}"
                ) from exc
        self._group_ready = True

    # -- Producer --------------------------------------------------------------------

    async def enqueue(self, message: JobMessage) -> str:
        with instrument_span(
            "queue.enqueue",
            attributes={"job.id": message.job_id, "queue.stream": self.stream},
        ):
            client = await self._redis()
            try:
                message_id = await client.xadd(  # type: ignore[attr-defined]
                    self.stream, message.to_stream_fields()
                )
            except _redis_errors() as exc:
                raise QueueError(f"failed to enqueue job {message.job_id}: {exc}") from exc
            _log.info(
                "job enqueued",
                job_id=message.job_id,
                message_id=message_id,
                attempt=message.attempt,
                resume_stage=message.resume_stage.value if message.resume_stage else None,
            )
            return str(message_id)

    # -- Consumer --------------------------------------------------------------------

    async def reserve(
        self, consumer: str, *, block_ms: int = 5000, count: int = 1
    ) -> list[tuple[str, JobMessage]]:
        await self._ensure_group()
        client = await self._redis()
        try:
            response = await client.xreadgroup(  # type: ignore[attr-defined]
                self.group,
                consumer,
                {self.stream: ">"},
                count=count,
                block=block_ms,
            )
        except _redis_errors() as exc:
            raise QueueError(f"failed to read from {self.stream!r}: {exc}") from exc
        return _decode_read_response(response)

    async def ack(self, message_id: str) -> None:
        client = await self._redis()
        await client.xack(self.stream, self.group, message_id)  # type: ignore[attr-defined]

    async def nack(self, message_id: str, *, reason: str) -> None:
        """Leave the message pending and log why.

        Redis Streams has no explicit negative-acknowledge, and that is actually the
        behaviour Scout wants: the entry stays in the pending list, so
        :meth:`reclaim_stale` redelivers it and an operator can see it in ``XPENDING``.
        A true dead-letter stream would hide failed work.
        """
        _log.warning("job message negatively acknowledged", message_id=message_id, reason=reason)

    async def reclaim_stale(
        self, consumer: str, *, min_idle_ms: int = DEFAULT_MIN_IDLE_MS, count: int = 10
    ) -> list[tuple[str, JobMessage]]:
        """Claim messages whose previous consumer stopped acknowledging them."""
        await self._ensure_group()
        client = await self._redis()
        try:
            response = await client.xautoclaim(  # type: ignore[attr-defined]
                self.stream,
                self.group,
                consumer,
                min_idle_time=min_idle_ms,
                start_id=_STREAM_BEGINNING,
                count=count,
            )
        except _redis_errors() as exc:
            raise QueueError(f"failed to reclaim stale messages: {exc}") from exc

        # redis-py returns (cursor, messages) on older versions and
        # (cursor, messages, deleted) on newer ones.
        messages = response[1] if isinstance(response, (list, tuple)) and len(response) > 1 else []
        reclaimed = _decode_message_list(messages)
        if reclaimed:
            _log.warning(
                "reclaimed abandoned job messages",
                count=len(reclaimed),
                min_idle_ms=min_idle_ms,
            )
        return reclaimed

    async def pending_count(self) -> int:
        client = await self._redis()
        try:
            summary = await client.xpending(self.stream, self.group)  # type: ignore[attr-defined]
        except _redis_errors() as exc:
            raise QueueError(f"failed to read the pending count: {exc}") from exc
        if isinstance(summary, dict):
            return int(summary.get("pending", 0))
        if isinstance(summary, (list, tuple)) and summary:
            return int(summary[0])
        return 0

    async def aclose(self) -> None:
        if self._client is not None:
            close = getattr(self._client, "aclose", None) or getattr(self._client, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result
            self._client = None
            self._group_ready = False


class InMemoryJobQueue:
    """Process-local queue used by unit tests and by the offline default.

    Implements the same at-least-once shape as Redis: :meth:`reserve` moves a message into a
    pending set rather than removing it, so a test can prove that an unacknowledged message
    is redelivered.
    """

    def __init__(self) -> None:
        self._ready: list[tuple[str, JobMessage]] = []
        self._pending: dict[str, JobMessage] = {}
        self._counter = 0
        self._delivered_at: dict[str, datetime] = {}

    async def enqueue(self, message: JobMessage) -> str:
        self._counter += 1
        message_id = f"{self._counter}-0"
        self._ready.append((message_id, message))
        return message_id

    async def reserve(
        self, consumer: str, *, block_ms: int = 5000, count: int = 1
    ) -> list[tuple[str, JobMessage]]:
        del consumer, block_ms
        taken = self._ready[:count]
        del self._ready[: len(taken)]
        for message_id, message in taken:
            self._pending[message_id] = message
            self._delivered_at[message_id] = utcnow()
        return taken

    async def ack(self, message_id: str) -> None:
        self._pending.pop(message_id, None)
        self._delivered_at.pop(message_id, None)

    async def nack(self, message_id: str, *, reason: str) -> None:
        _log.warning("job message negatively acknowledged", message_id=message_id, reason=reason)

    async def reclaim_stale(
        self, consumer: str, *, min_idle_ms: int = DEFAULT_MIN_IDLE_MS, count: int = 10
    ) -> list[tuple[str, JobMessage]]:
        del consumer
        now = utcnow()
        stale: list[tuple[str, JobMessage]] = []
        for message_id, message in list(self._pending.items()):
            delivered = self._delivered_at.get(message_id)
            if delivered is None:
                continue
            idle_ms = (now - delivered).total_seconds() * 1000
            if idle_ms >= min_idle_ms:
                stale.append((message_id, message))
                if len(stale) >= count:
                    break
        return stale

    async def pending_count(self) -> int:
        return len(self._pending)

    async def aclose(self) -> None:
        return None

    # -- Test helpers ----------------------------------------------------------------

    def reset(self) -> None:
        self._ready.clear()
        self._pending.clear()
        self._delivered_at.clear()
        self._counter = 0

    @property
    def ready_count(self) -> int:
        return len(self._ready)


def _redis_errors() -> tuple[type[BaseException], ...]:
    """Return the exception types the Redis client can raise.

    Narrowed deliberately (Rule 12): catching ``Exception`` around a Redis call would also
    swallow a bug in Scout's own serialisation code. If the ``redis`` package is absent the
    tuple falls back to ``OSError`` so this module still imports cleanly.
    """
    try:
        from redis.exceptions import RedisError
    except ImportError:  # pragma: no cover - redis is a declared dependency
        return (OSError,)
    return (RedisError, OSError)


def _decode_read_response(response: object) -> list[tuple[str, JobMessage]]:
    """Decode an ``XREADGROUP`` response into message tuples.

    Kept as a free function because the nested response shape is the single most
    error-prone part of the Redis Streams API, and a named function with a docstring is
    easier to test than an inline comprehension.
    """
    if not response:
        return []
    decoded: list[tuple[str, JobMessage]] = []
    try:
        streams = list(response)  # type: ignore[call-overload]
    except TypeError:
        return []
    for entry in streams:
        if not isinstance(entry, (list, tuple)) or len(entry) < _STREAM_ENTRY_ARITY:
            continue
        messages = entry[1]
        if isinstance(messages, (list, tuple)):
            decoded.extend(_decode_message_list(messages))
    return decoded


def _decode_message_list(messages: object) -> list[tuple[str, JobMessage]]:
    decoded: list[tuple[str, JobMessage]] = []
    if not isinstance(messages, (list, tuple)):
        return decoded
    for item in messages:
        if not isinstance(item, (list, tuple)) or len(item) != _STREAM_ENTRY_ARITY:
            continue
        message_id, fields = item
        if not isinstance(fields, dict):
            continue
        try:
            decoded.append((str(message_id), JobMessage.from_stream_fields(fields)))
        except (KeyError, ValueError) as exc:
            # A malformed entry must be reported, not silently dropped: it means a producer
            # wrote something the consumer cannot read, which is a bug worth seeing.
            _log.error(
                "discarding malformed job message",
                message_id=str(message_id),
                fields=json.dumps({str(k): str(v) for k, v in fields.items()}),
                error=str(exc),
            )
    return decoded
