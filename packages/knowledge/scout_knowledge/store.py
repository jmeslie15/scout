"""Knowledge storage: one protocol, two interchangeable implementations (spec section 30).

Rule 2 requires every external integration to sit behind an interface, and Rule 1 forbids
introducing infrastructure before it is needed. Together they mean: retrieval must work
over PostgreSQL keyword search, with no vector database, and the store must be
substitutable so the offline path and the production path cannot drift.

The two implementations share :func:`scout_knowledge.ranking.rank_documents` for all
eligibility and ordering decisions. The PostgreSQL implementation adds full-text search
(``to_tsvector('english', title || ' ' || body)`` against ``plainto_tsquery``) purely to
*narrow the candidate set* in the database; ``ts_rank`` is selected for the round trip but
is not the ranking. Ranking happens in Python because:

* the authority-weighted function is the domain invariant (spec section 29) and it must
  have exactly one implementation;
* ``ts_rank`` knows nothing about ``KnowledgeAuthority``, scope, or recency, so any
  ordering it produced would have to be redone anyway;
* two orderings computed by two engines would eventually disagree.

**Table shape expected** (``knowledge_documents``)::

    id                text primary key
    title             text not null
    body              text not null
    source_type       text not null
    authority         text not null
    scope             text not null default '*'
    tags              jsonb not null default '[]'
    source_reference  text null
    embedding_id      text null
    created_at        timestamptz not null
    updated_at        timestamptz not null
    superseded_by_id  text null
    expires_at        timestamptz null

``scout_persistence`` owns the SQLAlchemy models and migrations for this table. This
module intentionally does **not** import it: the workstream that owns persistence is
separate, and a narrow session protocol plus raw ``text()`` SQL keeps the dependency
one-way and testable without a database.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from sqlalchemy import bindparam, text
from sqlalchemy.sql import TextClause

from scout_domain.models.base import utcnow
from scout_domain.models.knowledge import KnowledgeDocument
from scout_knowledge.models import RetrievalQuery, RetrievalResult
from scout_knowledge.ranking import rank_documents

__all__ = [
    "TABLE_COLUMNS",
    "AsyncSessionLike",
    "InMemoryKnowledgeStore",
    "KnowledgeStore",
    "MappingResultLike",
    "PostgresKnowledgeStore",
    "ResultLike",
]

#: The documented column shape of ``knowledge_documents`` this store reads and writes.
TABLE_COLUMNS: tuple[str, ...] = (
    "id",
    "title",
    "body",
    "source_type",
    "authority",
    "scope",
    "tags",
    "source_reference",
    "embedding_id",
    "created_at",
    "updated_at",
    "superseded_by_id",
    "expires_at",
)

_COLUMN_LIST = ", ".join(TABLE_COLUMNS)

#: How many candidates the database returns per requested result. The SQL ``LIMIT`` is a
#: candidate bound, not the final answer: the Python ranker may promote a document that
#: ``ts_rank`` placed lower, so the pool must be wider than ``query.limit``.
CANDIDATE_OVERFETCH = 5

#: Floor for the candidate pool, so a small ``limit`` still gives the authority ranker
#: room to reorder.
MIN_CANDIDATE_POOL = 50

_SELECT_BY_ID_SQL = f"SELECT {_COLUMN_LIST} FROM knowledge_documents WHERE id = :id"

_SELECT_FOR_SCOPE_SQL = (
    f"SELECT {_COLUMN_LIST} FROM knowledge_documents "
    "WHERE superseded_by_id IS NULL "
    "AND (expires_at IS NULL OR expires_at > :now) "
    "AND (scope = :scope OR scope = '*') "
    "ORDER BY created_at DESC, id ASC"
)

_UPSERT_SQL = f"""
INSERT INTO knowledge_documents ({_COLUMN_LIST})
VALUES (
    :id,
    :title,
    :body,
    :source_type,
    :authority,
    :scope,
    CAST(:tags AS jsonb),
    :source_reference,
    :embedding_id,
    :created_at,
    :updated_at,
    :superseded_by_id,
    :expires_at
)
ON CONFLICT (id) DO UPDATE SET
    title = EXCLUDED.title,
    body = EXCLUDED.body,
    source_type = EXCLUDED.source_type,
    authority = EXCLUDED.authority,
    scope = EXCLUDED.scope,
    tags = EXCLUDED.tags,
    source_reference = EXCLUDED.source_reference,
    embedding_id = EXCLUDED.embedding_id,
    updated_at = EXCLUDED.updated_at,
    superseded_by_id = EXCLUDED.superseded_by_id,
    expires_at = EXCLUDED.expires_at
"""

_SUPERSEDE_SQL = (
    "UPDATE knowledge_documents SET superseded_by_id = :new_id, updated_at = :now "
    "WHERE id = :old_id"
)


# ---------------------------------------------------------------------------------
# Narrow protocols. These are the only things this module knows about persistence.
# ---------------------------------------------------------------------------------


@runtime_checkable
class MappingResultLike(Protocol):
    """The slice of an SQLAlchemy ``MappingResult`` this store consumes."""

    def all(self) -> Sequence[Mapping[str, Any]]:
        """Return every row as a string-keyed mapping."""
        ...

    def first(self) -> Mapping[str, Any] | None:
        """Return the first row as a string-keyed mapping, or ``None``."""
        ...


@runtime_checkable
class ResultLike(Protocol):
    """The slice of an SQLAlchemy ``Result`` this store consumes."""

    @property
    def rowcount(self) -> int:
        """Number of rows affected by a DML statement."""
        ...

    def mappings(self) -> MappingResultLike:
        """Return the result as mappings rather than positional tuples."""
        ...


@runtime_checkable
class AsyncSessionLike(Protocol):
    """The narrow slice of an async SQLAlchemy session this store needs.

    Deliberately not ``scout_persistence``'s session type: depending on the concrete
    session would couple knowledge retrieval to another workstream's package, and a fake
    implementing these three methods is enough to test the SQL path offline.
    """

    async def execute(
        self,
        statement: TextClause,
        parameters: Mapping[str, Any] | None = None,
    ) -> ResultLike:
        """Execute a statement and return its result."""
        ...

    async def commit(self) -> None:
        """Commit the current transaction."""
        ...

    async def flush(self) -> None:
        """Flush pending unit-of-work state to the transaction."""
        ...


@runtime_checkable
class KnowledgeStore(Protocol):
    """Persistence interface for engineering memory.

    Both implementations must apply identical scope, expiry, and supersession rules; the
    shared ranking module is how that is guaranteed rather than promised.
    """

    async def upsert(self, document: KnowledgeDocument) -> KnowledgeDocument:
        """Insert or replace ``document`` by id, returning the stored value."""
        ...

    async def get(self, document_id: str) -> KnowledgeDocument | None:
        """Return one document by id, including superseded or expired ones."""
        ...

    async def list_active_for_scope(
        self,
        scope: str,
        *,
        now: datetime | None = None,
    ) -> list[KnowledgeDocument]:
        """Return active documents applicable to ``scope`` (exact or organisation-wide)."""
        ...

    async def supersede(self, old_id: str, new_id: str) -> None:
        """Point ``old_id`` at ``new_id`` without deleting history."""
        ...

    async def search(
        self,
        query: RetrievalQuery,
        *,
        now: datetime | None = None,
    ) -> RetrievalResult:
        """Rank the documents applicable to ``query``."""
        ...


# ---------------------------------------------------------------------------------
# Shared row helpers
# ---------------------------------------------------------------------------------


def _decode_tags(value: Any) -> list[str]:
    """Normalise a JSONB ``tags`` column into a list of strings.

    asyncpg hands back a decoded list for ``jsonb``; a text driver may hand back the raw
    JSON string. Accepting both keeps the store portable across drivers without a second
    code path.
    """
    if value is None:
        return []
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return []
        if isinstance(decoded, list):
            return [str(item) for item in decoded]
        return []
    if isinstance(value, Sequence):
        return [str(item) for item in value]
    return []


def _document_from_row(row: Mapping[str, Any]) -> KnowledgeDocument:
    """Validate a database row into a :class:`KnowledgeDocument`."""
    return KnowledgeDocument(
        id=str(row["id"]),
        title=str(row["title"]),
        body=str(row["body"]),
        source_type=str(row["source_type"]),
        authority=str(row["authority"]),
        scope=str(row["scope"]),
        tags=_decode_tags(row["tags"]),
        source_reference=row["source_reference"],
        embedding_id=row["embedding_id"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        superseded_by_id=row["superseded_by_id"],
        expires_at=row["expires_at"],
    )


def _bind_parameters(document: KnowledgeDocument) -> dict[str, Any]:
    """Render ``document`` as bind parameters for the documented table shape."""
    return {
        "id": document.id,
        "title": document.title,
        "body": document.body,
        "source_type": document.source_type.value,
        "authority": document.authority.value,
        "scope": document.scope,
        "tags": json.dumps(document.tags),
        "source_reference": document.source_reference,
        "embedding_id": document.embedding_id,
        "created_at": document.created_at,
        "updated_at": document.updated_at,
        "superseded_by_id": document.superseded_by_id,
        "expires_at": document.expires_at,
    }


# ---------------------------------------------------------------------------------
# In-memory implementation
# ---------------------------------------------------------------------------------


class InMemoryKnowledgeStore:
    """Dict-backed store used by tests and by the offline default configuration.

    It implements the *same* eligibility and ranking path as the PostgreSQL store, so a
    passing unit test is evidence about production behaviour rather than about a
    simplified double.
    """

    def __init__(self, documents: Sequence[KnowledgeDocument] | None = None) -> None:
        self._documents: dict[str, KnowledgeDocument] = {}
        for document in documents or ():
            self._documents[document.id] = document

    @property
    def documents(self) -> list[KnowledgeDocument]:
        """A snapshot of everything stored, for assertions and the dashboard."""
        return list(self._documents.values())

    async def upsert(self, document: KnowledgeDocument) -> KnowledgeDocument:
        """Insert or replace ``document`` by id."""
        self._documents[document.id] = document
        return document

    async def get(self, document_id: str) -> KnowledgeDocument | None:
        """Return one document by id, including superseded or expired ones."""
        return self._documents.get(document_id)

    async def list_active_for_scope(
        self,
        scope: str,
        *,
        now: datetime | None = None,
    ) -> list[KnowledgeDocument]:
        """Return active documents applicable to ``scope``."""
        moment = now or utcnow()
        return [
            document
            for document in self._documents.values()
            if document.is_active(now=moment) and document.applies_to(scope)
        ]

    async def supersede(self, old_id: str, new_id: str) -> None:
        """Point ``old_id`` at ``new_id``.

        The replacement must already exist, matching the ``INSERT ... ON CONFLICT`` order
        the PostgreSQL store requires. Superseding an unknown document raises, because a
        silent no-op would leave a stale decision retrievable.
        """
        document = self._documents.get(old_id)
        if document is None:
            raise KeyError(f"unknown knowledge document: {old_id!r}")
        self._documents[old_id] = document.evolve(superseded_by_id=new_id, updated_at=utcnow())

    async def search(
        self,
        query: RetrievalQuery,
        *,
        now: datetime | None = None,
    ) -> RetrievalResult:
        """Rank the documents applicable to ``query``."""
        return rank_documents(query, list(self._documents.values()), now=now)


# ---------------------------------------------------------------------------------
# PostgreSQL implementation
# ---------------------------------------------------------------------------------


class PostgresKnowledgeStore:
    """PostgreSQL-backed store using full-text search for candidate selection.

    Write methods ``flush`` but do not commit: transaction boundaries belong to the
    caller's unit of work (Rule 13, explicit interfaces). :meth:`commit` is offered for
    standalone callers that own no unit of work.
    """

    def __init__(self, session: AsyncSessionLike) -> None:
        self._session = session

    async def commit(self) -> None:
        """Commit the current transaction."""
        await self._session.commit()

    async def upsert(self, document: KnowledgeDocument) -> KnowledgeDocument:
        """Insert or replace ``document`` by id."""
        await self._session.execute(text(_UPSERT_SQL), _bind_parameters(document))
        await self._session.flush()
        return document

    async def get(self, document_id: str) -> KnowledgeDocument | None:
        """Return one document by id, including superseded or expired ones."""
        result = await self._session.execute(text(_SELECT_BY_ID_SQL), {"id": document_id})
        row = result.mappings().first()
        return None if row is None else _document_from_row(row)

    async def list_active_for_scope(
        self,
        scope: str,
        *,
        now: datetime | None = None,
    ) -> list[KnowledgeDocument]:
        """Return active documents applicable to ``scope``."""
        moment = now or utcnow()
        result = await self._session.execute(
            text(_SELECT_FOR_SCOPE_SQL),
            {"scope": scope, "now": moment},
        )
        return [_document_from_row(row) for row in result.mappings().all()]

    async def supersede(self, old_id: str, new_id: str) -> None:
        """Point ``old_id`` at ``new_id`` without deleting history."""
        result = await self._session.execute(
            text(_SUPERSEDE_SQL),
            {"old_id": old_id, "new_id": new_id, "now": utcnow()},
        )
        if result.rowcount == 0:
            raise KeyError(f"unknown knowledge document: {old_id!r}")
        await self._session.flush()

    async def search(
        self,
        query: RetrievalQuery,
        *,
        now: datetime | None = None,
    ) -> RetrievalResult:
        """Select FTS candidates in SQL, then rank them with the shared Python ranker."""
        moment = now or utcnow()
        statement, parameters = self._build_search_statement(query, now=moment)
        result = await self._session.execute(statement, parameters)
        candidates = [_document_from_row(row) for row in result.mappings().all()]
        return rank_documents(query, candidates, now=moment)

    def _build_search_statement(
        self,
        query: RetrievalQuery,
        *,
        now: datetime,
    ) -> tuple[TextClause, dict[str, Any]]:
        """Build the candidate-selection query and its bind parameters.

        The ``ts_rank`` projection is retained so the SQL round trip is readable and so a
        future migration can compare the two orderings; it is *not* the result ordering.
        """
        clauses = [
            (
                "to_tsvector('english', title || ' ' || body) "
                "@@ plainto_tsquery('english', :query_text)"
            ),
            "(scope = :scope OR scope = '*')",
        ]
        if not query.include_expired:
            clauses.append("(expires_at IS NULL OR expires_at > :now)")
        if query.document_types is not None:
            clauses.append("source_type IN :document_types")

        parameters: dict[str, Any] = {
            "query_text": query.text,
            "scope": query.repository_full_name,
            "now": now,
            "candidate_limit": max(query.limit * CANDIDATE_OVERFETCH, MIN_CANDIDATE_POOL),
        }
        if query.document_types is not None:
            parameters["document_types"] = tuple(
                source_type.value for source_type in query.document_types
            )

        where = " AND ".join(clauses)
        sql = (
            f"SELECT {_COLUMN_LIST}, "
            "ts_rank(to_tsvector('english', title || ' ' || body), "
            "plainto_tsquery('english', :query_text)) AS lexical_rank "
            "FROM knowledge_documents "
            f"WHERE superseded_by_id IS NULL AND {where} "
            "ORDER BY lexical_rank DESC, id ASC "
            "LIMIT :candidate_limit"
        )
        statement = text(sql)
        if query.document_types is not None:
            statement = statement.bindparams(bindparam("document_types", expanding=True))
        return statement, parameters
