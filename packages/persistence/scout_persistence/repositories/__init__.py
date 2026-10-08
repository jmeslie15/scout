"""Repository classes: one per aggregate, all taking an ``AsyncSession``.

Every repository here follows the same two rules, and they are what make the
:class:`~scout_persistence.unit_of_work.UnitOfWork` possible:

1. **The session is injected, never created.** Two repositories in one unit of work share
   one session, so they see each other's uncommitted rows and one commit covers both.
2. **Nothing commits or rolls back.** Repositories flush. The transaction boundary is the
   unit of work's, so a failure part-way through a multi-aggregate write leaves nothing
   behind.

The exception is :class:`~scout_persistence.repositories.audit.AuditEventRepository`,
which additionally exposes no mutation method at all: the audit ledger is append-only,
and the absence of an ``update`` or ``delete`` is the enforcement mechanism.
"""

from __future__ import annotations

from scout_persistence.repositories.agent_run import AgentRunRepository
from scout_persistence.repositories.approval import ApprovalRepository
from scout_persistence.repositories.artifact import ArtifactRepository
from scout_persistence.repositories.audit import AuditEventRepository
from scout_persistence.repositories.base import BaseRepository, ensure_valid_columns
from scout_persistence.repositories.decision import EngineeringDecisionRepository
from scout_persistence.repositories.finding import FindingRepository, severity_rank_expression
from scout_persistence.repositories.job import ACTIVE_STATUSES, JobRepository
from scout_persistence.repositories.knowledge import ORGANISATION_SCOPE, KnowledgeRepository
from scout_persistence.repositories.model_request import ModelRequestRepository
from scout_persistence.repositories.notification import NotificationRepository
from scout_persistence.repositories.question import QuestionRepository
from scout_persistence.repositories.repository import RepositoryRepository
from scout_persistence.repositories.sandbox import TERMINAL_STATUSES, SandboxRunRepository
from scout_persistence.repositories.stage import RESOLVED_OUTCOMES, StageRepository
from scout_persistence.repositories.tool_run import ToolRunRepository
from scout_persistence.repositories.webhook_event import WebhookEventRepository

__all__ = [
    "ACTIVE_STATUSES",
    "ORGANISATION_SCOPE",
    "RESOLVED_OUTCOMES",
    "TERMINAL_STATUSES",
    "AgentRunRepository",
    "ApprovalRepository",
    "ArtifactRepository",
    "AuditEventRepository",
    "BaseRepository",
    "EngineeringDecisionRepository",
    "FindingRepository",
    "JobRepository",
    "KnowledgeRepository",
    "ModelRequestRepository",
    "NotificationRepository",
    "QuestionRepository",
    "RepositoryRepository",
    "SandboxRunRepository",
    "StageRepository",
    "ToolRunRepository",
    "WebhookEventRepository",
    "ensure_valid_columns",
    "severity_rank_expression",
]
