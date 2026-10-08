"""Ranking invariants (spec sections 29 and 30).

These tests are the executable form of "human decisions have higher authority than
inferred AI knowledge". If someone rebalances the weights in
:mod:`scout_knowledge.ranking`, this file is what stops them from quietly demoting a
recorded human decision below a plausible-sounding AI guess.
"""

from __future__ import annotations

from datetime import timedelta
from itertools import pairwise

import pytest

from scout_domain.models.knowledge import (
    AUTHORITY_RANK,
    KnowledgeAuthority,
    KnowledgeSourceType,
)
from scout_knowledge.models import RetrievalQuery
from scout_knowledge.ranking import (
    AUTHORITY_STEP,
    RECENCY_HALF_LIFE_DAYS,
    SECONDARY_WEIGHT_TOTAL,
    lexical_relevance,
    rank_documents,
    recency_score,
    tokenize,
)

#: Query used by the authority tests. Two uncommon terms, so the arithmetic is easy to
#: reason about and accidental stopword effects cannot hide a bug.
AUTHORITY_QUERY = "quantum entanglement"

_ORDERED_AUTHORITIES = sorted(AUTHORITY_RANK, key=lambda authority: AUTHORITY_RANK[authority])
_ADJACENT_PAIRS = list(pairwise(_ORDERED_AUTHORITIES))


def test_secondary_weights_cannot_bridge_an_authority_step() -> None:
    """Authority dominance is a property of the weights, not of the test data."""
    assert SECONDARY_WEIGHT_TOTAL < AUTHORITY_STEP


@pytest.mark.parametrize(("lower", "higher"), _ADJACENT_PAIRS)
def test_authority_dominance_holds_for_every_adjacent_pair(
    make_document, now, lower, higher
) -> None:
    """A max-relevance lower-authority document still loses to a min-relevance higher one."""
    high_authority = make_document(
        id="kdoc_high",
        title="Unrelated heading",
        body="Only quantum appears here.",
        authority=higher,
        source_type=KnowledgeSourceType.ADR,
        scope="*",
        updated_at=now - timedelta(days=3650),
    )
    low_authority = make_document(
        id="kdoc_low",
        title="Quantum entanglement explained",
        body="Quantum entanglement is the subject of this document.",
        authority=lower,
        source_type=KnowledgeSourceType.ADR,
        scope="acme/api",
        updated_at=now,
    )
    query = RetrievalQuery(text=AUTHORITY_QUERY, repository_full_name="acme/api")

    result = rank_documents(query, [low_authority, high_authority], now=now)

    assert [item.document.id for item in result.items] == ["kdoc_high", "kdoc_low"]
    assert result.items[0].score > result.items[1].score


def test_policy_outranks_a_max_relevance_brand_new_inferred_document(make_document, now) -> None:
    """The spec's headline invariant, stated as its own test."""
    policy = make_document(
        id="kdoc_policy",
        title="Migration policy",
        body="This policy mentions quantum once.",
        authority=KnowledgeAuthority.POLICY,
        source_type=KnowledgeSourceType.ENGINEERING_POLICY,
        scope="*",
        updated_at=now - timedelta(days=2000),
    )
    inferred = make_document(
        id="kdoc_inferred",
        title="Quantum entanglement",
        body="Quantum entanglement quantum entanglement, freshly inferred.",
        authority=KnowledgeAuthority.INFERRED,
        source_type=KnowledgeSourceType.REPOSITORY_DOCUMENTATION,
        scope="acme/api",
        updated_at=now,
    )
    query = RetrievalQuery(text=AUTHORITY_QUERY, repository_full_name="acme/api")

    result = rank_documents(query, [inferred, policy], now=now)

    assert result.items[0].document.id == "kdoc_policy"
    assert result.items[0].document.authority is KnowledgeAuthority.POLICY


def test_scope_filtering_excludes_other_repositories(make_document, now) -> None:
    """A document scoped to another repository is excluded, not merely discounted."""
    exact = make_document(id="kdoc_exact", scope="acme/api", body="deployment pipeline")
    organisation = make_document(id="kdoc_org", scope="*", body="deployment pipeline")
    other = make_document(id="kdoc_other", scope="other/service", body="deployment pipeline")
    query = RetrievalQuery(text="deployment pipeline", repository_full_name="acme/api")

    result = rank_documents(query, [other, organisation, exact], now=now)

    returned = [item.document.id for item in result.items]
    assert returned == ["kdoc_exact", "kdoc_org"]
    assert "kdoc_other" not in returned
    assert result.total_candidates == 2


def test_recency_decay_is_monotonic(make_document, now) -> None:
    """Newer documents score strictly higher when everything else is equal."""
    fresh = make_document(id="kdoc_fresh", body="cache invalidation", updated_at=now)
    half_life = make_document(
        id="kdoc_half",
        body="cache invalidation",
        updated_at=now - timedelta(days=int(RECENCY_HALF_LIFE_DAYS)),
    )
    old = make_document(
        id="kdoc_old",
        body="cache invalidation",
        updated_at=now - timedelta(days=int(RECENCY_HALF_LIFE_DAYS) * 2),
    )
    query = RetrievalQuery(text="cache invalidation", repository_full_name="acme/api")

    result = rank_documents(query, [old, half_life, fresh], now=now)

    assert [item.document.id for item in result.items] == ["kdoc_fresh", "kdoc_half", "kdoc_old"]
    fresh_score, _ = recency_score(fresh, now=now)
    half_score, _ = recency_score(half_life, now=now)
    old_score, _ = recency_score(old, now=now)
    assert fresh_score == pytest.approx(1.0)
    assert half_score == pytest.approx(0.5)
    assert old_score == pytest.approx(0.25)
    assert fresh_score > half_score > old_score


def test_future_timestamp_does_not_score_above_one(make_document, now) -> None:
    """Clock skew between writer and reader must not inflate recency."""
    skewed = make_document(id="kdoc_skew", updated_at=now + timedelta(days=30))
    score, _ = recency_score(skewed, now=now)
    assert score == pytest.approx(1.0)


def test_exact_phrase_beats_the_same_terms_scattered(make_document, now) -> None:
    """Phrase agreement is the tiebreaker when term frequencies are identical."""
    ordered = make_document(id="kdoc_phrase", title="Rules", body="alpha beta gamma implementation")
    scrambled = make_document(
        id="kdoc_scattered", title="Rules", body="gamma alpha beta implementation"
    )
    frequencies = {"alpha": 2, "beta": 2, "gamma": 2, "implementation": 2}

    phrase_match = lexical_relevance(
        "alpha beta gamma",
        ordered,
        frequencies=frequencies,
        corpus_size=2,
    )
    scattered_match = lexical_relevance(
        "alpha beta gamma",
        scrambled,
        frequencies=frequencies,
        corpus_size=2,
    )

    assert set(phrase_match.matched_terms) == {"alpha", "beta", "gamma"}
    assert phrase_match.score > scattered_match.score
    assert any("exact phrase" in reason for reason in phrase_match.reasons)
    assert not any("exact phrase" in reason for reason in scattered_match.reasons)


def test_rank_reasons_explain_the_score(make_document, now) -> None:
    """The dashboard can always answer "why was this retrieved?"."""
    document = make_document(
        id="kdoc_explain",
        title="Gateway authorization",
        body="Authorization is enforced at the gateway for the reporting service.",
        authority=KnowledgeAuthority.POLICY,
        source_type=KnowledgeSourceType.ENGINEERING_POLICY,
    )
    query = RetrievalQuery(text="gateway authorization", repository_full_name="acme/api")

    result = rank_documents(query, [document], now=now)

    reasons = result.items[0].rank_reasons
    assert reasons[0].startswith("authority POLICY")
    assert any("lexical coverage" in reason for reason in reasons)
    assert any("recency" in reason for reason in reasons)
    assert any("scope matches the repository exactly" in reason for reason in reasons)
    assert set(result.items[0].matched_terms) == {"gateway", "authorization"}


def test_superseded_and_expired_documents_are_excluded(make_document, now) -> None:
    live = make_document(id="kdoc_live", body="feature flag rollout")
    superseded = make_document(
        id="kdoc_superseded", body="feature flag rollout", superseded_by_id="kdoc_live"
    )
    expired = make_document(
        id="kdoc_expired",
        body="feature flag rollout",
        expires_at=now - timedelta(days=1),
    )
    query = RetrievalQuery(text="feature flag rollout", repository_full_name="acme/api")

    result = rank_documents(query, [live, superseded, expired], now=now)

    assert [item.document.id for item in result.items] == ["kdoc_live"]


def test_include_expired_returns_history(make_document, now) -> None:
    expired = make_document(
        id="kdoc_expired",
        body="temporary exception",
        expires_at=now - timedelta(days=1),
    )
    query = RetrievalQuery(
        text="temporary exception",
        repository_full_name="acme/api",
        include_expired=True,
    )

    result = rank_documents(query, [expired], now=now)

    assert [item.document.id for item in result.items] == ["kdoc_expired"]


def test_expiry_boundary_is_exclusive(make_document, now) -> None:
    """A document expiring exactly now is expired; ``expires_at`` is an exclusive bound."""
    boundary = make_document(id="kdoc_boundary", body="boundary case", expires_at=now)
    query = RetrievalQuery(text="boundary case", repository_full_name="acme/api")

    assert rank_documents(query, [boundary], now=now).items == []


def test_min_authority_and_document_types_filter(make_document, now) -> None:
    policy = make_document(
        id="kdoc_policy",
        body="retention window",
        authority=KnowledgeAuthority.POLICY,
        source_type=KnowledgeSourceType.ENGINEERING_POLICY,
    )
    inferred = make_document(
        id="kdoc_inferred",
        body="retention window",
        authority=KnowledgeAuthority.INFERRED,
    )
    query = RetrievalQuery(
        text="retention window",
        repository_full_name="acme/api",
        min_authority=KnowledgeAuthority.APPROVED_DOCUMENT,
        document_types=frozenset({KnowledgeSourceType.ENGINEERING_POLICY}),
    )

    result = rank_documents(query, [policy, inferred], now=now)

    assert [item.document.id for item in result.items] == ["kdoc_policy"]


def test_limit_caps_items_but_not_total_candidates(make_document, now) -> None:
    documents = [make_document(id=f"kdoc_{index}", body="search term here") for index in range(3)]
    query = RetrievalQuery(text="search term", repository_full_name="acme/api", limit=2)

    result = rank_documents(query, documents, now=now)

    assert len(result.items) == 2
    assert result.total_candidates == 3


def test_identical_scores_break_ties_by_id(make_document, now) -> None:
    """Deterministic ordering is what lets the two stores agree."""
    first = make_document(id="kdoc_a", body="same content")
    second = make_document(id="kdoc_b", body="same content")
    query = RetrievalQuery(text="same content", repository_full_name="acme/api")

    forward = rank_documents(query, [first, second], now=now)
    backward = rank_documents(query, [second, first], now=now)

    assert [item.document.id for item in forward.items] == ["kdoc_a", "kdoc_b"]
    assert [item.document.id for item in forward.items] == [
        item.document.id for item in backward.items
    ]


def test_unrelated_documents_are_not_retrieved_by_authority_alone(make_document, now) -> None:
    """Authority ranks candidates; it does not make an unrelated policy relevant."""
    unrelated_policy = make_document(
        id="kdoc_policy",
        title="Password rotation",
        body="Rotate credentials every ninety days.",
        authority=KnowledgeAuthority.POLICY,
        source_type=KnowledgeSourceType.ENGINEERING_POLICY,
    )
    query = RetrievalQuery(text="queue partitioning", repository_full_name="acme/api")

    result = rank_documents(query, [unrelated_policy], now=now)

    assert result.items == []
    assert result.total_candidates == 0


def test_empty_query_returns_no_candidates(make_document, now) -> None:
    document = make_document(id="kdoc_any", body="anything at all")
    query = RetrievalQuery(text="   ", repository_full_name="acme/api")

    result = rank_documents(query, [document], now=now)

    assert result.items == []
    assert result.strategy == "authority_weighted_keyword"


def test_tokenize_drops_stopwords_and_short_tokens() -> None:
    assert tokenize("The rate of a limit is") == ["rate", "limit"]


def test_retrieval_query_validates_and_clamps() -> None:
    with pytest.raises(ValueError, match="limit must be at least 1"):
        RetrievalQuery(text="x", repository_full_name="acme/api", limit=0)
    with pytest.raises(ValueError, match="repository_full_name is required"):
        RetrievalQuery(text="x", repository_full_name="  ")

    clamped = RetrievalQuery(text="  padded  ", repository_full_name="acme/api", limit=10_000)
    assert clamped.text == "padded"
    assert clamped.limit == 100
