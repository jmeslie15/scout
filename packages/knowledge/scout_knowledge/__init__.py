"""Engineering memory: decisions, documents, and authority-weighted retrieval.

Public surface of the package. Retrieval works without embeddings or a vector database
(spec section 30): authority-weighted keyword search is the baseline, and
``KnowledgeDocument.embedding_id`` is an optional enhancement that nothing here requires.

Layering: this package depends on ``scout_domain`` only. It deliberately does not import
``scout_persistence``; :class:`~scout_knowledge.store.PostgresKnowledgeStore` talks to a
narrow session protocol instead, so the package is testable offline and the persistence
workstream can evolve independently.
"""

from __future__ import annotations

from scout_knowledge.decisions import DecisionRegistry
from scout_knowledge.ingest import (
    ADR_AUTHORITY,
    AdrStatus,
    ingest_adr,
    ingest_documentation_pages,
    ingest_repository_documentation,
    parse_adr_status,
    parse_document_title,
    stable_document_id,
)
from scout_knowledge.models import (
    DEFAULT_RETRIEVAL_LIMIT,
    MAX_RETRIEVAL_LIMIT,
    RankedDocument,
    RetrievalQuery,
    RetrievalResult,
)
from scout_knowledge.ranking import (
    AUTHORITY_STEP,
    AUTHORITY_WEIGHT,
    LEXICAL_WEIGHT,
    PHRASE_WEIGHT,
    RECENCY_HALF_LIFE_DAYS,
    RECENCY_WEIGHT,
    SCOPE_WEIGHT,
    LexicalMatch,
    is_eligible,
    lexical_relevance,
    rank_documents,
    recency_score,
    scope_score,
    score_document,
    tokenize,
)
from scout_knowledge.store import (
    TABLE_COLUMNS,
    AsyncSessionLike,
    InMemoryKnowledgeStore,
    KnowledgeStore,
    PostgresKnowledgeStore,
)

__all__ = [
    "ADR_AUTHORITY",
    "AUTHORITY_STEP",
    "AUTHORITY_WEIGHT",
    "DEFAULT_RETRIEVAL_LIMIT",
    "LEXICAL_WEIGHT",
    "MAX_RETRIEVAL_LIMIT",
    "PHRASE_WEIGHT",
    "RECENCY_HALF_LIFE_DAYS",
    "RECENCY_WEIGHT",
    "SCOPE_WEIGHT",
    "TABLE_COLUMNS",
    "AdrStatus",
    "AsyncSessionLike",
    "DecisionRegistry",
    "InMemoryKnowledgeStore",
    "KnowledgeStore",
    "LexicalMatch",
    "PostgresKnowledgeStore",
    "RankedDocument",
    "RetrievalQuery",
    "RetrievalResult",
    "ingest_adr",
    "ingest_documentation_pages",
    "ingest_repository_documentation",
    "is_eligible",
    "lexical_relevance",
    "parse_adr_status",
    "parse_document_title",
    "rank_documents",
    "recency_score",
    "scope_score",
    "score_document",
    "stable_document_id",
    "tokenize",
]
