"""Job-state helpers: the only path from one status to another.

The domain's state machine is the authority on whether an edge is legal (spec section 11), but
it needs facts -- which stages are resolved, how many questions are open, whether a human has
recorded a decision, how much retry budget remains. Those facts live in PostgreSQL, so this
module's job is to read them, hand them to the machine, and only then write.

Three properties are deliberate:

* **Nothing here assigns a status.** The write goes through
  :meth:`scout_persistence.repositories.job.JobRepository.update_status`, and only after
  :func:`scout_domain.state.machine.assert_transition` has accepted the edge. A guard failure
  propagates as :class:`~scout_domain.errors.InvalidJobTransition` and the write never happens.
* **Every transition is audited in the same unit of work.** A status change that is not on the
  ledger is an incident nobody can reconstruct.
* **The resume invariant is asserted, not assumed.** ``docs/ARCHITECTURE.md`` section 4.1
  records that the domain layer permits a job to sit in ``PREPARING`` while ``PREPARING`` and
  ``ANALYZING`` are both resolved -- legal, but not derived. Running the wrong stage from that
  state would silently produce a report that disagrees with the recorded status, so the
  orchestrator refuses to proceed and says why.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from scout_domain.enums import ActorType, ApprovalDecision, JobStatus, RiskLevel
from scout_domain.ids import IdKind, new_id
from scout_domain.models.audit import AuditEvent
from scout_domain.models.base import utcnow
from scout_domain.models.job import VerificationJob
from scout_domain.risk import gate_for_risk
from scout_domain.state.machine import (
    TransitionContext,
    assert_transition,
    pipeline_index,
)
from scout_orchestrator.errors import ResumeInvariantViolation

if TYPE_CHECKING:
    from scout_domain.config import ScoutRepositoryConfig
    from scout_domain.models.approval import Approval
    from scout_domain.risk import RiskAssessment
    from scout_persistence.unit_of_work import UnitOfWork

__all__ = [
    "NEVER_SKIPPABLE",
    "PRE_PIPELINE_STATES",
    "RECLAIM_ERROR_PREFIX",
    "SkipDecision",
    "apply_transition",
    "assert_resume_invariant",
    "build_transition_context",
    "record_audit",
    "skip_decision",
]

#: States from which entering the first (or any) unresolved pipeline stage is expected rather
#: than a divergence. ``FAILED`` is here because the retry path moves through it, and ``QUEUED``
#: because the webhook path and the reclaim sweep both re-enqueue work.
PRE_PIPELINE_STATES: Final[frozenset[JobStatus]] = frozenset(
    {JobStatus.RECEIVED, JobStatus.QUEUED, JobStatus.FAILED}
)

#: Stages a repository may never disable, whatever ``.scout/engineering.yaml`` says. PREPARING is
#: structurally required (nothing downstream has a checkout without it), and INVESTIGATING is
#: where escalations are raised, so a repository could otherwise silence every question its own
#: change would have raised.
NEVER_SKIPPABLE: Final[frozenset[JobStatus]] = frozenset(
    {JobStatus.PREPARING, JobStatus.INVESTIGATING}
)

#: Prefix the lease reclaim sweep writes into a stage's ``error`` column.
#:
#: The reclaim happens in one process and its consequence (a retry) is decided in another, so the
#: two need a shared marker. It is a *constant*, not a substring of an arbitrary message: the
#: point is not to classify unknown errors from their text -- which spec section 51 forbids -- but
#: to recognise a marker Scout itself wrote, in the one case where the exception object did not
#: survive because the process that raised it no longer exists.
RECLAIM_ERROR_PREFIX: Final = "reclaimed: "

#: Actor recorded for orchestration decisions. Distinct from ``SYSTEM`` so an operator reading
#: the ledger can tell "Scout's scheduler did this" from "the platform did this".
_ACTOR: Final = "orchestrator"


async def build_transition_context(
    uow: UnitOfWork,
    job: VerificationJob,
    *,
    reason: str | None = None,
) -> TransitionContext:
    """Read the job-level facts the state machine's guards need.

    Everything is read from the database rather than passed in by the caller, so a job cannot
    claim a stage is resolved when no row says so.
    """
    resolved = await uow.stages.resolved_stages(job.id)
    open_questions = await uow.questions.count_open_for_job(job.id)
    return TransitionContext(
        resolved_stages=resolved,
        open_questions=open_questions,
        human_decision=await _latest_human_decision(uow, job),
        retries_remaining=max(0, job.max_attempts - job.attempt),
        reason=reason,
    )


async def _latest_human_decision(uow: UnitOfWork, job: VerificationJob) -> ApprovalDecision | None:
    """Return the most recent still-valid approval decision for this job's commit, if any.

    Validity is commit-scoped on purpose (spec section 74): an approval for an older commit is
    not an authorization for this revision, and treating it as one is how "old approval = still
    valid" bugs reach production.
    """
    approvals: list[Approval] = [
        uow.approvals.to_domain(row)
        for row in await uow.approvals.list_valid_for_commit(job.id, job.head_sha)
    ]
    if not approvals:
        return None
    newest = max(approvals, key=lambda approval: (approval.decided_at, approval.id))
    return newest.decision


async def apply_transition(
    uow: UnitOfWork,
    job: VerificationJob,
    target: JobStatus,
    *,
    reason: str | None = None,
    context: TransitionContext | None = None,
    **fields: Any,
) -> VerificationJob:
    """Validate and persist a status change, returning the updated job value.

    Args:
        uow: The open unit of work. The status write and its audit event commit together.
        job: The current job value. Its ``status`` is the edge's source.
        target: The status to move to.
        reason: Free-text reason; required by the failure, cancellation, and retry edges.
        context: A pre-built context, when the caller has already gathered the facts.
        **fields: Additional columns to persist (``failure_reason``, ``recommendation``, ...).

    Returns:
        An evolved job value with the new status and fields. The caller must use this value;
        ``job.status`` is never assigned anywhere in Scout.

    Raises:
        InvalidJobTransition: when the edge does not exist or its guard is unsatisfied.
    """
    resolved = context or await build_transition_context(uow, job, reason=reason)
    assert_transition(job.status, target, resolved)
    await uow.jobs.update_status(job.id, target, **fields)
    await record_audit(
        uow,
        job,
        "job.transition",
        metadata={
            "from": job.status.value,
            "to": target.value,
            "reason": reason,
            "resolved_stages": sorted(stage.value for stage in resolved.resolved_stages),
            "open_questions": resolved.open_questions,
            "retries_remaining": resolved.retries_remaining,
            "human_decision": (
                None if resolved.human_decision is None else resolved.human_decision.value
            ),
        },
    )
    return job.evolve(status=target, **fields)


async def record_audit(
    uow: UnitOfWork,
    job: VerificationJob,
    action: str,
    *,
    metadata: dict[str, Any] | None = None,
    actor: str = _ACTOR,
    actor_type: ActorType = ActorType.SYSTEM,
) -> None:
    """Append one audit event for a privileged action (spec section 43).

    Built through the domain model rather than the repository's shortcut so that an
    unrecognised actor type cannot be written even by an internal caller.
    """
    await uow.audit_events.append(
        AuditEvent(
            id=new_id(IdKind.AUDIT_EVENT),
            actor=actor,
            actor_type=actor_type,
            action=action,
            resource="verification_job",
            resource_id=job.id,
            timestamp=utcnow(),
            metadata=dict(metadata or {}),
            job_id=job.id,
            correlation_id=job.correlation_id or None,
        )
    )


def assert_resume_invariant(
    job: VerificationJob,
    next_stage: JobStatus | None,
    *,
    resolved_stages: frozenset[JobStatus] = frozenset(),
    skipped_stages: frozenset[JobStatus] = frozenset(),
) -> None:
    """Assert that the job's status agrees with the stage derived from its resolved stages.

    ``docs/ARCHITECTURE.md`` section 4.1 records the gap this closes: because forward progress may
    skip *resolved* stages, the state machine permits a job whose ``PREPARING`` and ``ANALYZING``
    stages are both resolved to sit in ``PREPARING``. That is legal but not *derived*, and running
    whatever stage the derivation produces while the status says something else would silently
    produce a report that disagrees with the recorded lifecycle.

    The permitted cases are exhaustive and documented:

    * ``next_stage is None`` -- the pipeline is complete; the caller handles post-pipeline states.
    * ``job.status is next_stage`` -- the normal resumption path.
    * ``job.status`` is a pre-pipeline state (``RECEIVED``, ``QUEUED``, ``FAILED``) -- the
      orchestrator is about to *enter* the pipeline.
    * ``job.status`` is a pipeline stage that has been resolved, and **every stage resolved ahead of
      it was resolved by a skip**. That is exactly the state the orchestrator itself produces: it
      records a ``SKIPPED`` row for a stage it will not run and then advances the status once, to
      the next stage that will actually run. A stage *completed* ahead of the status is the
      divergence being detected, which is what makes the check meaningful rather than vacuous.

    Raises:
        ResumeInvariantViolation: when the status and the derived stage disagree.
    """
    if next_stage is None or job.status is next_stage or job.status in PRE_PIPELINE_STATES:
        return

    status_index = pipeline_index(job.status)
    if status_index is None:
        raise ResumeInvariantViolation(
            "the job's status is not a pipeline stage, so no resume point can be derived",
            job_id=job.id,
            status=job.status.value,
            next_stage=next_stage.value,
        )

    completed_ahead = sorted(
        stage.value
        for stage in resolved_stages
        if (index := pipeline_index(stage)) is not None
        and index > status_index
        and stage not in skipped_stages
    )
    if completed_ahead:
        raise ResumeInvariantViolation(
            "the job's status lags stages that already completed",
            job_id=job.id,
            status=job.status.value,
            next_stage=next_stage.value,
            stages_completed_ahead_of_status=completed_ahead,
            resolved_stages=sorted(stage.value for stage in resolved_stages),
        )

    if job.status not in resolved_stages:
        raise ResumeInvariantViolation(
            "the job's status is an unresolved stage that is not the derived resume point",
            job_id=job.id,
            status=job.status.value,
            next_stage=next_stage.value,
            resolved_stages=sorted(stage.value for stage in resolved_stages),
        )


def latest_resolved_stage(resolved_stages: frozenset[JobStatus]) -> JobStatus | None:
    """Return the pipeline stage with the greatest index in ``resolved_stages``.

    Non-pipeline members (``COMPLETED`` and friends) are ignored: only pipeline positions are
    ordered, and a status outside the pipeline has no frontier position to compare against.
    """
    ranked = [
        (index, stage) for stage in resolved_stages if (index := pipeline_index(stage)) is not None
    ]
    if not ranked:
        return None
    return max(ranked)[1]


# --------------------------------------------------------------------------------------
# Risk-based execution (spec section 41)
# --------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SkipDecision:
    """Whether a pipeline stage may be skipped, and the auditable reason if so."""

    skip: bool
    reason: str | None = None

    @property
    def runs(self) -> bool:
        """Whether the stage must run."""
        return not self.skip


def skip_decision(
    stage: JobStatus,
    *,
    config: ScoutRepositoryConfig,
    risk: RiskAssessment | None,
    required_stages: frozenset[JobStatus] = frozenset(),
) -> SkipDecision:
    """Decide whether ``stage`` may be skipped for this job (spec sections 41, 77).

    Precedence, highest first, and the ordering is the security property:

    0. **Structural stages.** ``PREPARING`` and ``INVESTIGATING`` are never skippable at all
       (see :data:`NEVER_SKIPPABLE`).
    1. **The operator floor.** ``SCOUT_REQUIRED_STAGES`` is the operator's own list and a
       repository cannot switch off a stage it names. Without this, a pull request could edit
       ``.scout/engineering.yaml`` to disable the check that would have caught it.
    2. **The repository's own required list.** A repository may demand a stage it would
       otherwise be able to disable.
    3. **The repository's disabled list**, recorded as a skip with a reason.
    4. **Security scanning switched off in the repository configuration.**
    5. **Risk.** A LOW-risk change may skip ``SECURITY_SCANNING``: the risk gate for LOW does
       not require human review, which is the domain's own way of saying automated
       verification is sufficient at that level. Every other stage always runs, so no change
       can skip the test suite or the investigation.

    A skip is never silent: the caller records a ``JobStage`` with ``outcome=SKIPPED`` and this
    reason, and the reason reaches the report.
    """
    if stage in NEVER_SKIPPABLE or stage in required_stages:
        return SkipDecision(False)

    if stage in _stage_names(config.verification.required_stages):
        return SkipDecision(False)

    if stage in _stage_names(config.verification.disabled_stages):
        return SkipDecision(
            True,
            "disabled by .scout/engineering.yaml (verification.disabled_stages); the operator "
            "did not require this stage",
        )

    if stage is JobStatus.SECURITY_SCANNING:
        return _security_skip_decision(config=config, risk=risk)

    return SkipDecision(False)


def _security_skip_decision(
    *, config: ScoutRepositoryConfig, risk: RiskAssessment | None
) -> SkipDecision:
    """Decide whether SECURITY_SCANNING may be skipped, on configuration and risk alone."""
    if not config.security.enabled:
        return SkipDecision(
            True,
            "security scanning is disabled by .scout/engineering.yaml (security.enabled=false)",
        )
    if risk is not None and risk.level is RiskLevel.LOW:
        gate = gate_for_risk(risk.level)
        if not gate.requires_human_review:
            return SkipDecision(
                True,
                "risk-based skip: the change was classified LOW risk, for which the approval "
                "gate does not require human review, so a security scan is not required "
                "(spec section 41)",
            )
    return SkipDecision(False)


def _stage_names(raw: list[str]) -> frozenset[JobStatus]:
    """Parse stage names from repository configuration, ignoring unknown values.

    Unknown names are reported as a ``CONFIGURATION`` finding during PREPARING, which is the
    right place for them: this function is called in the middle of the scheduling loop and must
    not fail a job over a spelling mistake it cannot attribute to a file.
    """
    parsed: set[JobStatus] = set()
    for item in raw:
        try:
            parsed.add(JobStatus(str(item).strip().upper()))
        except ValueError:
            continue
    return frozenset(parsed)


def stage_timeout_exceeded(job: VerificationJob, *, max_runtime_seconds: int) -> str | None:
    """Return why the job has exceeded its runtime budget, or ``None``.

    Checked before each stage rather than by a background reaper: the orchestrator is the only
    component that may end the job, and ending it here guarantees the failure is recorded on
    the job and the ledger in the same place as every other decision.
    """
    if job.started_at is None:
        return None
    elapsed = (utcnow() - job.started_at).total_seconds()
    if elapsed <= max_runtime_seconds:
        return None
    return (
        f"the job exceeded its {max_runtime_seconds}-second runtime budget ({elapsed:.0f}s elapsed)"
    )


def reclaim_error(reason: str) -> str:
    """Render the error text a lease reclaim writes onto an abandoned stage."""
    return f"{RECLAIM_ERROR_PREFIX}{reason}"


def is_reclaim_error(error: str | None) -> bool:
    """Whether an error string was written by the lease reclaim sweep."""
    return error is not None and error.startswith(RECLAIM_ERROR_PREFIX)
