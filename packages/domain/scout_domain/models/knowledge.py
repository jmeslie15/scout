"""Engineering memory (spec sections 29 and 30).

Scout must not repeatedly flag something a human already decided. Human decisions carry
higher authority than inferred AI knowledge, and that ordering is encoded in
:class:`KnowledgeAuthority` so retrieval can rank by it rather than by recency alone.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from scout_domain.models.base import ScoutModel, utcnow

__all__ = [
    "AUTHORITY_RANK",
    "EngineeringDecision",
    "KnowledgeAuthority",
    "KnowledgeDocument",
    "KnowledgeSourceType",
]


class KnowledgeSourceType(StrEnum):
    """Where a piece of engineering memory came from (spec section 29)."""

    HUMAN_ANSWER = "HUMAN_ANSWER"
    ARCHITECTURE_DOCUMENT = "ARCHITECTURE_DOCUMENT"
    ADR = "ADR"
    REPOSITORY_DOCUMENTATION = "REPOSITORY_DOCUMENTATION"
    VERIFIED_FINDING = "VERIFIED_FINDING"
    APPROVED_EXCEPTION = "APPROVED_EXCEPTION"
    ENGINEERING_POLICY = "ENGINEERING_POLICY"


class KnowledgeAuthority(StrEnum):
    """How much weight a knowledge document carries when it conflicts with others."""

    #: A binding policy. Overrides everything below it.
    POLICY = "POLICY"
    #: An explicit human decision recorded in answer to a Scout question.
    HUMAN_DECISION = "HUMAN_DECISION"
    #: A reviewed and approved architecture document or ADR.
    APPROVED_DOCUMENT = "APPROVED_DOCUMENT"
    #: Repository documentation. Authoritative for intent, may be stale.
    REPOSITORY_DOC = "REPOSITORY_DOC"
    #: A previously verified finding. Evidence-backed but possibly superseded.
    VERIFIED_FINDING = "VERIFIED_FINDING"
    #: Inferred by an agent. Lowest authority by construction.
    INFERRED = "INFERRED"


#: Ranking used by retrieval. Higher wins.
AUTHORITY_RANK: dict[KnowledgeAuthority, int] = {
    KnowledgeAuthority.INFERRED: 0,
    KnowledgeAuthority.VERIFIED_FINDING: 1,
    KnowledgeAuthority.REPOSITORY_DOC: 2,
    KnowledgeAuthority.APPROVED_DOCUMENT: 3,
    KnowledgeAuthority.HUMAN_DECISION: 4,
    KnowledgeAuthority.POLICY: 5,
}

#: Source types that must never be stored with ``INFERRED`` authority, and vice versa.
_HUMAN_SOURCES: frozenset[KnowledgeSourceType] = frozenset(
    {
        KnowledgeSourceType.HUMAN_ANSWER,
        KnowledgeSourceType.APPROVED_EXCEPTION,
        KnowledgeSourceType.ENGINEERING_POLICY,
    }
)


class KnowledgeDocument(ScoutModel):
    """A retrievable unit of engineering memory.

    ``scope`` is ``owner/name`` for repository-specific knowledge or ``*`` for
    organisation-wide knowledge. Retrieval filters on scope before ranking, so a
    decision about one service is never applied to an unrelated one.
    """

    id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=300)
    body: str = Field(min_length=1)
    source_type: KnowledgeSourceType
    authority: KnowledgeAuthority

    #: ``owner/name`` or ``*``.
    scope: str = Field(default="*", max_length=200)
    tags: list[str] = Field(default_factory=list)
    #: Pointer back to whatever produced this document (question answer, ADR path, ...).
    source_reference: str | None = Field(default=None, max_length=1000)

    #: Identifier of the stored embedding, when one has been computed. Embeddings are
    #: optional (spec section 30): keyword search must work without them.
    embedding_id: str | None = None

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    #: Documents can be retired without deleting history.
    superseded_by_id: str | None = None
    #: Optional expiry, e.g. for a temporary exception.
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def _validate_authority(self) -> KnowledgeDocument:
        if self.source_type in _HUMAN_SOURCES and self.authority is KnowledgeAuthority.INFERRED:
            raise ValueError(
                f"knowledge from {self.source_type.value} cannot be INFERRED authority"
            )
        if (
            self.source_type is KnowledgeSourceType.VERIFIED_FINDING
            and self.authority is KnowledgeAuthority.INFERRED
        ):
            raise ValueError("a verified finding cannot be INFERRED authority")
        if self.expires_at is not None and self.expires_at < self.created_at:
            raise ValueError("expires_at precedes created_at")
        return self

    @property
    def authority_rank(self) -> int:
        return AUTHORITY_RANK[self.authority]

    def is_active(self, *, now: datetime | None = None) -> bool:
        if self.superseded_by_id is not None:
            return False
        if self.expires_at is None:
            return True
        return self.expires_at > (now or utcnow())

    def applies_to(self, repository_full_name: str) -> bool:
        return self.scope in {"*", repository_full_name}


class EngineeringDecision(ScoutModel):
    """An explicit, attributable engineering decision (spec section 29).

    Example from the spec: the reporting service intentionally bypasses normal user
    authorization because access is enforced at the gateway. Once recorded here, Scout
    must consider this decision before raising the same issue again.
    """

    id: str = Field(min_length=1)
    statement: str = Field(min_length=1, max_length=4000)
    rationale: str = Field(default="", max_length=8000)

    decided_by: str = Field(min_length=1, max_length=200)
    decided_at: datetime = Field(default_factory=utcnow)

    #: ``owner/name`` or ``*``.
    scope: str = Field(default="*", max_length=200)
    tags: list[str] = Field(default_factory=list)

    #: Provenance.
    source_question_id: str | None = None
    source_job_id: str | None = None
    source_answer_id: str | None = None

    #: Decision chaining, so a reversal is recorded rather than overwritten.
    supersedes_id: str | None = None
    superseded_by_id: str | None = None

    #: Documents derived from this decision, for retrieval.
    knowledge_document_ids: list[str] = Field(default_factory=list)

    @property
    def is_active(self) -> bool:
        return self.superseded_by_id is None

    def applies_to(self, repository_full_name: str) -> bool:
        return self.scope in {"*", repository_full_name}
