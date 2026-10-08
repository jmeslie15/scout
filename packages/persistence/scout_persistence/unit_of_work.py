"""The unit of work: one session, every repository, one commit boundary.

The API and the orchestrator both need to touch several aggregates to record one
logical fact — "this PR revision was received" writes a webhook event, a job row, an
audit event, and possibly a notification. Those writes must all land or none of them
may, and the commit must happen outside the code that knows about jobs and webhooks.

``async with UnitOfWork(session_factory) as uow:`` gives every repository bound to the
same :class:`~sqlalchemy.ext.asyncio.AsyncSession`, commits once when the block
exits cleanly, and rolls back once when it does not. The exception is re-raised:
a caller that swallows a failed verification write is the bug Rule 12 exists to
prevent, and this class refuses to help.
"""

from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scout_persistence.repositories.agent_run import AgentRunRepository
from scout_persistence.repositories.approval import ApprovalRepository
from scout_persistence.repositories.artifact import ArtifactRepository
from scout_persistence.repositories.audit import AuditEventRepository
from scout_persistence.repositories.decision import EngineeringDecisionRepository
from scout_persistence.repositories.finding import FindingRepository
from scout_persistence.repositories.job import JobRepository
from scout_persistence.repositories.knowledge import KnowledgeRepository
from scout_persistence.repositories.model_request import ModelRequestRepository
from scout_persistence.repositories.notification import NotificationRepository
from scout_persistence.repositories.question import QuestionRepository
from scout_persistence.repositories.repository import RepositoryRepository
from scout_persistence.repositories.sandbox import SandboxRunRepository
from scout_persistence.repositories.stage import StageRepository
from scout_persistence.repositories.tool_run import ToolRunRepository
from scout_persistence.repositories.webhook_event import WebhookEventRepository

__all__ = ["UnitOfWork"]


class UnitOfWork:
    """A transactional scope exposing every repository as an attribute.

    Entered with ``async with``. The session is created on entry and closed on exit,
    so a ``UnitOfWork`` instance is single-use by construction: reusing one after its
    block has exited raises instead of silently operating on a closed session.
    """

    __slots__ = (
        "_entered",
        "_session_factory",
        "agent_runs",
        "approvals",
        "artifacts",
        "audit_events",
        "decisions",
        "findings",
        "jobs",
        "knowledge",
        "model_requests",
        "notifications",
        "questions",
        "repositories",
        "sandbox_runs",
        "session",
        "stages",
        "tool_runs",
        "webhook_events",
    )

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._entered = False
        # Typed as ``AsyncSession`` for readers; assigned in ``__aenter__`` because a
        # session must not outlive the event loop that created it.
        self.session: AsyncSession = None  # type: ignore[assignment]

    async def __aenter__(self) -> Self:
        """Open the session and bind every repository to it."""
        if self._entered:
            raise RuntimeError("this UnitOfWork has already been entered")
        self._entered = True
        session = self._session_factory()
        self.session = session

        self.repositories = RepositoryRepository(session)
        self.webhook_events = WebhookEventRepository(session)
        self.jobs = JobRepository(session)
        self.stages = StageRepository(session)
        self.findings = FindingRepository(session)
        self.questions = QuestionRepository(session)
        self.approvals = ApprovalRepository(session)
        self.artifacts = ArtifactRepository(session)
        self.tool_runs = ToolRunRepository(session)
        self.agent_runs = AgentRunRepository(session)
        self.model_requests = ModelRequestRepository(session)
        self.notifications = NotificationRepository(session)
        self.audit_events = AuditEventRepository(session)
        self.knowledge = KnowledgeRepository(session)
        self.decisions = EngineeringDecisionRepository(session)
        self.sandbox_runs = SandboxRunRepository(session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool:
        """Commit on success, roll back on failure, and never suppress the failure."""
        session = self.session
        try:
            if exc_type is None:
                await session.commit()
            else:
                await session.rollback()
        except BaseException:
            # A failing commit still leaves an open transaction; roll it back before
            # letting the original (or this) exception escape, so the connection
            # returns to the pool clean.
            await session.rollback()
            raise
        finally:
            await session.close()
        return False
