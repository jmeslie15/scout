"""Engineering memory persistence: documents, keyword search, and embeddings.

The keyword search here is PostgreSQL full-text search over a generated, weighted
``tsvector`` (title ``A``, body ``B``) rather than a ``LIKE '%term%'`` scan. Two reasons:
``LIKE`` cannot rank, so a document that mentions a term once in passing outranks one
about it; and a stems-and-stopwords index is what makes search usable on the vocabulary
engineers actually type ("retry", "retries", "retrying").

Embedding search is deliberately *not* implemented as a vector index. Spec section 30
makes embeddings optional and requires keyword search to work without them, so the schema
stores vectors as JSON and the ranking path here is lexical only. A future pgvector
column is an additive migration, not a rewrite.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Row, delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from scout_domain.ids import IdKind, new_id
from scout_domain.models.base import utcnow
from scout_domain.models.knowledge import (
    AUTHORITY_RANK,
    EngineeringDecision,
    KnowledgeAuthority,
    KnowledgeDocument,
    KnowledgeSourceType,
)
from scout_persistence.mapping import (
    from_engineering_decision,
    from_knowledge_document,
    to_engineering_decision,
    to_knowledge_document,
)
from scout_persistence.repositories.base import BaseRepository
from scout_persistence.tables import (
    EngineeringDecisionRow,
    KnowledgeDocumentRow,
    KnowledgeEmbeddingRow,
)

__all__ = ["ORGANISATION_SCOPE", "KnowledgeRepository"]


class KnowledgeRepository(BaseRepository):
    """Reads and writes ``knowledge_documents`` and ``engineering_decisions`` rows."""

    def __init__(self, session: AsyncSession) -> None:
        """Bind the repository to ``session``."""
        super().__init__(session)

    # -- documents --------------------------------------------------------------

    async def upsert(self, document: KnowledgeDocument) -> KnowledgeDocumentRow:
        """Insert or update a document by id, refreshing its search vector.

        Upsert rather than insert because the same knowledge is legitimately re-derived:
        a question answered again, a document re-indexed after an edit. A second insert
        would violate the primary key, and treating that as an error would force every
        caller to implement its own read-modify-write.

        The generated ``search_vector`` column is maintained by PostgreSQL, so an updated
        body is immediately searchable with no reindex step to forget.
        """
        row = await self.session.get(KnowledgeDocumentRow, document.id)
        if row is None:
            row = from_knowledge_document(document)
            self.session.add(row)
        else:
            row.title = document.title
            row.body = document.body
            row.source_type = document.source_type.value
            row.authority = document.authority.value
            row.authority_rank = AUTHORITY_RANK[document.authority]
            row.scope = document.scope
            row.tags = list(document.tags)
            row.source_reference = document.source_reference
            row.embedding_id = document.embedding_id
            row.superseded_by_id = document.superseded_by_id
            row.expires_at = document.expires_at
            self.stamp(row)
        await self.session.flush()
        return row

    async def get(self, document_id: str) -> KnowledgeDocumentRow | None:
        """Return the document row with ``document_id``, or ``None``."""
        return await self.session.get(KnowledgeDocumentRow, document_id)

    async def search_keyword(
        self,
        query: str,
        *,
        scope: str | None = None,
        limit: int = 20,
        now: datetime | None = None,
    ) -> list[KnowledgeDocumentRow]:
        """Search documents by keyword, best match first.

        ``plainto_tsquery`` is used rather than ``to_tsquery`` because the query text
        comes from an agent or an operator, and ``plainto_tsquery`` treats it as plain
        words — no syntax to get wrong, no operator injection, no error on a stray
        colon. Ranking combines the text rank with ``authority_rank`` so that a binding
        policy outranks a mere inference that happens to match slightly better, which is
        the ordering spec section 29 asks for.

        ``scope`` filters to one repository plus organisation-wide (``*``) knowledge:
        retrieval must never apply one service's decision to another, but must apply the
        org-wide ones everywhere.
        """
        parsed = func.plainto_tsquery("english", query)
        statement = select(KnowledgeDocumentRow).where(
            KnowledgeDocumentRow.search_vector.op("@@")(parsed)
        )
        if scope is not None:
            statement = statement.where(
                or_(KnowledgeDocumentRow.scope == scope, KnowledgeDocumentRow.scope == "*")
            )
        active = now or utcnow()
        statement = statement.where(
            KnowledgeDocumentRow.superseded_by_id.is_(None),
            or_(
                KnowledgeDocumentRow.expires_at.is_(None),
                KnowledgeDocumentRow.expires_at > active,
            ),
        )
        # Rank first, authority second. Relevance decides ordering; authority only
        # breaks ties between equally relevant documents, which is how a binding policy
        # beats an inference that happens to mention the same term exactly as often.
        statement = statement.order_by(
            func.ts_rank(KnowledgeDocumentRow.search_vector, parsed).desc(),
            KnowledgeDocumentRow.authority_rank.desc(),
            KnowledgeDocumentRow.created_at.desc(),
        ).limit(limit)
        result = await self.session.scalars(statement)
        return list(result)

    async def search_scored(
        self,
        query: str,
        *,
        scope: str | None = None,
        limit: int = 20,
    ) -> list[Row[tuple[KnowledgeDocumentRow, float]]]:
        """Return documents with their relevance score, for debugging retrieval.

        Exposed because "why did Scout not find the decision we recorded?" is a question
        that cannot be answered from a bare result list, and the score is the only
        evidence for why one document outranked another.
        """
        parsed = func.plainto_tsquery("english", query)
        rank = func.ts_rank(KnowledgeDocumentRow.search_vector, parsed)
        statement = (
            select(KnowledgeDocumentRow, rank.label("rank"))
            .where(KnowledgeDocumentRow.search_vector.op("@@")(parsed))
            .order_by(rank.desc(), KnowledgeDocumentRow.created_at.desc())
            .limit(limit)
        )
        if scope is not None:
            statement = statement.where(
                or_(KnowledgeDocumentRow.scope == scope, KnowledgeDocumentRow.scope == "*")
            )
        return list((await self.session.execute(statement)).all())

    async def list_active_for_scope(
        self,
        scope: str,
        *,
        limit: int = 100,
        now: datetime | None = None,
    ) -> list[KnowledgeDocumentRow]:
        """Return active documents for ``scope`` plus organisation-wide ones.

        "Active" excludes superseded documents and documents whose ``expires_at`` has
        passed: a temporary exception that expired must stop influencing verification
        without anyone having to remember to delete it.
        """
        active = now or utcnow()
        result = await self.session.scalars(
            select(KnowledgeDocumentRow)
            .where(
                or_(KnowledgeDocumentRow.scope == scope, KnowledgeDocumentRow.scope == "*"),
                KnowledgeDocumentRow.superseded_by_id.is_(None),
                or_(
                    KnowledgeDocumentRow.expires_at.is_(None),
                    KnowledgeDocumentRow.expires_at > active,
                ),
            )
            .order_by(
                KnowledgeDocumentRow.authority_rank.desc(),
                KnowledgeDocumentRow.created_at.desc(),
            )
            .limit(limit)
        )
        return list(result)

    async def list_by_authority(
        self,
        authority: KnowledgeAuthority,
    ) -> list[KnowledgeDocumentRow]:
        """Return every document with a given authority rank."""
        result = await self.session.scalars(
            select(KnowledgeDocumentRow)
            .where(KnowledgeDocumentRow.authority == authority.value)
            .order_by(KnowledgeDocumentRow.created_at.desc())
        )
        return list(result)

    async def supersede(self, document_id: str, *, by_document_id: str) -> KnowledgeDocumentRow:
        """Mark a document superseded, retaining it for history.

        Superseding rather than deleting is what lets Scout answer "what did we used to
        believe, and since when?" during an incident.
        """
        row = await self.session.get(KnowledgeDocumentRow, document_id)
        if row is None:
            raise LookupError(f"no knowledge document {document_id!r}")
        row.superseded_by_id = by_document_id
        self.stamp(row)
        await self.session.flush()
        return row

    async def count_active_for_scope(self, scope: str, *, now: datetime | None = None) -> int:
        """Return how many active documents apply to ``scope``."""
        active = now or utcnow()
        total = await self.session.scalar(
            select(func.count())
            .select_from(KnowledgeDocumentRow)
            .where(
                or_(KnowledgeDocumentRow.scope == scope, KnowledgeDocumentRow.scope == "*"),
                KnowledgeDocumentRow.superseded_by_id.is_(None),
                or_(
                    KnowledgeDocumentRow.expires_at.is_(None),
                    KnowledgeDocumentRow.expires_at > active,
                ),
            )
        )
        return int(total or 0)

    # -- embeddings -------------------------------------------------------------

    async def record_embedding(
        self,
        *,
        document_id: str,
        model: str,
        vector: list[float],
        content_digest: str | None = None,
    ) -> str:
        """Persist an embedding for ``document_id`` and link it back; return its id.

        Existing embeddings for the same document are deleted first: two vectors for one
        document would make similarity search ambiguous, and the newest is the only one
        that reflects the current text.
        """
        await self.session.execute(
            delete(KnowledgeEmbeddingRow).where(KnowledgeEmbeddingRow.document_id == document_id)
        )
        # `IdKind` has no embedding kind: spec section 42 declares a
        # `knowledge_embeddings` table but the domain never gave embeddings a
        # prefixed identifier. A `kdoc_` id keeps the value consistent with the
        # vocabulary that exists and cross-references the document it belongs to.
        embedding_id = new_id(IdKind.KNOWLEDGE_DOCUMENT)
        self.session.add(
            KnowledgeEmbeddingRow(
                id=embedding_id,
                document_id=document_id,
                model=model,
                dimensions=len(vector),
                vector=vector,
                content_digest=content_digest,
            )
        )
        row = await self.session.get(KnowledgeDocumentRow, document_id)
        if row is None:
            raise LookupError(f"no knowledge document {document_id!r}")
        row.embedding_id = embedding_id
        self.stamp(row)
        await self.session.flush()
        return embedding_id

    async def get_embedding(self, document_id: str) -> list[float] | None:
        """Return the stored vector for ``document_id``, or ``None``."""
        raw = await self.session.scalar(
            select(KnowledgeEmbeddingRow.vector)
            .where(KnowledgeEmbeddingRow.document_id == document_id)
            .order_by(KnowledgeEmbeddingRow.created_at.desc())
            .limit(1)
        )
        if raw is None:
            return None
        return [float(value) for value in raw]

    # -- engineering decisions --------------------------------------------------

    async def record_decision(self, decision: EngineeringDecision) -> EngineeringDecisionRow:
        """Insert an engineering decision and return its row.

        A decision is the strongest form of engineering memory Scout holds, so it is
        stored as its own row rather than as a knowledge document: it has provenance
        (which question, which answer, which job) that a document does not.
        """
        row = from_engineering_decision(decision)
        self.session.add(row)
        try:
            await self.session.flush()
        except IntegrityError as exc:  # pragma: no cover - surfaced with context
            raise ValueError(f"could not record engineering decision {decision.id!r}") from exc
        return row

    async def get_decision(self, decision_id: str) -> EngineeringDecisionRow | None:
        """Return the decision row with ``decision_id``, or ``None``."""
        return await self.session.get(EngineeringDecisionRow, decision_id)

    async def list_decisions_for_scope(
        self,
        scope: str,
        *,
        limit: int = 100,
    ) -> list[EngineeringDecisionRow]:
        """Return active decisions applying to ``scope``, newest first.

        Superseded decisions are excluded: a reversal must stop influencing
        verification the moment it is recorded, or Scout would keep citing a decision
        the team has already changed.
        """
        result = await self.session.scalars(
            select(EngineeringDecisionRow)
            .where(
                or_(EngineeringDecisionRow.scope == scope, EngineeringDecisionRow.scope == "*"),
                EngineeringDecisionRow.superseded_by_id.is_(None),
            )
            .order_by(EngineeringDecisionRow.decided_at.desc())
            .limit(limit)
        )
        return list(result)

    async def search_decisions(
        self, query: str, *, limit: int = 20
    ) -> list[EngineeringDecisionRow]:
        """Find decisions whose statement or rationale matches ``query``.

        Uses full-text search over the two text columns rather than a stored tsvector:
        decisions are far fewer than knowledge documents, and adding a second generated
        column plus index for them is not yet worth the migration.
        """
        parsed = func.plainto_tsquery("english", query)
        vector = func.to_tsvector(
            "english", EngineeringDecisionRow.statement + " " + EngineeringDecisionRow.rationale
        )
        result = await self.session.scalars(
            select(EngineeringDecisionRow)
            .where(vector.op("@@")(parsed))
            .order_by(EngineeringDecisionRow.decided_at.desc())
            .limit(limit)
        )
        return list(result)

    @staticmethod
    def to_domain(row: KnowledgeDocumentRow) -> KnowledgeDocument:
        """Convert a knowledge-document row into its domain model."""
        return to_knowledge_document(row)

    @staticmethod
    def decision_to_domain(row: EngineeringDecisionRow) -> EngineeringDecision:
        """Convert an engineering-decision row into its domain model."""
        return to_engineering_decision(row)

    @staticmethod
    def source_type_from(value: str) -> KnowledgeSourceType:
        """Convert a stored ``source_type`` string back into its enum member."""
        return KnowledgeSourceType(value)


#: The scope value that means "applies to every repository".
ORGANISATION_SCOPE = "*"
