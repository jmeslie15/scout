"""Scout's persistence layer: schema, repositories, unit of work, and migrations.

This package owns one half of Scout's storage story and is explicit about which half:

* **PostgreSQL holds state and locators** — jobs, stages, findings, evidence
  *references*, questions and answers, knowledge, approvals, audit events, and the
  locator rows for artifacts. See :mod:`scout_persistence.tables`.
* **The artifact store holds bytes** — tool stdout, diffs, prompts, generated tests. No
  column in the schema accepts raw output (Rule 15); the database stores ``sha256`` and a
  ``storage_key``, and the report dereferences them through
  :class:`scout_domain.models.artifact.Artifact`.

Three modules make up the public surface:

``tables``
    SQLAlchemy 2.0 declarative models. Identifiers are caller-supplied prefixed ULIDs,
    enums are ``VARCHAR`` + ``CHECK`` (never native PostgreSQL ``ENUM``), and idempotency
    is enforced by unique constraints rather than by application reads.
``session`` / ``unit_of_work``
    Engine construction, the session scope that commits once, and the transactional scope
    that exposes every repository bound to the same session.
``mapping``
    Hand-written, bidirectional conversion between rows and the Pydantic domain models.
    Nothing is automatic, because a silent mapping bug surfaces as a wrong report.

Domain code never imports this package: dependencies point inward, and ``scout_domain``
must remain free of a database driver.
"""

from __future__ import annotations

from scout_persistence.mapping import from_domain, to_domain, to_from_domain_row
from scout_persistence.repositories import (
    AgentRunRepository,
    ApprovalRepository,
    ArtifactRepository,
    AuditEventRepository,
    EngineeringDecisionRepository,
    FindingRepository,
    JobRepository,
    KnowledgeRepository,
    ModelRequestRepository,
    NotificationRepository,
    QuestionRepository,
    RepositoryRepository,
    SandboxRunRepository,
    StageRepository,
    ToolRunRepository,
    WebhookEventRepository,
)
from scout_persistence.session import (
    async_session_scope,
    check_database,
    create_engine_from_settings,
    create_session_factory,
    dispose_engine,
)
from scout_persistence.tables import TABLE_NAMES, Base
from scout_persistence.unit_of_work import UnitOfWork

__all__ = [
    "TABLE_NAMES",
    "AgentRunRepository",
    "ApprovalRepository",
    "ArtifactRepository",
    "AuditEventRepository",
    "Base",
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
    "UnitOfWork",
    "WebhookEventRepository",
    "async_session_scope",
    "check_database",
    "create_engine_from_settings",
    "create_session_factory",
    "dispose_engine",
    "from_domain",
    "to_domain",
    "to_from_domain_row",
]
