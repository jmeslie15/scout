"""Recorded engineering decisions as retrievable, authoritative memory (spec section 29).

The spec's worked example is a reporting service that intentionally bypasses normal user
authorization because access is enforced at the gateway. The requirement that follows is
the reason this module exists: *once a decision is recorded, Scout must consider it before
raising the same issue again*.

A decision is an :class:`~scout_domain.models.knowledge.EngineeringDecision`; it only
becomes retrievable when it is promoted to a
:class:`~scout_domain.models.knowledge.KnowledgeDocument` with
``HUMAN_DECISION`` authority. Ids are linked in both directions so an audit can walk from
a retrieval hit back to the human who decided it, and from the decision forward to
everything Scout now believes because of it.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from scout_domain.errors import ConfigurationError
from scout_domain.ids import IdKind, new_id
from scout_domain.models.knowledge import (
    EngineeringDecision,
    KnowledgeAuthority,
    KnowledgeDocument,
    KnowledgeSourceType,
)
from scout_domain.models.question import Question, QuestionAnswer
from scout_knowledge.ranking import normalize
from scout_knowledge.store import KnowledgeStore

__all__ = ["DecisionRegistry"]

#: Longest title a knowledge document may carry (mirrors the domain constraint).
_MAX_TITLE_LENGTH = 300

#: Minimum shared terms before a loose textual signature match is accepted. One shared
#: term is coincidence; two is weak but usable evidence.
_MIN_PARTIAL_OVERLAP_TERMS = 2


class DecisionRegistry:
    """In-process index of recorded decisions, optionally backed by a knowledge store.

    Decisions are held here rather than read back from the database because a decision is
    only meaningful together with the chain that produced it (what it superseded, which
    answer it came from). The knowledge documents it mints are what retrieval sees.
    """

    def __init__(self, *, store: KnowledgeStore | None = None) -> None:
        self._store = store
        self._decisions: dict[str, EngineeringDecision] = {}

    @property
    def decisions(self) -> list[EngineeringDecision]:
        """A snapshot of every recorded decision, active or superseded."""
        return list(self._decisions.values())

    async def record(
        self,
        decision: EngineeringDecision,
        *,
        promote_to_knowledge: bool,
    ) -> KnowledgeDocument | None:
        """Record ``decision``, optionally minting a retrievable knowledge document.

        Returns the minted document when ``promote_to_knowledge`` is set, otherwise
        ``None``. Promoting without a store is a configuration error, not a silent
        downgrade: a decision the operator believed was durable must not quietly stay
        in-process.
        """
        if decision.supersedes_id is not None:
            previous = self._decisions.get(decision.supersedes_id)
            if previous is not None:
                self._decisions[previous.id] = previous.evolve(superseded_by_id=decision.id)

        if not promote_to_knowledge:
            self._decisions[decision.id] = decision
            return None

        if self._store is None:
            raise ConfigurationError(
                "promoting an engineering decision to knowledge requires a KnowledgeStore",
                decision_id=decision.id,
            )

        stored = await self._store.upsert(self._document_for(decision))
        document_ids = list(dict.fromkeys([*decision.knowledge_document_ids, stored.id]))
        self._decisions[decision.id] = decision.evolve(knowledge_document_ids=document_ids)
        return stored

    async def record_from_answer(
        self,
        *,
        answer: QuestionAnswer,
        title: str,
        scope: str,
    ) -> KnowledgeDocument | None:
        """Record a human answer as a decision, returning its retrievable document.

        This is the single-call entry point used by the answer endpoint. It is usable with
        **no store** on purpose: the API owns the transaction and writes the returned
        document through its own knowledge repository, so the registry only has to mint a
        validated document and remember the decision in-process. When a store *is*
        configured the document is upserted here too, so a long-lived registry (the worker)
        can record and retrieve without the caller persisting anything.

        ``title`` is the question title supplied by the caller — questions are written to be
        specific and answerable (spec section 28), which makes them a better retrieval key
        than the answer's opening words.

        Returns ``None`` when the answer was not marked for promotion, so the caller treats
        promotion as a no-op rather than an error.
        """
        if not answer.promote_to_knowledge:
            return None

        resolved_scope = scope.strip() or "*"
        decision = EngineeringDecision(
            id=new_id(IdKind.DECISION),
            statement=answer.body,
            rationale=f"Recorded from answer {answer.id} to question {answer.question_id}.",
            decided_by=answer.answered_by,
            decided_at=answer.answered_at,
            scope=resolved_scope,
            source_question_id=answer.question_id,
            source_answer_id=answer.id,
        )
        document = self._document_for(decision, title=title)

        if self._store is None:
            self._decisions[decision.id] = decision.evolve(knowledge_document_ids=[document.id])
            return document

        stored = await self._store.upsert(document)
        self._decisions[decision.id] = decision.evolve(knowledge_document_ids=[stored.id])
        return stored

    async def supersede(
        self,
        old_id: str,
        new_decision: EngineeringDecision,
        *,
        promote_to_knowledge: bool = False,
    ) -> EngineeringDecision:
        """Record ``new_decision`` as the replacement for ``old_id``.

        A reversal is recorded rather than overwritten (spec section 29), so the old
        decision stays in the registry with ``superseded_by_id`` set. When
        ``promote_to_knowledge`` is set, the replacement's knowledge document supersedes
        the old decision's documents too — otherwise retrieval would keep serving the
        reversed decision at ``HUMAN_DECISION`` authority.
        """
        previous = self._decisions.get(old_id)
        if previous is None:
            raise KeyError(f"unknown engineering decision: {old_id!r}")

        replacement = new_decision.evolve(supersedes_id=old_id)
        document = await self.record(replacement, promote_to_knowledge=promote_to_knowledge)
        stored = self._decisions[replacement.id]

        if document is not None and self._store is not None:
            for old_document_id in previous.knowledge_document_ids:
                existing = await self._store.get(old_document_id)
                if existing is not None:
                    await self._store.supersede(old_document_id, document.id)
        return stored

    def find_applicable(
        self,
        repository_full_name: str,
        *,
        tags: Iterable[str] | None = None,
        as_of: datetime | None = None,
    ) -> list[EngineeringDecision]:
        """Return active decisions in scope, newest first.

        ``tags`` filters to decisions sharing at least one tag. ``as_of`` answers the
        historical question "what did we believe at time T": a decision reversed *after*
        ``as_of`` was still applicable then, so it is returned.
        """
        requested = {tag.strip().lower() for tag in tags} if tags is not None else None
        matches: list[EngineeringDecision] = []
        for decision in self._decisions.values():
            if not decision.applies_to(repository_full_name):
                continue
            if as_of is not None and decision.decided_at > as_of:
                continue
            if not self._is_active_at(decision, as_of=as_of):
                continue
            if requested is not None:
                decision_tags = {tag.lower() for tag in decision.tags}
                if not requested & decision_tags:
                    continue
            matches.append(decision)
        matches.sort(key=lambda item: (item.decided_at, item.id), reverse=True)
        return matches

    def acknowledge_before_flagging(
        self,
        issue_signature: str,
        repository_full_name: str,
        decisions: Iterable[EngineeringDecision],
    ) -> EngineeringDecision | None:
        """Return a recorded decision that already covers ``issue_signature``, if any.

        Spec section 29 requires Scout to consider a recorded decision **before** raising
        the same issue again: an issue that a human has already decided is not a finding,
        it is a re-litigation. The security and code-review agents **must** call this hook
        before emitting a finding, passing the decisions returned by
        :meth:`find_applicable` (or any broader set of candidates) and a stable
        ``issue_signature`` such as ``security.missing_authorization`` or
        ``correctness.race_condition``. A non-``None`` return means the agent must not
        raise that issue; it should reference the decision instead.

        Matching is intentionally conservative but signature-based rather than
        full-text-similar: an exact tag match wins, a tag/prefix match is next, and a
        decision whose text contains every signature term also counts. When several
        decisions match, the strongest match wins, with recency breaking ties.
        """
        signature = normalize(issue_signature)
        if not signature:
            return None

        best: tuple[int, datetime, EngineeringDecision] | None = None
        for decision in decisions:
            if not decision.is_active or not decision.applies_to(repository_full_name):
                continue
            strength = self._signature_strength(signature, decision)
            if strength == 0:
                continue
            if best is None or (strength, decision.decided_at) > (best[0], best[1]):
                best = (strength, decision.decided_at, decision)
        return None if best is None else best[2]

    @staticmethod
    def from_question_answer(
        answer: QuestionAnswer,
        question: Question,
        *,
        knowledge_scope: str,
    ) -> EngineeringDecision:
        """Build the decision a human answer represents.

        A human answer *is* a decision regardless of whether it is promoted, so this
        constructs the object unconditionally; promotion to retrievable memory is the
        separate :meth:`record` step, driven by ``answer.promote_to_knowledge``.
        """
        rationale_parts = [f"Answer to question {question.id} ({question.title})."]
        if question.investigation_summary:
            rationale_parts.append(f"Investigation: {question.investigation_summary}")
        if question.context:
            rationale_parts.append(f"Context: {question.context}")

        return EngineeringDecision(
            id=new_id(IdKind.DECISION),
            statement=answer.body,
            rationale=" ".join(rationale_parts),
            decided_by=answer.answered_by,
            decided_at=answer.answered_at,
            scope=knowledge_scope,
            source_question_id=question.id,
            source_job_id=question.job_id,
            source_answer_id=answer.id,
        )

    # -- internals ------------------------------------------------------------------

    def _is_active_at(
        self,
        decision: EngineeringDecision,
        *,
        as_of: datetime | None,
    ) -> bool:
        """Whether ``decision`` was in force at ``as_of`` (or is in force now)."""
        if decision.superseded_by_id is None:
            return True
        if as_of is None:
            return False
        replacement = self._decisions.get(decision.superseded_by_id)
        if replacement is None:
            return False
        return replacement.decided_at > as_of

    @staticmethod
    def _signature_strength(signature: str, decision: EngineeringDecision) -> int:
        """Score how well ``signature`` matches ``decision``; ``0`` means no match."""
        tags = {normalize(tag) for tag in decision.tags}
        tags.discard("")
        if signature in tags:
            return 3
        if any(signature in tag or tag in signature for tag in tags):
            return 2

        signature_terms = set(signature.split())
        text_terms = set(normalize(f"{decision.statement} {decision.rationale}").split())
        if signature_terms and signature_terms <= text_terms:
            return 2
        overlap = signature_terms & text_terms
        if len(overlap) >= _MIN_PARTIAL_OVERLAP_TERMS and len(overlap) * 2 >= len(signature_terms):
            return 1
        return 0

    @staticmethod
    def _document_for(
        decision: EngineeringDecision,
        *,
        title: str | None = None,
    ) -> KnowledgeDocument:
        """Mint the retrievable document that carries ``decision``.

        ``title`` overrides the default of "first line of the statement"; the answer
        endpoint passes the *question* title, which is written to be specific and
        answerable (spec section 28) and is therefore a better retrieval key than the
        answer's opening words.
        """
        resolved_title = title.strip() if title is not None else ""
        if not resolved_title:
            resolved_title = (
                _first_line(decision.statement)[:_MAX_TITLE_LENGTH]
                or decision.statement[:_MAX_TITLE_LENGTH]
            )
        body = decision.statement
        if decision.rationale:
            body = f"{decision.statement}\n\nRationale: {decision.rationale}"
        return KnowledgeDocument(
            id=new_id(IdKind.KNOWLEDGE_DOCUMENT),
            title=resolved_title[:_MAX_TITLE_LENGTH],
            body=body,
            source_type=KnowledgeSourceType.HUMAN_ANSWER,
            authority=KnowledgeAuthority.HUMAN_DECISION,
            scope=decision.scope,
            tags=list(decision.tags),
            source_reference=decision.id,
            created_at=decision.decided_at,
            updated_at=decision.decided_at,
        )


def _first_line(text: str) -> str:
    """Return the first non-empty line of ``text``, stripped."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""
