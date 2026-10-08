"""Authority-weighted ranking for engineering-memory retrieval (spec sections 29, 30).

Scout's premise is that a human decision beats an inferred AI conclusion. This module
makes that premise executable: :data:`AUTHORITY_STEP` is one full point per authority
rank, and every other signal — lexical relevance, recency, repository scope — is
normalised into ``[0, 1]`` and weighted so that their *combined maximum* is still less
than a single authority step::

    LEXICAL_WEIGHT + RECENCY_WEIGHT + SCOPE_WEIGHT == 0.95 < 1.0 == AUTHORITY_STEP

The consequence is the invariant spec section 29 demands: a max-relevance, brand-new
``INFERRED`` document loses to a min-relevance, ancient ``POLICY`` document, and the
ordering holds for *every* adjacent pair of authorities, not just the endpoints.
Within one authority tier lexical relevance decides, then recency, then scope.

Ranking is done in Python, once, for both the in-memory and PostgreSQL stores. PostgreSQL
full-text search is used to select candidates (and to score them for the round trip), but
the final ordering is computed here so the two stores cannot diverge as the SQL evolves.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Final

from scout_domain.models.base import utcnow
from scout_domain.models.knowledge import AUTHORITY_RANK, KnowledgeDocument
from scout_knowledge.models import RankedDocument, RetrievalQuery, RetrievalResult

__all__ = [
    "AUTHORITY_STEP",
    "AUTHORITY_WEIGHT",
    "LEXICAL_WEIGHT",
    "PHRASE_WEIGHT",
    "RECENCY_HALF_LIFE_DAYS",
    "RECENCY_WEIGHT",
    "SCOPE_WEIGHT",
    "STRATEGY_NAME",
    "LexicalMatch",
    "document_frequencies",
    "is_eligible",
    "lexical_relevance",
    "rank_documents",
    "recency_score",
    "scope_score",
    "score_document",
    "tokenize",
]

# ---------------------------------------------------------------------------------
# Weights. The ordering constraint is the point; see the module docstring.
# ---------------------------------------------------------------------------------

#: Points awarded per authority rank (see ``AUTHORITY_RANK``). One full point, so that no
#: combination of the secondary signals below can bridge an authority step.
AUTHORITY_STEP: Final[float] = 1.0

#: Lexical relevance is the dominant *secondary* signal: within an authority tier the
#: best textual match wins.
LEXICAL_WEIGHT: Final[float] = 0.60

#: Recency is a tunable tiebreaker. It must never dominate authority or lexical
#: relevance, so it is weighted below both.
RECENCY_WEIGHT: Final[float] = 0.25

#: Scope is the weakest signal, because scope is mostly enforced as a *filter*: a
#: document scoped to a different repository is excluded outright, and this weight only
#: separates an exact-repository document from an organisation-wide one.
SCOPE_WEIGHT: Final[float] = 0.10

#: Sum of all non-authority weights. Must remain strictly below ``AUTHORITY_STEP`` for
#: the authority-dominance invariant to hold; the ranking tests fail if someone raises
#: one of them past that bound.
SECONDARY_WEIGHT_TOTAL: Final[float] = LEXICAL_WEIGHT + RECENCY_WEIGHT + SCOPE_WEIGHT

#: Displayed name of this strategy in :class:`~scout_knowledge.models.RetrievalResult`.
STRATEGY_NAME: Final[str] = "authority_weighted_keyword"

#: Backwards-compatible alias for the weight of the authority term, for dashboards and
#: documentation that report "the authority weight".
AUTHORITY_WEIGHT: Final[float] = AUTHORITY_STEP

# ---------------------------------------------------------------------------------
# Recency
# ---------------------------------------------------------------------------------

#: A document's recency contribution halves every 180 days. Six months is roughly one
#: planning cycle: a decision still carries weight after it, but a fresher one about the
#: same topic should edge it out when authority and relevance are equal.
RECENCY_HALF_LIFE_DAYS: Final[float] = 180.0

_SECONDS_PER_DAY: Final[float] = 86_400.0

# ---------------------------------------------------------------------------------
# Lexical relevance
# ---------------------------------------------------------------------------------

_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(r"[a-z0-9_]+")
_QUOTED_PHRASE_PATTERN: Final[re.Pattern[str]] = re.compile(r'"([^"]+)"')

#: Query terms shorter than this are noise (``a``, ``of``) rather than signal.
MIN_TOKEN_LENGTH: Final[int] = 2

#: English stopwords. Keeping the list inline rather than depending on a corpus package
#: keeps retrieval dependency-free (Rule 1).
STOPWORDS: Final[frozenset[str]] = frozenset(
    {
        "a",
        "about",
        "above",
        "after",
        "again",
        "against",
        "all",
        "also",
        "am",
        "an",
        "and",
        "any",
        "are",
        "as",
        "at",
        "be",
        "because",
        "been",
        "before",
        "being",
        "below",
        "between",
        "both",
        "but",
        "by",
        "can",
        "cannot",
        "could",
        "did",
        "do",
        "does",
        "doing",
        "down",
        "during",
        "each",
        "few",
        "for",
        "from",
        "further",
        "had",
        "has",
        "have",
        "having",
        "he",
        "her",
        "here",
        "hers",
        "herself",
        "him",
        "himself",
        "his",
        "how",
        "if",
        "in",
        "into",
        "is",
        "it",
        "its",
        "itself",
        "just",
        "may",
        "me",
        "might",
        "more",
        "most",
        "must",
        "my",
        "myself",
        "no",
        "nor",
        "not",
        "now",
        "of",
        "off",
        "on",
        "once",
        "only",
        "or",
        "other",
        "ought",
        "our",
        "ours",
        "ourselves",
        "out",
        "over",
        "own",
        "same",
        "she",
        "should",
        "so",
        "some",
        "such",
        "than",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "themselves",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "through",
        "to",
        "too",
        "under",
        "until",
        "up",
        "very",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "while",
        "who",
        "whom",
        "why",
        "will",
        "with",
        "would",
        "you",
        "your",
        "yours",
        "yourself",
        "yourselves",
    }
)

#: A term in the title counts this many times in the term frequency. Titles are curated
#: summaries, so a title match is stronger evidence of topicality than a body match.
TITLE_TERM_BOOST: Final[float] = 2.0

#: BM25-style term-frequency saturation. Diminishing returns stop a document that repeats
#: one query term from out-ranking a document that covers the whole query.
TF_SATURATION: Final[float] = 1.5

#: Share of the lexical score reserved for an exact-phrase match. An exact phrase is worth
#: more than the same terms scattered through the document (spec section 30).
PHRASE_WEIGHT: Final[float] = 0.25

#: Strength of an exact phrase match found in the body rather than the title.
BODY_PHRASE_STRENGTH: Final[float] = 0.75

#: Strength of an exact phrase match in the title.
TITLE_PHRASE_STRENGTH: Final[float] = 1.0


def tokenize(text: str) -> list[str]:
    """Lower-case ``text`` into indexable terms, dropping stopwords and short tokens."""
    return [
        token
        for token in _TOKEN_PATTERN.findall(text.lower())
        if len(token) >= MIN_TOKEN_LENGTH and token not in STOPWORDS
    ]


def normalize(text: str) -> str:
    """Return the token sequence of ``text`` as a single space-joined string.

    Phrase matching compares normalised strings so that punctuation, casing, and filler
    words ("bypass *the* rate limit") do not defeat an exact-phrase match.
    """
    return " ".join(tokenize(text))


@dataclass(frozen=True, slots=True)
class LexicalMatch:
    """The lexical component of a score, with the terms that produced it."""

    score: float = 0.0
    matched_terms: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def document_frequencies(documents: Sequence[KnowledgeDocument]) -> dict[str, int]:
    """Count, per term, how many ``documents`` contain it.

    Document frequency is computed over the candidate set returned by the store rather
    than over the whole corpus. That keeps retrieval a single round trip and makes IDF a
    property of "the documents competing for this query", which is what actually decides
    the ordering.
    """
    frequencies: Counter[str] = Counter()
    for document in documents:
        frequencies.update(set(tokenize(f"{document.title} {document.body}")))
    return dict(frequencies)


def inverse_document_frequency(
    term: str,
    *,
    frequencies: Mapping[str, int],
    corpus_size: int,
) -> float:
    """Smoothed inverse document frequency, always at least ``1.0``.

    The ``+1.0`` floor matters: a term present in every candidate must still contribute,
    otherwise a query made entirely of common terms would score zero everywhere.
    """
    seen_in = frequencies.get(term, 0)
    return math.log((1.0 + corpus_size) / (1.0 + seen_in)) + 1.0


def _phrase_strength(query_text: str, document: KnowledgeDocument) -> tuple[float, str | None]:
    """Return the best exact-phrase match strength for ``query_text`` in ``document``.

    Quoted phrases in the query are matched individually; otherwise the whole query is
    treated as one phrase. A title hit outranks a body hit.
    """
    quoted = _QUOTED_PHRASE_PATTERN.findall(query_text)
    phrases = quoted or [query_text]
    title = normalize(document.title)
    body = normalize(document.body)

    best = 0.0
    reason: str | None = None
    for phrase in phrases:
        normalized = normalize(phrase)
        if not normalized:
            continue
        if normalized in title and best < TITLE_PHRASE_STRENGTH:
            best = TITLE_PHRASE_STRENGTH
            reason = f'exact phrase "{normalized}" matched in the title'
        elif normalized in body and best < BODY_PHRASE_STRENGTH:
            best = BODY_PHRASE_STRENGTH
            reason = f'exact phrase "{normalized}" matched in the body'
    return best, reason


def lexical_relevance(
    query_text: str,
    document: KnowledgeDocument,
    *,
    frequencies: Mapping[str, int],
    corpus_size: int,
) -> LexicalMatch:
    """Score term overlap and exact-phrase agreement in ``[0, 1]``.

    This is a self-contained TF-IDF variant rather than a call into a search library, so
    the same arithmetic runs against both stores (Rule 1: no infrastructure that is not
    needed, and no second ranking implementation to keep in sync).
    """
    query_terms = list(dict.fromkeys(tokenize(query_text)))
    if not query_terms:
        return LexicalMatch(0.0, [], ["query contained no indexable terms"])

    term_frequency: dict[str, float] = {}
    for term in tokenize(document.body):
        term_frequency[term] = term_frequency.get(term, 0.0) + 1.0
    for term in tokenize(document.title):
        term_frequency[term] = term_frequency.get(term, 0.0) + TITLE_TERM_BOOST

    matched = [term for term in query_terms if term in term_frequency]
    if not matched:
        return LexicalMatch(0.0, [], ["no query term occurs in the document"])

    total_weight = sum(
        inverse_document_frequency(term, frequencies=frequencies, corpus_size=corpus_size)
        for term in query_terms
    )
    matched_weight = sum(
        inverse_document_frequency(term, frequencies=frequencies, corpus_size=corpus_size)
        * (term_frequency[term] / (term_frequency[term] + TF_SATURATION))
        for term in matched
    )
    coverage = matched_weight / total_weight if total_weight else 0.0

    phrase_strength, phrase_reason = _phrase_strength(query_text, document)
    score = min(1.0, coverage * (1.0 - PHRASE_WEIGHT) + phrase_strength * PHRASE_WEIGHT)

    reasons = [f"lexical coverage {coverage:.2f} ({len(matched)}/{len(query_terms)} query terms)"]
    if phrase_reason is not None:
        reasons.append(phrase_reason)
    return LexicalMatch(score=score, matched_terms=matched, reasons=reasons)


def recency_score(
    document: KnowledgeDocument,
    *,
    now: datetime,
    half_life_days: float = RECENCY_HALF_LIFE_DAYS,
) -> tuple[float, str]:
    """Exponential decay from ``document.updated_at``, halving every ``half_life_days``.

    A document with a future timestamp (clock skew between the writer and the reader) is
    treated as brand new rather than scoring above ``1.0``.
    """
    reference = document.updated_at
    age_days = max(0.0, (now - reference).total_seconds() / _SECONDS_PER_DAY)
    score = 0.5 ** (age_days / half_life_days) if half_life_days > 0 else 0.0
    return score, f"recency {score:.2f} (age {age_days:.1f}d, half-life {half_life_days:.0f}d)"


def scope_score(document: KnowledgeDocument, repository_full_name: str) -> tuple[float, str] | None:
    """Return the scope contribution, or ``None`` when the document does not apply.

    ``None`` means *excluded*, not *low score*: applying one repository's decision to
    another repository is a correctness failure, not a ranking preference.
    """
    if document.scope == repository_full_name:
        return 1.0, "scope matches the repository exactly"
    if document.scope == "*":
        return 0.5, "scope is organisation-wide (*)"
    return None


def is_eligible(
    document: KnowledgeDocument,
    query: RetrievalQuery,
    *,
    now: datetime,
) -> bool:
    """Whether ``document`` may be considered at all for ``query``.

    Both stores funnel through this predicate so scope, expiry, supersession,
    source-type, and minimum-authority handling cannot drift between them. SQL repeats
    the cheap clauses for index efficiency; this function remains authoritative.
    """
    if document.superseded_by_id is not None:
        return False
    if not query.include_expired and document.expires_at is not None and document.expires_at <= now:
        return False
    if query.document_types is not None and document.source_type not in query.document_types:
        return False
    if (
        query.min_authority is not None
        and AUTHORITY_RANK[document.authority] < AUTHORITY_RANK[query.min_authority]
    ):
        return False
    return document.applies_to(query.repository_full_name)


def score_document(
    document: KnowledgeDocument,
    *,
    query: RetrievalQuery,
    now: datetime,
    frequencies: Mapping[str, int],
    corpus_size: int,
) -> RankedDocument | None:
    """Score one eligible document, or return ``None`` if it is out of scope / unmatched."""
    scope = scope_score(document, query.repository_full_name)
    if scope is None:
        return None

    lexical = lexical_relevance(
        query.text,
        document,
        frequencies=frequencies,
        corpus_size=corpus_size,
    )
    # A document that shares no term with the query is not a retrieval candidate, even if
    # it holds the highest authority. Authority ranks *candidates*; it does not make an
    # unrelated policy relevant to this question.
    if not lexical.matched_terms:
        return None

    recency, recency_reason = recency_score(document, now=now)
    authority_rank = AUTHORITY_RANK[document.authority]
    score = (
        authority_rank * AUTHORITY_STEP
        + LEXICAL_WEIGHT * lexical.score
        + RECENCY_WEIGHT * recency
        + SCOPE_WEIGHT * scope[0]
    )

    highest_rank = max(AUTHORITY_RANK.values())
    reasons = [
        f"authority {document.authority.value} (rank {authority_rank}/{highest_rank})",
        *lexical.reasons,
        recency_reason,
        scope[1],
    ]
    return RankedDocument(
        document=document,
        score=score,
        matched_terms=list(lexical.matched_terms),
        rank_reasons=reasons,
    )


def rank_documents(
    query: RetrievalQuery,
    documents: Sequence[KnowledgeDocument],
    *,
    now: datetime | None = None,
) -> RetrievalResult:
    """Filter, score, and order ``documents`` for ``query``.

    The sort is fully deterministic: equal scores fall back to authority, then to the
    document id. A nondeterministic tiebreak would make the two stores disagree on
    identical corpora, which is exactly the divergence this design exists to prevent.
    """
    moment = now or utcnow()
    candidates = [document for document in documents if is_eligible(document, query, now=moment)]
    if not candidates:
        return RetrievalResult(items=[], total_candidates=0, strategy=STRATEGY_NAME)

    frequencies = document_frequencies(candidates)
    ranked: list[RankedDocument] = []
    for document in candidates:
        scored = score_document(
            document,
            query=query,
            now=moment,
            frequencies=frequencies,
            corpus_size=len(candidates),
        )
        if scored is not None:
            ranked.append(scored)

    ranked.sort(
        key=lambda item: (
            -item.score,
            -AUTHORITY_RANK[item.document.authority],
            item.document.id,
        )
    )
    return RetrievalResult(
        items=ranked[: query.limit],
        total_candidates=len(ranked),
        strategy=STRATEGY_NAME,
    )
