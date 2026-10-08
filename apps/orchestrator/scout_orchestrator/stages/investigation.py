"""INVESTIGATING: turn genuinely unresolved items into first-class questions.

Spec section 27 makes a question the *last* resort: deterministic evidence, repository
inspection, existing tests, documentation, engineering memory, and AI reasoning all come
first, and a question must record why those were insufficient. So this stage does not go
looking for things to ask about. It is handed the specific facts that remained unresolved --
a blocked acceptance criterion, a check that produced no result, a finding that demands human
judgement -- and it states each one precisely enough that a human can answer it.

Spec section 28 requires a question to be answerable, which is why every question carries:

* a concise title naming the decision, not the topic;
* an ``investigation_summary`` stating what Scout already checked and why that was not enough;
* the evidence behind it, so the reader is not asked to take Scout's word for the premise;
* options, when the answer is closed-form, and a default assumption when it is not.

A question that cannot be answered from the information given is noise, and noise is how an
escalation channel gets muted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from scout_domain.enums import (
    ActorType,
    CriterionStatus,
    EvidenceKind,
    JobStatus,
    QuestionStatus,
    Severity,
)
from scout_domain.ids import IdKind, new_id
from scout_domain.models.evidence import EvidenceReference
from scout_domain.models.question import Question
from scout_domain.models.report import ReportSection, ReportSectionStatus
from scout_orchestrator.stages.context import BaseStage, StageContext, StageResult

if TYPE_CHECKING:
    from collections.abc import Sequence

__all__ = ["InvestigationStage"]

_STAGE: Final = JobStatus.INVESTIGATING

#: Section key prefixes whose ``UNVERIFIED`` state genuinely blocks a conclusion. A section
#: under another prefix is reported as unverified but does not, on its own, justify asking a
#: human: it is context, not a missing verdict.
_MATERIAL_SECTION_PREFIXES: Final[tuple[str, ...]] = ("security.", "testing.")

#: Severity at or above which a finding that demands human judgement becomes a question.
_HUMAN_FINDING_SEVERITY: Final = Severity.HIGH

_MAX_QUESTIONS: Final = 25


class InvestigationStage(BaseStage):
    """Collect unresolved items into answerable questions."""

    stage = _STAGE

    async def run(self, ctx: StageContext) -> StageResult:
        """Build one question per unresolved item, and record what was unresolved."""
        questions: list[Question] = []
        questions.extend(_questions_for_blocked_criteria(ctx))
        questions.extend(_questions_for_unverified_sections(ctx))
        questions.extend(_questions_for_findings(ctx))

        deduped = _dedupe(questions)[:_MAX_QUESTIONS]
        return StageResult.succeeded_for(
            self.stage,
            narrative=(
                "no unresolved item required a human"
                if not deduped
                else f"raised {len(deduped)} question(s) that evidence could not settle"
            ),
            questions=tuple(deduped),
            sections=() if not deduped else (_section(deduped),),
            payload={
                "question_count": len(deduped),
                "question_titles": [question.title for question in deduped],
            },
        )


def _questions_for_blocked_criteria(ctx: StageContext) -> list[Question]:
    """One question per blocked acceptance criterion.

    The domain model requires a blocked criterion to have an open question explaining what is
    missing, so this function is not a nicety: without it the report cannot be constructed at
    all.
    """
    return [
        _question(
            ctx,
            title=f"Cannot verify acceptance criterion: {_clip(criterion.criterion, 180)}",
            description=(
                "Scout could not execute any procedure for this acceptance criterion, so it "
                "cannot say whether the change meets it."
            ),
            context=criterion.criterion,
            investigation_summary=(
                f"State of the attempt: {criterion.notes or 'no procedure was available'}. "
                f"Procedure recorded: {'; '.join(criterion.procedure) or 'none'}."
            ),
            severity=Severity.HIGH,
            options=[
                "The criterion is met; explain how it was confirmed",
                "The criterion is not met; the change needs more work",
                "The criterion is not applicable to this change; say why",
            ],
            evidence=list(criterion.evidence),
            default_assumption=(
                "unanswered means the criterion stays unverified and the change is not "
                "recommended for promotion"
            ),
        )
        for criterion in ctx.inputs.criteria
        if criterion.status is CriterionStatus.BLOCKED
    ]


def _questions_for_unverified_sections(ctx: StageContext) -> list[Question]:
    """One question per material check that produced no result."""
    questions: list[Question] = []
    for section in ctx.inputs.sections:
        if section.status is not ReportSectionStatus.UNVERIFIED:
            continue
        if not section.key.startswith(_MATERIAL_SECTION_PREFIXES):
            continue
        reason = section.unverified_reason or "the check produced no usable result"
        questions.append(
            _question(
                ctx,
                title=f"Unverified check: {_clip(section.title, 180)}",
                description=(
                    f"{section.title} did not produce a trustworthy result for this revision: "
                    f"{reason}"
                ),
                context=section.summary or reason,
                investigation_summary=(
                    "Scout attempted this check and it failed for infrastructure reasons, so "
                    "the absence of findings is not evidence that there is nothing to find."
                ),
                severity=Severity.HIGH,
                options=[
                    "Re-run the check in a working environment",
                    "Accept the gap; state why it is safe to proceed without it",
                ],
                evidence=list(section.evidence),
                default_assumption=(
                    "unanswered leaves the check unverified, so the change cannot be "
                    "recommended for promotion"
                ),
            )
        )
    return questions


def _questions_for_findings(ctx: StageContext) -> list[Question]:
    """One question per serious finding that explicitly demands human judgement.

    Only findings the domain flagged as needing a human are escalated. A finding that cites
    machine evidence does not need to be asked about -- it needs to be fixed, and the report
    already says so.
    """
    return [
        _question(
            ctx,
            title=f"Human judgement required: {_clip(finding.title, 180)}",
            description=finding.description,
            context=finding.recommendation or "no recommendation was recorded",
            investigation_summary=(
                "This finding could not be settled from machine evidence, which is why it is "
                "escalated rather than decided by Scout."
            ),
            severity=finding.severity,
            options=[
                "Accept the risk; record why",
                "Treat it as blocking; the change needs more work",
            ],
            evidence=list(finding.evidence),
            default_assumption="unanswered leaves the finding unresolved",
        )
        for finding in ctx.inputs.findings
        if finding.requires_human and finding.severity >= _HUMAN_FINDING_SEVERITY
    ]


def _question(
    ctx: StageContext,
    *,
    title: str,
    description: str,
    context: str,
    investigation_summary: str,
    severity: Severity,
    options: Sequence[str],
    evidence: Sequence[EvidenceReference],
    default_assumption: str,
) -> Question:
    """Build a question with everything spec section 28 requires."""
    return Question(
        id=new_id(IdKind.QUESTION),
        job_id=ctx.job.id,
        title=title[:300],
        description=description[:2000] or title[:300],
        context=context[:4000],
        investigation_summary=investigation_summary[:4000],
        evidence=list(evidence),
        severity=severity,
        options=[option[:500] for option in options],
        default_assumption=default_assumption[:2000],
        status=QuestionStatus.OPEN,
        created_by="investigation",
        created_by_type=ActorType.AGENT,
    )


def _dedupe(questions: Sequence[Question]) -> list[Question]:
    """Drop questions with an identical title, keeping the first.

    Two sections can describe the same missing check (for example a tool failure recorded on
    both the tool's section and the change-coverage section). Asking twice would train a
    reviewer to ignore the channel.
    """
    seen: set[str] = set()
    unique: list[Question] = []
    for question in questions:
        fingerprint = question.title.lower()
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        unique.append(question)
    return unique


def _section(questions: Sequence[Question]) -> ReportSection:
    """Render the raised questions as an ``INFO`` report section."""
    return ReportSection(
        key="investigating.questions",
        title="Open engineering questions",
        status=ReportSectionStatus.INFO,
        summary=f"{len(questions)} question(s) require a human answer before a recommendation",
        details=[
            f"[{question.severity.value}] {question.title} -- {question.default_assumption}"
            for question in questions[:20]
        ],
        evidence=[
            EvidenceReference(
                kind=EvidenceKind.AGENT_RUN,
                reference_id="investigating",
                summary="the unresolved items were collected from the recorded evidence",
            )
        ],
    )


def _clip(value: str, limit: int) -> str:
    """Trim a title to ``limit`` characters without cutting mid-word where avoidable."""
    collapsed = " ".join(value.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "\u2026"
