"""Questions and answers: first-class entities (spec sections 27 and 28)."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from scout_domain.enums import ActorType, QuestionStatus, Severity
from scout_domain.models.base import ScoutModel, utcnow
from scout_domain.models.evidence import EvidenceReference

__all__ = ["Question", "QuestionAnswer"]


class QuestionAnswer(ScoutModel):
    """A human (or, exceptionally, system) response to a question.

    Answers become engineering memory (spec section 29) and are retrieved with
    ``AUTHORITATIVE`` evidence strength, so they outrank anything Scout inferred.
    """

    id: str = Field(min_length=1)
    question_id: str = Field(min_length=1)

    #: Why Scout could not resolve this deterministically. Required, because an
    #: unexplained escalation is noise (spec section 26 ordering).
    body: str = Field(min_length=1)

    answered_by: str = Field(min_length=1, max_length=200)
    answered_by_type: ActorType = ActorType.HUMAN
    answered_at: datetime = Field(default_factory=utcnow)

    #: Whether this answer should be retained as durable engineering knowledge.
    promote_to_knowledge: bool = False
    #: Scope for promoted knowledge; typically ``owner/name`` or ``*`` for org-wide.
    knowledge_scope: str | None = Field(default=None, max_length=200)


class Question(ScoutModel):
    """Something Scout genuinely does not know and will not invent (spec section 27).

    The investigation order is: deterministic evidence, repository inspection,
    existing tests, documentation, engineering memory, AI reasoning, human question.
    A question is the *last* resort, and each one must record why the earlier steps
    were insufficient (``investigation_summary``).
    """

    id: str = Field(min_length=1)
    job_id: str = Field(min_length=1)

    #: Concise, specific title. Spec section 28 requires questions to be answerable:
    #: "Should managers have access to the export metadata?" not "authorization concerns".
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1)

    #: Why Scout is asking, and what it already tried. Rendered into the notification.
    context: str = Field(default="", max_length=4000)
    investigation_summary: str = Field(default="", max_length=4000)

    evidence: list[EvidenceReference] = Field(default_factory=list)
    severity: Severity = Severity.MEDIUM

    #: Candidate answers offered to the human, when the question is closed-form.
    options: list[str] = Field(default_factory=list)
    #: What Scout will assume if the question goes unanswered. Explicit, so silence is
    #: never mistaken for approval.
    default_assumption: str | None = Field(default=None, max_length=2000)

    status: QuestionStatus = QuestionStatus.OPEN
    created_by: str = Field(min_length=1, max_length=100)
    created_by_type: ActorType = ActorType.AGENT
    created_at: datetime = Field(default_factory=utcnow)

    answers: list[QuestionAnswer] = Field(default_factory=list)
    #: The answer Scout should act on when several exist.
    accepted_answer_id: str | None = None

    @model_validator(mode="after")
    def _validate_answers(self) -> Question:
        answer_ids = {answer.id for answer in self.answers}
        if len(answer_ids) != len(self.answers):
            raise ValueError("duplicate answer ids on question")

        if self.accepted_answer_id is not None and self.accepted_answer_id not in answer_ids:
            raise ValueError(
                f"accepted_answer_id {self.accepted_answer_id!r} does not match any answer"
            )

        if self.status is QuestionStatus.ANSWERED and not self.answers:
            raise ValueError("an ANSWERED question must carry at least one answer")

        if self.status is QuestionStatus.OPEN and self.answers:
            raise ValueError(
                "a question with answers must not remain OPEN; set ANSWERED or SUPERSEDED"
            )
        return self

    @property
    def is_open(self) -> bool:
        return self.status is QuestionStatus.OPEN

    @property
    def accepted_answer(self) -> QuestionAnswer | None:
        """The answer Scout should act on, if any."""
        if self.accepted_answer_id is not None:
            for answer in self.answers:
                if answer.id == self.accepted_answer_id:
                    return answer
        return self.answers[-1] if self.answers else None

    def with_answer(self, answer: QuestionAnswer) -> Question:
        """Return a copy of this question with ``answer`` recorded and status closed."""
        return self.evolve(
            answers=[*self.answers, answer],
            status=QuestionStatus.ANSWERED,
            accepted_answer_id=self.accepted_answer_id or answer.id,
        )
