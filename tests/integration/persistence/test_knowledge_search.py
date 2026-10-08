"""Knowledge persistence: keyword search, scope filtering, and retrieval.

Keyword search must work with no embeddings at all (spec section 30), which is why this
file is the one that matters for engineering memory. The implementation uses PostgreSQL
full-text search over a generated, weighted ``tsvector`` rather than ``LIKE``: ``LIKE``
cannot rank, cannot stem ("retry" does not match "retries"), and gives a document that
mentions a term once the same weight as one that is about it.

``plainto_tsquery`` is used rather than ``to_tsquery`` because the query text comes from an
agent or an operator. Plain-word parsing means there is no syntax to get wrong and no
operator injection, so a stray colon is a search term rather than a database error.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from scout_domain.models.base import utcnow
from scout_domain.models.knowledge import (
    EngineeringDecision,
    KnowledgeAuthority,
    KnowledgeDocument,
    KnowledgeSourceType,
)
from scout_persistence.repositories.knowledge import (
    ORGANISATION_SCOPE,
    KnowledgeRepository,
)

pytestmark = pytest.mark.integration


async def test_keyword_search_finds_a_document_by_a_term_in_its_body(
    session: AsyncSession, document_factory: object
) -> None:
    """A term that appears only in the body is findable.

    This is the headline requirement: an engineer searching for "exponential backoff" must
    reach the decision that explains it, even though the phrase is not in the title.
    """
    repository = KnowledgeRepository(session)
    await repository.upsert(  # type: ignore[operator]
        document_factory(
            title="Exporter resilience",
            body="The exporter retries with exponential backoff and a jittered delay.",
        )
    )
    await repository.upsert(  # type: ignore[operator]
        document_factory(title="Unrelated", body="Nothing to see here about widgets.")
    )

    results = await repository.search_keyword("exponential backoff")

    assert [row.title for row in results] == ["Exporter resilience"]


async def test_keyword_search_stems_words(session: AsyncSession, document_factory: object) -> None:
    """Morphology is handled: searching "retries" finds "retry" and vice versa."""
    repository = KnowledgeRepository(session)
    await repository.upsert(  # type: ignore[operator]
        document_factory(title="Retry policy", body="Scout retries a failed stage twice.")
    )

    for query in ("retry", "retries", "retrying", "retried"):
        results = await repository.search_keyword(query)
        assert results, f"query {query!r} found nothing"


async def test_keyword_search_ignores_stopwords(
    session: AsyncSession, document_factory: object
) -> None:
    """A query made only of stopwords returns nothing rather than everything."""
    repository = KnowledgeRepository(session)
    await repository.upsert(document_factory())  # type: ignore[operator]
    assert await repository.search_keyword("the and of") == []


async def test_keyword_search_handles_punctuation_that_would_break_tsquery(
    session: AsyncSession, document_factory: object
) -> None:
    """A query with tsquery metacharacters is treated as text, not as syntax.

    ``to_tsquery`` would raise on ``auth:`` or ``a & b``; ``plainto_tsquery`` cannot, which
    is exactly why an agent-supplied query uses it.
    """
    repository = KnowledgeRepository(session)
    await repository.upsert(  # type: ignore[operator]
        document_factory(title="Authorization", body="Authorization is enforced at the gateway.")
    )

    for query in ("auth:", "authorization & gateway", "authorization (gateway)", "gateway; drop"):
        results = await repository.search_keyword(query)
        assert isinstance(results, list)


async def test_search_filters_by_scope_and_keeps_organisation_wide(
    session: AsyncSession, document_factory: object
) -> None:
    """A repository-scoped search sees its own documents plus org-wide ones.

    Applying one service's decision to an unrelated service is the failure this prevents,
    while org-wide policy has to reach everywhere.
    """
    repository = KnowledgeRepository(session)
    await repository.upsert(document_factory(scope="acme/widgets"))  # type: ignore[operator]
    await repository.upsert(document_factory(scope="acme/other", title="Other repo memory"))  # type: ignore[operator]
    await repository.upsert(  # type: ignore[operator]
        document_factory(scope=ORGANISATION_SCOPE, title="Org-wide policy")
    )

    scoped = await repository.search_keyword("authorization", scope="acme/widgets")
    titles = {row.title for row in scoped}

    assert "Export metadata authorization" in titles
    assert "Org-wide policy" in titles
    assert "Other repo memory" not in titles


async def test_unscoped_search_sees_every_scope(
    session: AsyncSession, document_factory: object
) -> None:
    """Without a scope, search is global — the operator console's view."""
    repository = KnowledgeRepository(session)
    await repository.upsert(document_factory(scope="acme/widgets"))  # type: ignore[operator]
    await repository.upsert(document_factory(scope="acme/other", title="Another"))  # type: ignore[operator]

    assert len(await repository.search_keyword("authorization")) == 2


async def test_search_excludes_superseded_documents(
    session: AsyncSession, document_factory: object
) -> None:
    """A superseded decision stops influencing verification immediately."""
    repository = KnowledgeRepository(session)
    original = document_factory(document_id="kdoc_old")  # type: ignore[operator]
    replacement = document_factory(document_id="kdoc_new", title="Replacement policy")  # type: ignore[operator]
    await repository.upsert(original)
    await repository.upsert(replacement)
    await repository.supersede(original.id, by_document_id=replacement.id)

    results = await repository.search_keyword("authorization")
    assert {row.id for row in results} == {"kdoc_new"}


async def test_search_excludes_expired_documents(
    session: AsyncSession, document_factory: object
) -> None:
    """An expired temporary exception stops applying without anyone deleting it."""
    repository = KnowledgeRepository(session)
    await repository.upsert(  # type: ignore[operator]
        document_factory(expires_at=utcnow() - timedelta(days=1))
    )
    assert await repository.search_keyword("authorization") == []

    # ...and the filter is time-based, not "expires_at is set".
    await repository.upsert(  # type: ignore[operator]
        document_factory(document_id="kdoc_future", expires_at=utcnow() + timedelta(days=30))
    )
    assert len(await repository.search_keyword("authorization")) == 1


async def test_list_active_for_scope_orders_by_authority(
    session: AsyncSession, document_factory: object
) -> None:
    """Higher-authority memory comes first, so retrieval ranks policy above inference.

    Spec section 29 requires human decisions to outrank inferred knowledge; sorting by
    ``authority_rank`` is where that becomes behaviour rather than intent.
    """
    repository = KnowledgeRepository(session)
    await repository.upsert(  # type: ignore[operator]
        document_factory(
            document_id="kdoc_inferred",
            title="Inferred",
            authority=KnowledgeAuthority.INFERRED,
            source_type=KnowledgeSourceType.REPOSITORY_DOCUMENTATION,
        )
    )
    await repository.upsert(  # type: ignore[operator]
        document_factory(
            document_id="kdoc_policy",
            title="Policy",
            authority=KnowledgeAuthority.POLICY,
            source_type=KnowledgeSourceType.ENGINEERING_POLICY,
        )
    )

    rows = await repository.list_active_for_scope("acme/widgets")
    assert [row.id for row in rows] == ["kdoc_policy", "kdoc_inferred"]


async def test_upsert_updates_an_existing_document(
    session: AsyncSession, document_factory: object
) -> None:
    """Re-deriving the same knowledge updates it rather than raising a duplicate key.

    The search vector is regenerated by PostgreSQL, so the edited body is immediately
    searchable without a reindex step anyone could forget.
    """
    repository = KnowledgeRepository(session)
    document = document_factory()  # type: ignore[operator]
    await repository.upsert(document)

    updated = await repository.upsert(  # type: ignore[operator]
        document.evolve(body="Now it mentions the circuit breaker as well.")
    )

    assert updated.id == document.id
    assert await repository.search_keyword("circuit breaker") != []
    assert await repository.search_keyword("gateway") == []


async def test_authority_rank_is_written_for_ranking(
    session: AsyncSession, document_factory: object
) -> None:
    """The stored rank matches the domain's, so SQL and Python agree on ordering."""
    from scout_domain.models.knowledge import AUTHORITY_RANK

    repository = KnowledgeRepository(session)
    row = await repository.upsert(  # type: ignore[operator]
        document_factory(authority=KnowledgeAuthority.POLICY)
    )
    assert row.authority_rank == AUTHORITY_RANK[KnowledgeAuthority.POLICY]


async def test_search_scored_reports_relevance(
    session: AsyncSession, document_factory: object
) -> None:
    """Scored search exposes why one document outranked another.

    Without the score, "why did Scout not find the decision we recorded?" is unanswerable.
    """
    repository = KnowledgeRepository(session)
    await repository.upsert(document_factory())  # type: ignore[operator]

    scored = await repository.search_scored("authorization")

    assert scored
    document, rank = scored[0]
    assert document.id.startswith("kdoc_")
    assert rank > 0


async def test_embedding_round_trips_and_links_back(
    session: AsyncSession, document_factory: object
) -> None:
    """An embedding can be stored and read, and the document points at it.

    Embeddings are optional (spec section 30), so the column is JSON rather than a
    ``pgvector`` type: the vector index is an operational upgrade, not a prerequisite.
    """
    repository = KnowledgeRepository(session)
    document = await repository.upsert(document_factory())  # type: ignore[operator]

    embedding_id = await repository.record_embedding(
        document_id=document.id,
        model="text-embedding-3-small",
        vector=[0.1, -0.2, 0.3],
        content_digest="d" * 64,
    )

    assert embedding_id.startswith("kdoc_")
    refreshed = await repository.get(document.id)
    assert refreshed is not None
    assert refreshed.embedding_id == embedding_id
    assert await repository.get_embedding(document.id) == [0.1, -0.2, 0.3]


async def test_recording_an_embedding_replaces_the_previous_one(
    session: AsyncSession, document_factory: object
) -> None:
    """One document has at most one vector, so similarity search is unambiguous."""
    repository = KnowledgeRepository(session)
    document = await repository.upsert(document_factory())  # type: ignore[operator]

    await repository.record_embedding(document_id=document.id, model="m1", vector=[1.0])
    await repository.record_embedding(document_id=document.id, model="m2", vector=[2.0, 3.0])

    assert await repository.get_embedding(document.id) == [2.0, 3.0]


async def test_count_active_for_scope_matches_the_filtered_list(
    session: AsyncSession, document_factory: object
) -> None:
    """The count uses the same filters as the list, so a dashboard cannot disagree."""
    repository = KnowledgeRepository(session)
    await repository.upsert(document_factory(scope="acme/widgets"))  # type: ignore[operator]
    await repository.upsert(document_factory(scope="acme/other", title="Other"))  # type: ignore[operator]
    await repository.upsert(  # type: ignore[operator]
        document_factory(
            document_id="kdoc_expired",
            expires_at=utcnow() - timedelta(days=1),
        )
    )

    assert await repository.count_active_for_scope("acme/widgets") == 1
    assert len(await repository.list_active_for_scope("acme/widgets")) == 1


async def test_decision_round_trips_and_is_searchable(
    session: AsyncSession,
) -> None:
    """An engineering decision is stored with its provenance and can be found."""
    repository = KnowledgeRepository(session)
    decision = EngineeringDecision(
        id="dec_01J8ZQ4K2M7X3N9PQRSTVWXY0",
        statement="The reporting service intentionally bypasses user authorization.",
        rationale="Access is enforced at the gateway, so the service trusts its caller.",
        decided_by="dana",
        scope="acme/widgets",
        source_question_id="qst_1",
        source_answer_id="ans_1",
    )
    row = await repository.record_decision(decision)

    assert KnowledgeRepository.decision_to_domain(row) == decision
    found = await repository.search_decisions("authorization gateway")
    assert [item.id for item in found] == [decision.id]


async def test_superseded_decisions_are_excluded_from_active_reads(
    session: AsyncSession,
) -> None:
    """A reversal stops influencing verification as soon as it is recorded."""
    from scout_persistence.repositories.decision import EngineeringDecisionRepository

    decisions = EngineeringDecisionRepository(session)
    original = EngineeringDecision(
        id="dec_old",
        statement="Bypass authorization in the reporting service.",
        decided_by="dana",
        scope="acme/widgets",
    )
    replacement = EngineeringDecision(
        id="dec_new",
        statement="The reporting service must enforce authorization itself.",
        decided_by="dana",
        scope="acme/widgets",
    )
    await decisions.create(original)
    await decisions.create(replacement)
    await decisions.supersede(original.id, superseded_by_id=replacement.id)

    active = await decisions.list_active_for_scope("acme/widgets")
    assert [row.id for row in active] == ["dec_new"]

    refreshed = await decisions.get(replacement.id)
    assert refreshed is not None
    assert refreshed.supersedes_id == "dec_old", "the chain must be walkable backwards"


async def test_generated_search_vector_is_maintained_by_postgres(
    session: AsyncSession, document_factory: object
) -> None:
    """The vector is non-empty immediately after insert, with no application-side step."""
    from sqlalchemy import text

    repository = KnowledgeRepository(session)
    document = await repository.upsert(  # type: ignore[operator]
        document_factory(body="A distinctive phrase: quicksilver harmonica.")
    )
    await session.flush()

    matches = await session.scalar(
        text(
            "SELECT count(*) FROM knowledge_documents "
            "WHERE id = :id AND search_vector @@ plainto_tsquery('english', :query)"
        ),
        {"id": document.id, "query": "quicksilver harmonica"},
    )
    assert matches == 1


def test_organisation_scope_constant_matches_the_domain_convention() -> None:
    """``*`` is the organisation-wide scope, spelled once."""
    assert ORGANISATION_SCOPE == "*"
    document = KnowledgeDocument(
        id="kdoc_1",
        title="T",
        body="B",
        source_type=KnowledgeSourceType.ENGINEERING_POLICY,
        authority=KnowledgeAuthority.POLICY,
        scope=ORGANISATION_SCOPE,
    )
    assert document.applies_to("acme/anything")
