"""The job state machine (spec sections 11 and 50).

Design notes
------------

A naive implementation models the pipeline as a fixed chain
(``PREPARING -> ANALYZING -> REVIEWING -> ...``) and forbids every other edge.
That breaks two hard requirements:

* **Resumability (spec section 50).** After a worker crash the orchestrator must
  resume from the last *incomplete* stage. If stages 1-3 already succeeded it must
  move straight to stage 4, which the naive chain forbids.
* **Risk-based execution (spec section 41).** A low-risk PR may legitimately skip
  ``SECURITY_SCANNING``, so requiring every predecessor to have *completed* would
  make the skip impossible to represent.

So the machine is modelled as an ordered pipeline plus an explicit notion of a
*resolved* stage (completed **or** deliberately skipped with a recorded reason):

* Forward progress is permitted to any later stage, provided every earlier stage is
  already resolved. This makes resumption and skipping first-class and auditable,
  and it makes "no stage is silently skipped" a domain invariant rather than a
  convention.
* Backward movement is permitted only through the named recovery edges
  (``FAILED -> QUEUED`` retry, ``WAITING_FOR_HUMAN -> INVESTIGATING`` resume), each
  with its own guard.
* Failure and cancellation are permitted from any non-terminal state.

Every edge carries a human-readable rationale so that operators reading an audit
trail can see *why* a transition was legal.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Final

from scout_domain.enums import ApprovalDecision, JobStatus
from scout_domain.errors import InvalidJobTransition

__all__ = [
    "VERIFICATION_PIPELINE",
    "Guard",
    "Transition",
    "TransitionContext",
    "allowed_transitions",
    "assert_transition",
    "can_transition",
    "is_forward_progress",
    "next_pipeline_stage",
    "pipeline_index",
    "transition",
]

#: The ordered verification pipeline. ``RECEIVED`` and ``QUEUED`` are pre-pipeline
#: states; ``READY_FOR_REVIEW`` and beyond are post-pipeline.
VERIFICATION_PIPELINE: Final[tuple[JobStatus, ...]] = (
    JobStatus.PREPARING,
    JobStatus.ANALYZING,
    JobStatus.REVIEWING,
    JobStatus.SECURITY_SCANNING,
    JobStatus.TESTING,
    JobStatus.FEATURE_VERIFYING,
    JobStatus.INVESTIGATING,
)

_PIPELINE_INDEX: Final[dict[JobStatus, int]] = {
    status: index for index, status in enumerate(VERIFICATION_PIPELINE)
}

#: States from which failure or cancellation is meaningless.
_TOMBSTONES: Final[frozenset[JobStatus]] = frozenset({JobStatus.COMPLETED, JobStatus.CANCELLED})

#: A guard inspects context and returns ``None`` when satisfied, or a human-readable
#: explanation of what is missing. Declared after :class:`TransitionContext` below.


@dataclass(frozen=True, slots=True)
class TransitionContext:
    """Everything the state machine needs in order to validate an edge.

    This is intentionally small. Guards must remain pure predicates over job-level
    facts; anything requiring I/O belongs in the orchestrator, which supplies the
    already-resolved facts here.
    """

    #: Stages that are complete or were explicitly skipped with a recorded reason.
    resolved_stages: frozenset[JobStatus] = frozenset()
    #: Number of questions not yet in a closed state.
    open_questions: int = 0
    #: A human authorization decision, if one has been recorded.
    human_decision: ApprovalDecision | None = None
    #: Remaining automatic retries for this job.
    retries_remaining: int = 0
    #: Free-text reason supplied by the caller; required by some edges.
    reason: str | None = None

    @property
    def stages_are_resolved(self) -> bool:
        return bool(self.resolved_stages)


#: A guard inspects context and returns ``None`` when satisfied, or a human-readable
#: explanation of what is missing.
Guard = Callable[[TransitionContext], str | None]


@dataclass(frozen=True, slots=True)
class Transition:
    """A single permitted edge, with the guard that must hold and why it exists."""

    source: JobStatus
    target: JobStatus
    rationale: str
    guard: Callable[[TransitionContext], str | None] | None = field(default=None, compare=False)

    def check(self, context: TransitionContext) -> str | None:
        """Return ``None`` if this edge is permitted, else the failure reason."""
        if self.guard is None:
            return None
        return self.guard(context)


# --------------------------------------------------------------------------------------
# Guards
# --------------------------------------------------------------------------------------


def _forward_guard(target: JobStatus) -> Callable[[TransitionContext], str | None]:
    """Build the guard for forward progress into ``target``.

    Every pipeline stage preceding ``target`` must already be resolved. This is what
    prevents a bug from skipping repository preparation and running tests against an
    un-checked-out workspace, and what guarantees an explicit record exists for each
    stage that did not run.
    """
    target_index = _PIPELINE_INDEX[target]
    required = VERIFICATION_PIPELINE[:target_index]

    def guard(context: TransitionContext) -> str | None:
        missing = [stage.value for stage in required if stage not in context.resolved_stages]
        if missing:
            return (
                f"cannot enter {target.value}: prerequisite stage(s) not resolved: "
                f"{', '.join(missing)}"
            )
        return None

    return guard


def _guard_no_open_questions(context: TransitionContext) -> str | None:
    if context.open_questions > 0:
        return f"{context.open_questions} question(s) are still open"
    return None


def _guard_has_open_questions(context: TransitionContext) -> str | None:
    if context.open_questions == 0:
        return "no open questions to escalate"
    return None


def _guard_human_decision_recorded(context: TransitionContext) -> str | None:
    if context.human_decision is None:
        return "no human authorization decision has been recorded"
    return None


def _guard_retries_remaining(context: TransitionContext) -> str | None:
    if context.retries_remaining <= 0:
        return "retry budget exhausted"
    return None


def _guard_reason_present(context: TransitionContext) -> str | None:
    if not context.reason:
        return "a reason is required for this transition"
    return None


# --------------------------------------------------------------------------------------
# Edge table
# --------------------------------------------------------------------------------------


def _build_transitions() -> tuple[Transition, ...]:
    edges: list[Transition] = []

    # -- Pre-pipeline ------------------------------------------------------------------
    edges.append(
        Transition(
            source=JobStatus.RECEIVED,
            target=JobStatus.QUEUED,
            rationale="A persisted job is handed to the queue.",
        )
    )

    # -- Entry into the pipeline -------------------------------------------------------
    # QUEUED may enter any stage whose predecessors are resolved: this is what makes
    # resumption after an orchestrator or worker restart cheap and safe.
    for stage in VERIFICATION_PIPELINE:
        edges.append(
            Transition(
                source=JobStatus.QUEUED,
                target=stage,
                rationale=(
                    "Enter the pipeline at the first unresolved stage; resumption after a "
                    "restart must not repeat completed work."
                ),
                guard=_forward_guard(stage),
            )
        )

    # -- Forward progress within the pipeline ------------------------------------------
    for index, source in enumerate(VERIFICATION_PIPELINE):
        for target in VERIFICATION_PIPELINE[index + 1 :]:
            edges.append(
                Transition(
                    source=source,
                    target=target,
                    rationale="Advance to a later stage once every earlier stage is resolved.",
                    guard=_forward_guard(target),
                )
            )

    # -- Pipeline exit ------------------------------------------------------------------
    edges.extend(
        [
            Transition(
                source=JobStatus.INVESTIGATING,
                target=JobStatus.READY_FOR_REVIEW,
                rationale="Investigation produced no unresolved questions; analysis is complete.",
                guard=_guard_no_open_questions,
            ),
            Transition(
                source=JobStatus.INVESTIGATING,
                target=JobStatus.WAITING_FOR_HUMAN,
                rationale=(
                    "Investigation could not resolve an ambiguity from evidence; a human "
                    "must answer before Scout can reach a recommendation."
                ),
                guard=_guard_has_open_questions,
            ),
            Transition(
                source=JobStatus.WAITING_FOR_HUMAN,
                target=JobStatus.INVESTIGATING,
                rationale=(
                    "All questions were answered; investigation resumes with the new "
                    "evidence in scope."
                ),
                guard=_guard_no_open_questions,
            ),
            Transition(
                source=JobStatus.WAITING_FOR_HUMAN,
                target=JobStatus.READY_FOR_REVIEW,
                rationale=(
                    "Questions were answered or dismissed without requiring further "
                    "automated investigation."
                ),
                guard=_guard_no_open_questions,
            ),
            Transition(
                source=JobStatus.READY_FOR_REVIEW,
                target=JobStatus.WAITING_FOR_HUMAN,
                rationale="A reviewer raised a new question, returning the job to escalation.",
                guard=_guard_has_open_questions,
            ),
            Transition(
                source=JobStatus.READY_FOR_REVIEW,
                target=JobStatus.COMPLETED,
                rationale=(
                    "A human recorded an authorization decision. Completing the job closes "
                    "the verification lifecycle; the decision itself (APPROVE, "
                    "REQUEST_CHANGES, or REJECT) is preserved on the approval record and is "
                    "never inferred from the job status."
                ),
                guard=_guard_human_decision_recorded,
            ),
            Transition(
                source=JobStatus.WAITING_FOR_HUMAN,
                target=JobStatus.COMPLETED,
                rationale=(
                    "A human closed the job with an explicit decision while questions "
                    "remained open, e.g. rejection without further investigation."
                ),
                guard=_guard_human_decision_recorded,
            ),
        ]
    )

    # -- Retry (spec section 51) --------------------------------------------------------
    edges.append(
        Transition(
            source=JobStatus.FAILED,
            target=JobStatus.QUEUED,
            rationale=(
                "An operator or the retry policy re-queues a retryable failure. The "
                "orchestrator resumes at the first unresolved stage."
            ),
            guard=_guard_retries_remaining,
        )
    )

    # -- Failure (spec section 51) ------------------------------------------------------
    # Failure is permitted from every state that is not a tombstone, so that a crash,
    # timeout, or non-retryable error is always representable rather than being
    # swallowed (Rule 12).
    failure_sources = [
        status for status in JobStatus if status not in _TOMBSTONES | {JobStatus.FAILED}
    ]
    for source in failure_sources:
        edges.append(
            Transition(
                source=source,
                target=JobStatus.FAILED,
                rationale="The job failed; the reason is recorded on the job and audit log.",
                guard=_guard_reason_present,
            )
        )

    # -- Cancellation -------------------------------------------------------------------
    cancel_sources = [status for status in JobStatus if status not in _TOMBSTONES]
    for source in cancel_sources:
        edges.append(
            Transition(
                source=source,
                target=JobStatus.CANCELLED,
                rationale="An operator cancelled the job.",
                guard=_guard_reason_present,
            )
        )

    return tuple(edges)


TRANSITIONS: Final[tuple[Transition, ...]] = _build_transitions()

_INDEX: Final[dict[tuple[JobStatus, JobStatus], Transition]] = {
    (edge.source, edge.target): edge for edge in TRANSITIONS
}


# --------------------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------------------


def pipeline_index(status: JobStatus) -> int | None:
    """Return ``status``'s position in the verification pipeline, or ``None``."""
    return _PIPELINE_INDEX.get(status)


def is_forward_progress(source: JobStatus, target: JobStatus) -> bool:
    """Return ``True`` if ``target`` is strictly later in the pipeline than ``source``."""
    source_index = _PIPELINE_INDEX.get(source)
    target_index = _PIPELINE_INDEX.get(target)
    if source_index is None or target_index is None:
        return False
    return target_index > source_index


def allowed_transitions(source: JobStatus) -> frozenset[JobStatus]:
    """Return every state reachable from ``source``, ignoring guard preconditions."""
    return frozenset(edge.target for edge in TRANSITIONS if edge.source is source)


def can_transition(
    source: JobStatus,
    target: JobStatus,
    context: TransitionContext | None = None,
) -> bool:
    """Return ``True`` if the transition is structurally valid and its guard holds."""
    edge = _INDEX.get((source, target))
    if edge is None:
        return False
    return edge.check(context or TransitionContext()) is None


def transition(
    source: JobStatus,
    target: JobStatus,
    context: TransitionContext | None = None,
) -> JobStatus:
    """Validate and apply a transition, returning ``target``.

    Raises:
        InvalidJobTransition: if the edge does not exist or its guard is unsatisfied.
    """
    assert_transition(source, target, context)
    return target


def assert_transition(
    source: JobStatus,
    target: JobStatus,
    context: TransitionContext | None = None,
) -> None:
    """Validate a transition, raising :class:`InvalidJobTransition` if it is illegal.

    This is the single choke point for job status changes. Callers must never assign
    ``job.status`` directly.
    """
    if source is target:
        raise InvalidJobTransition(
            "A job may not transition to the state it already occupies.",
            source=source.value,
            target=target.value,
        )

    resolved_context = context or TransitionContext()
    edge = _INDEX.get((source, target))
    if edge is None:
        allowed = ", ".join(sorted(status.value for status in allowed_transitions(source)))
        raise InvalidJobTransition(
            f"Transition {source.value} -> {target.value} is not permitted.",
            source=source.value,
            target=target.value,
            permitted=allowed,
        )

    failure_reason = edge.check(resolved_context)
    if failure_reason is not None:
        raise InvalidJobTransition(
            f"Transition {source.value} -> {target.value} is not permitted: {failure_reason}",
            source=source.value,
            target=target.value,
            guard_failure=failure_reason,
        )


def next_pipeline_stage(
    resolved_stages: Iterable[JobStatus],
    *,
    from_stage: JobStatus | None = None,
) -> JobStatus | None:
    """Return the first pipeline stage that still needs to run.

    Args:
        resolved_stages: Stages already completed or explicitly skipped.
        from_stage: When supplied, searching starts at this stage (inclusive) so a
            caller can re-run a single stage without restarting the pipeline.

    Returns:
        The next stage to execute, or ``None`` when the pipeline is complete.
    """
    resolved = set(resolved_stages)
    start = _PIPELINE_INDEX.get(from_stage, 0) if from_stage is not None else 0
    for stage in VERIFICATION_PIPELINE[start:]:
        if stage not in resolved:
            return stage
    return None
