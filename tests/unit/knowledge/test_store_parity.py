"""Both stores must behave identically for the same corpus.

``PostgresKnowledgeStore`` is exercised against a fake async session that emulates the
documented table shape and a simple full-text index. The assertions that matter are the
ones comparing it with ``InMemoryKnowledgeStore``: if the two diverge, retrieval changes
meaning depending on which configuration is deployed, which is precisely how engineering
memory stops being trustworthy.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import postgresql

from scout_domain.models.knowledge import (
    KnowledgeAuthority,
    KnowledgeDocument,
    KnowledgeSourceType,
)
from scout_knowledge import store as store_module
from scout_knowledge.models import RetrievalQuery
from scout_knowledge.ranking import tokenize
from scout_knowledge.store import (
    InMemoryKnowledgeStore,
    KnowledgeStore,
    PostgresKnowledgeStore,
)

StoreFactory = Callable[[Sequence[KnowledgeDocument]], dict[str, KnowledgeStore]]


def _as_row(document: KnowledgeDocument) -> dict[str, Any]:
    """Render a document the way the documented table shape stores it."""
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


class _FakeMappings:
    def __init__(self, rows: Sequence[Mapping[str, Any]]) -> None:
        self._rows = list(rows)

    def all(self) -> list[Mapping[str, Any]]:
        return list(self._rows)

    def first(self) -> Mapping[str, Any] | None:
        return self._rows[0] if self._rows else None


class _FakeResult:
    def __init__(self, rows: Sequence[Mapping[str, Any]] = (), *, rowcount: int = 0) -> None:
        self._rows = list(rows)
        self.rowcount = rowcount

    def mappings(self) -> _FakeMappings:
        return _FakeMappings(self._rows)


class FakeKnowledgeSession:
    """A minimal stand-in for an async SQLAlchemy session over ``knowledge_documents``.

    It is a test double for the *database*, not for the store: the store still builds real
    SQL with ``text()``, and this object recognises the statements it is sent so the SQL
    path (parameter binding, row shape) is covered without a live PostgreSQL.
    """

    def __init__(self, rows: Sequence[Mapping[str, Any]] | None = None) -> None:
        self.rows: dict[str, dict[str, Any]] = {str(row["id"]): dict(row) for row in rows or ()}
        self.statements: list[str] = []
        self.commits = 0
        self.flushes = 0

    async def execute(
        self,
        statement: Any,
        parameters: Mapping[str, Any] | None = None,
    ) -> _FakeResult:
        sql = str(statement)
        params = dict(parameters or {})
        self.statements.append(sql)

        if sql.lstrip().upper().startswith("INSERT"):
            self.rows[str(params["id"])] = params
            return _FakeResult(rowcount=1)
        if sql.lstrip().upper().startswith("UPDATE"):
            row = self.rows.get(str(params["old_id"]))
            if row is None:
                return _FakeResult(rowcount=0)
            row["superseded_by_id"] = params["new_id"]
            row["updated_at"] = params["now"]
            return _FakeResult(rowcount=1)
        if "plainto_tsquery" in sql:
            return _FakeResult(self._search(sql, params))
        if "WHERE id = :id" in sql:
            row = self.rows.get(str(params["id"]))
            return _FakeResult([row] if row is not None else [])
        if "ORDER BY created_at DESC" in sql:
            return _FakeResult(self._list_active(params))
        msg = f"unexpected SQL issued by the store: {sql[:80]!r}"
        raise AssertionError(msg)

    async def commit(self) -> None:
        self.commits += 1

    async def flush(self) -> None:
        self.flushes += 1

    def _list_active(self, params: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        scope = str(params["scope"])
        moment = params["now"]
        rows = [
            row
            for row in self.rows.values()
            if row["superseded_by_id"] is None
            and (row["expires_at"] is None or row["expires_at"] > moment)
            and (row["scope"] == scope or row["scope"] == "*")
        ]
        rows.sort(key=lambda row: (row["created_at"], str(row["id"])), reverse=True)
        return rows

    def _search(self, sql: str, params: Mapping[str, Any]) -> list[Mapping[str, Any]]:
        query_terms = set(tokenize(str(params["query_text"])))
        scope = str(params["scope"])
        moment = params["now"]
        enforce_expiry = "expires_at > :now" in sql
        types = params.get("document_types")

        candidates: list[Mapping[str, Any]] = []
        for row in self.rows.values():
            if row["superseded_by_id"] is not None:
                continue
            if enforce_expiry and row["expires_at"] is not None and row["expires_at"] <= moment:
                continue
            if row["scope"] != scope and row["scope"] != "*":
                continue
            if types is not None and row["source_type"] not in types:
                continue
            document_terms = set(tokenize(f"{row['title']} {row['body']}"))
            if query_terms and not query_terms & document_terms:
                continue
            candidates.append(row)

        # Emulate ``ORDER BY lexical_rank DESC, id ASC`` well enough to exercise LIMIT.
        candidates.sort(key=lambda row: str(row["id"]))
        limit = int(params.get("candidate_limit", len(candidates)))
        return candidates[:limit]


@pytest.fixture
def store_factory() -> StoreFactory:
    """Build both store implementations over the same corpus."""

    def _build(corpus: Sequence[KnowledgeDocument]) -> dict[str, KnowledgeStore]:
        return {
            "memory": InMemoryKnowledgeStore(corpus),
            "postgres": PostgresKnowledgeStore(FakeKnowledgeSession([_as_row(d) for d in corpus])),
        }

    return _build


@pytest.fixture(params=["memory", "postgres"])
def store_kind(request: pytest.FixtureRequest) -> str:
    """Parametrise a test over both store implementations."""
    return str(request.param)


CORPUS_QUERY = "database migration strategy"


def _corpus(make_document) -> list[KnowledgeDocument]:
    return [
        make_document(
            id="kdoc_policy",
            title="Database change policy",
            body="Every database migration strategy must be reviewed by the platform team.",
            authority=KnowledgeAuthority.POLICY,
            source_type=KnowledgeSourceType.ENGINEERING_POLICY,
            scope="*",
        ),
        make_document(
            id="kdoc_decision",
            title="Migration tooling",
            body="We adopt expand-contract as the database migration strategy for this service.",
            authority=KnowledgeAuthority.HUMAN_DECISION,
            source_type=KnowledgeSourceType.HUMAN_ANSWER,
            scope="acme/api",
        ),
        make_document(
            id="kdoc_doc",
            title="Migration guide",
            body="The database migration strategy is documented here for onboarding.",
            authority=KnowledgeAuthority.REPOSITORY_DOC,
            source_type=KnowledgeSourceType.REPOSITORY_DOCUMENTATION,
            scope="acme/api",
        ),
        make_document(
            id="kdoc_inferred",
            title="Generated summary",
            body="A strategy for database migration might exist somewhere.",
            authority=KnowledgeAuthority.INFERRED,
            source_type=KnowledgeSourceType.REPOSITORY_DOCUMENTATION,
            scope="acme/api",
        ),
        make_document(
            id="kdoc_other_repo",
            title="Other service migration",
            body="Database migration strategy for an unrelated service.",
            authority=KnowledgeAuthority.POLICY,
            source_type=KnowledgeSourceType.ENGINEERING_POLICY,
            scope="other/service",
        ),
        make_document(
            id="kdoc_unrelated",
            title="Frontend bundling",
            body="We use esbuild for the dashboard bundle.",
            authority=KnowledgeAuthority.POLICY,
            source_type=KnowledgeSourceType.ENGINEERING_POLICY,
            scope="acme/api",
        ),
    ]


async def test_both_stores_rank_the_same_corpus_identically(
    make_document, now, store_factory
) -> None:
    corpus = _corpus(make_document)
    stores = store_factory(corpus)
    query = RetrievalQuery(text=CORPUS_QUERY, repository_full_name="acme/api", limit=5)

    memory = await stores["memory"].search(query, now=now)
    postgres = await stores["postgres"].search(query, now=now)

    memory_view = [(item.document.id, round(item.score, 12)) for item in memory.items]
    postgres_view = [(item.document.id, round(item.score, 12)) for item in postgres.items]
    assert memory_view == postgres_view
    assert memory.total_candidates == postgres.total_candidates
    assert memory.strategy == postgres.strategy


async def test_both_stores_promote_authority_over_relevance(
    make_document, now, store_factory
) -> None:
    stores = store_factory(_corpus(make_document))
    query = RetrievalQuery(text=CORPUS_QUERY, repository_full_name="acme/api", limit=5)

    for store in stores.values():
        result = await store.search(query, now=now)
        assert result.items[0].document.id == "kdoc_policy"
        assert "kdoc_other_repo" not in {item.document.id for item in result.items}
        assert "kdoc_unrelated" not in {item.document.id for item in result.items}


async def test_both_stores_upsert_and_get(make_document, now, store_kind, store_factory) -> None:
    store = store_factory([])[store_kind]
    document = make_document(id="kdoc_roundtrip", title="Round trip", body="Body text")

    returned = await store.upsert(document)
    fetched = await store.get("kdoc_roundtrip")

    assert returned == document
    assert fetched is not None
    assert fetched.id == document.id
    assert fetched.title == document.title
    assert fetched.authority is KnowledgeAuthority.REPOSITORY_DOC
    assert await store.get("kdoc_missing") is None


async def test_both_stores_update_an_existing_document(
    make_document, now, store_kind, store_factory
) -> None:
    store = store_factory([])[store_kind]
    original = make_document(id="kdoc_update", title="Original", body="Original body")
    await store.upsert(original)

    revised = original.evolve(title="Revised", body="Revised body")
    await store.upsert(revised)

    fetched = await store.get("kdoc_update")
    assert fetched is not None
    assert fetched.title == "Revised"
    assert fetched.body == "Revised body"


async def test_both_stores_supersede_and_exclude(
    make_document, now, store_kind, store_factory
) -> None:
    old = make_document(id="kdoc_old", body="connection pooling limits")
    new = make_document(id="kdoc_new", body="connection pooling limits revised")
    store = store_factory([old, new])[store_kind]

    await store.supersede("kdoc_old", "kdoc_new")

    fetched = await store.get("kdoc_old")
    assert fetched is not None
    assert fetched.superseded_by_id == "kdoc_new"

    result = await store.search(
        RetrievalQuery(text="connection pooling limits", repository_full_name="acme/api"),
        now=now,
    )
    assert "kdoc_old" not in {item.document.id for item in result.items}


async def test_both_stores_reject_superseding_an_unknown_document(
    make_document, store_kind, store_factory
) -> None:
    store = store_factory([])[store_kind]
    with pytest.raises(KeyError, match="unknown knowledge document"):
        await store.supersede("kdoc_missing", "kdoc_any")


async def test_both_stores_exclude_expired_and_superseded(
    make_document, now, store_kind, store_factory
) -> None:
    live = make_document(id="kdoc_live", body="retention policy")
    superseded = make_document(
        id="kdoc_superseded", body="retention policy", superseded_by_id="kdoc_live"
    )
    expired = make_document(
        id="kdoc_expired",
        body="retention policy",
        expires_at=now - timedelta(days=1),
    )
    store = store_factory([live, superseded, expired])[store_kind]

    result = await store.search(
        RetrievalQuery(text="retention policy", repository_full_name="acme/api"),
        now=now,
    )

    assert [item.document.id for item in result.items] == ["kdoc_live"]


async def test_both_stores_include_expired_when_asked(
    make_document, now, store_kind, store_factory
) -> None:
    expired = make_document(
        id="kdoc_expired",
        body="temporary exception",
        expires_at=now - timedelta(days=1),
    )
    store = store_factory([expired])[store_kind]

    result = await store.search(
        RetrievalQuery(
            text="temporary exception",
            repository_full_name="acme/api",
            include_expired=True,
        ),
        now=now,
    )

    assert [item.document.id for item in result.items] == ["kdoc_expired"]


async def test_both_stores_list_active_for_scope(
    make_document, now, store_kind, store_factory
) -> None:
    corpus = [
        make_document(id="kdoc_org", scope="*", body="org wide"),
        make_document(id="kdoc_exact", scope="acme/api", body="exact"),
        make_document(id="kdoc_other", scope="other/service", body="other"),
        make_document(id="kdoc_gone", scope="acme/api", body="gone", superseded_by_id="kdoc_exact"),
        make_document(
            id="kdoc_expired",
            scope="acme/api",
            body="expired",
            expires_at=now - timedelta(days=1),
        ),
    ]
    store = store_factory(corpus)[store_kind]

    active = await store.list_active_for_scope("acme/api", now=now)

    assert {document.id for document in active} == {"kdoc_org", "kdoc_exact"}


async def test_postgres_search_uses_postgres_full_text_search(make_document, now) -> None:
    """The SQL path must actually use ``to_tsvector``/``plainto_tsquery``/``ts_rank``."""
    session = FakeKnowledgeSession([_as_row(make_document(id="kdoc_fts", body="full text search"))])
    store = PostgresKnowledgeStore(session)

    await store.search(
        RetrievalQuery(text="full text search", repository_full_name="acme/api"),
        now=now,
    )

    search_sql = next(sql for sql in session.statements if "plainto_tsquery" in sql)
    assert "to_tsvector('english', title || ' ' || body)" in search_sql
    assert "ts_rank" in search_sql
    assert "LIMIT :candidate_limit" in search_sql


async def test_postgres_store_exposes_commit(make_document) -> None:
    session = FakeKnowledgeSession()
    store = PostgresKnowledgeStore(session)

    await store.upsert(make_document(id="kdoc_commit"))
    await store.commit()

    assert session.commits == 1
    assert session.flushes == 1


def test_every_postgres_statement_compiles_for_the_postgres_dialect() -> None:
    """Catch SQL typos without a live database by compiling against the real dialect."""
    dialect = postgresql.dialect()

    for sql in (
        store_module._SELECT_BY_ID_SQL,
        store_module._SELECT_FOR_SCOPE_SQL,
        store_module._UPSERT_SQL,
        store_module._SUPERSEDE_SQL,
    ):
        compiled = str(text(sql).compile(dialect=dialect))
        assert "knowledge_documents" in compiled


def test_search_statement_compiles_with_and_without_document_types(now) -> None:
    store = PostgresKnowledgeStore(FakeKnowledgeSession())
    dialect = postgresql.dialect()

    unfiltered, _ = store._build_search_statement(
        RetrievalQuery(text="full text", repository_full_name="acme/api"),
        now=now,
    )
    filtered, parameters = store._build_search_statement(
        RetrievalQuery(
            text="full text",
            repository_full_name="acme/api",
            document_types=frozenset(
                {KnowledgeSourceType.ADR, KnowledgeSourceType.REPOSITORY_DOCUMENTATION}
            ),
        ),
        now=now,
    )

    compiled = str(filtered.compile(dialect=dialect))
    assert "to_tsvector" in compiled
    assert "plainto_tsquery" in compiled
    assert "IN (" in compiled
    assert set(parameters["document_types"]) == {"ADR", "REPOSITORY_DOCUMENTATION"}
    assert "IN (" not in str(unfiltered.compile(dialect=dialect))
