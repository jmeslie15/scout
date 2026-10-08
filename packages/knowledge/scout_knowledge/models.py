"""Internal retrieval value objects.

These types never cross a process boundary and are never persisted, so they are plain
frozen dataclasses rather than validating Pydantic models: retrieval runs on every agent
turn, and paying Pydantic validation cost per candidate would be pure overhead. The
:class:`~scout_domain.models.knowledge.KnowledgeDocument` they wrap *is* a boundary type
and remains a validated ``ScoutModel``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from scout_domain.models.knowledge import (
    KnowledgeAuthority,
    KnowledgeDocument,
    KnowledgeSourceType,
)

__all__ = [
    "DEFAULT_RETRIEVAL_LIMIT",
    "MAX_RETRIEVAL_LIMIT",
    "RankedDocument",
    "RetrievalQuery",
    "RetrievalResult",
]

#: Default number of documents returned to an agent. Deliberately small: retrieval feeds
#: a token budget, and a long tail of weak matches is worse than a short, sharp list.
DEFAULT_RETRIEVAL_LIMIT = 10

#: Hard ceiling on ``limit``. A caller cannot ask for an unbounded result set, which
#: would turn a retrieval call into a table scan with a context dump attached.
MAX_RETRIEVAL_LIMIT = 100


@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    """What the caller is looking for.

    ``repository_full_name`` is required rather than optional: scope filtering is the
    mechanism that stops a decision about one service from being applied to an unrelated
    one (spec section 29), so a query cannot opt out of it by omission.
    """

    text: str
    repository_full_name: str
    document_types: frozenset[KnowledgeSourceType] | None = None
    limit: int = DEFAULT_RETRIEVAL_LIMIT
    min_authority: KnowledgeAuthority | None = None
    #: Expired documents are excluded unless a caller explicitly asks for history.
    include_expired: bool = False

    def __post_init__(self) -> None:
        if not self.repository_full_name.strip():
            raise ValueError("repository_full_name is required for scope filtering")
        if self.limit < 1:
            raise ValueError(f"limit must be at least 1, got {self.limit}")
        if self.limit > MAX_RETRIEVAL_LIMIT:
            object.__setattr__(self, "limit", MAX_RETRIEVAL_LIMIT)
        stripped = self.text.strip()
        if stripped != self.text:
            object.__setattr__(self, "text", stripped)


@dataclass(frozen=True, slots=True)
class RankedDocument:
    """One candidate plus the reasons it ranked where it did.

    ``rank_reasons`` is not decoration: the dashboard shows *why* a document was
    retrieved, which is the difference between engineering memory a human trusts and a
    black box that silently changes Scout's behaviour.
    """

    document: KnowledgeDocument
    score: float
    matched_terms: list[str] = field(default_factory=list)
    rank_reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    """The outcome of one retrieval.

    ``total_candidates`` counts everything that passed eligibility, before ``limit`` was
    applied, so a caller can tell "nothing exists" apart from "the ranking cut it off".
    """

    items: list[RankedDocument] = field(default_factory=list)
    total_candidates: int = 0
    #: Which retrieval strategy produced this result; recorded for observability.
    strategy: str = "authority_weighted_keyword"
