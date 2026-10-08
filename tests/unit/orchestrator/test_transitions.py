"""The orchestrator only reaches a new status through the state machine.

Spec section 11 says a job has exactly one explicit state and that nothing assigns it directly; the
state machine's ``assert_transition`` is the single choke point. These tests prove the orchestrator
honours that rather than merely intending to:

* an illegal edge raises, and **no** status write happens -- a partially applied transition is how a
  job's status and its resolved-stage set drift apart;
* the transition context is built from real persisted facts (the resolved-stage projection and the
  open-question count), so a forward edge is refused when its guard is unsatisfied;
* the resume invariant catches the gap the architecture document calls out (a job sitting in
  ``PREPARING`` while ``PREPARING`` and ``ANALYZING`` are both resolved);
* an operator-recorded human decision is what unlocks ``COMPLETED``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fakes import FakeUnitOfWork, make_job_row

from scout_domain.enums import ApprovalDecision, JobStatus
from scout_domain.errors import InvalidJobTransition
from scout_domain.models.job import JobStageOutcome
from scout_orchestrator.errors import ResumeInvariantViolation
from scout_orchestrator.state import (
    apply_transition,
    assert_resume_invariant,
    build_transition_context,
    latest_resolved_stage,
)

if TYPE_CHECKING:
    from scout_domain.models.job import VerificationJob


def _job(uow: FakeUnitOfWork, **overrides: object) -> VerificationJob:
    """Seed a job row and return its domain value."""
    row = uow.add_job(make_job_row(**overrides))
    return uow.jobs.to_domain(row)


async def test_illegal_transition_raises_and_writes_nothing(uow: FakeUnitOfWork) -> None:
    """``QUEUED -> READY_FOR_REVIEW`` is not an edge, so nothing may be written."""
    job = _job(uow, status=JobStatus.QUEUED)

    with pytest.raises(InvalidJobTransition):
        await apply_transition(uow, job, JobStatus.READY_FOR_REVIEW, reason="nonsense")

    assert uow.statuses_written() == []
    assert uow.job_row(job.id).status == JobStatus.QUEUED.value


async def test_forward_progress_is_refused_while_a_predecessor_is_unresolved(
    uow: FakeUnitOfWork,
) -> None:
    """``QUEUED -> TESTING`` requires every earlier stage to be resolved."""
    job = _job(uow, status=JobStatus.QUEUED)
    uow.add_stage(job.id, JobStatus.PREPARING, JobStageOutcome.SUCCEEDED)

    with pytest.raises(InvalidJobTransition) as excinfo:
        await apply_transition(uow, job, JobStatus.TESTING, reason="skip ahead")

    assert "not resolved" in str(excinfo.value)
    assert uow.statuses_written() == []


async def test_forward_progress_is_allowed_once_predecessors_are_resolved(
    uow: FakeUnitOfWork,
) -> None:
    """A skip recorded as resolved is what makes the resumption edge legal (spec section 50)."""
    job = _job(uow, status=JobStatus.QUEUED)
    for stage in (
        JobStatus.PREPARING,
        JobStatus.ANALYZING,
        JobStatus.REVIEWING,
    ):
        uow.add_stage(job.id, stage, JobStageOutcome.SUCCEEDED)
    uow.add_stage(
        job.id,
        JobStatus.SECURITY_SCANNING,
        JobStageOutcome.SKIPPED,
        skip_reason="LOW-risk change does not require a security scan",
    )

    advanced = await apply_transition(uow, job, JobStatus.TESTING, reason="resume at TESTING")

    assert advanced.status is JobStatus.TESTING
    assert uow.statuses_written() == [JobStatus.TESTING]


async def test_transition_context_carries_persisted_facts(uow: FakeUnitOfWork) -> None:
    """The context holds the resolved-stage projection and the open-question count."""
    job = _job(uow, status=JobStatus.QUEUED)
    uow.add_stage(job.id, JobStatus.PREPARING, JobStageOutcome.SUCCEEDED)
    uow.add_question(job.id)
    uow.add_question(job.id)

    context = await build_transition_context(uow, job, reason="investigating")

    assert context.resolved_stages == frozenset({JobStatus.PREPARING})
    assert context.open_questions == 2
    assert context.retries_remaining == 2
    assert context.reason == "investigating"
    assert context.human_decision is None


async def test_open_questions_bar_completion_of_investigation(uow: FakeUnitOfWork) -> None:
    """``INVESTIGATING -> READY_FOR_REVIEW`` is guarded by the open-question count."""
    job = _job(uow, status=JobStatus.INVESTIGATING)
    uow.add_question(job.id)

    with pytest.raises(InvalidJobTransition):
        await apply_transition(uow, job, JobStatus.READY_FOR_REVIEW, reason="no questions?")

    assert uow.statuses_written() == []


async def test_investigation_escalates_when_questions_are_open(uow: FakeUnitOfWork) -> None:
    """Open questions are the guard for the escalation edge, not an obstacle to it."""
    job = _job(uow, status=JobStatus.INVESTIGATING)
    uow.add_question(job.id)

    advanced = await apply_transition(
        uow, job, JobStatus.WAITING_FOR_HUMAN, reason="a human must answer"
    )

    assert advanced.status is JobStatus.WAITING_FOR_HUMAN
    assert uow.statuses_written() == [JobStatus.WAITING_FOR_HUMAN]


async def test_human_decision_is_read_from_the_approvals_table(uow: FakeUnitOfWork) -> None:
    """``COMPLETED`` is reachable only with a valid approval for *this* commit."""
    job = _job(uow, status=JobStatus.READY_FOR_REVIEW)
    uow.add_approval(job_id=job.id, commit_sha=job.head_sha)

    context = await build_transition_context(uow, job)
    assert context.human_decision is ApprovalDecision.APPROVE

    advanced = await apply_transition(uow, job, JobStatus.COMPLETED, reason="human approved")
    assert advanced.status is JobStatus.COMPLETED
    assert uow.statuses_written() == [JobStatus.COMPLETED]


async def test_approval_for_an_older_commit_does_not_authorize(uow: FakeUnitOfWork) -> None:
    """Spec section 74: an old approval is not an authorization for a new revision."""
    job = _job(uow, status=JobStatus.READY_FOR_REVIEW)
    uow.add_approval(job_id=job.id, commit_sha="c" * 40)

    context = await build_transition_context(uow, job)

    assert context.human_decision is None
    with pytest.raises(InvalidJobTransition):
        await apply_transition(uow, job, JobStatus.COMPLETED, reason="stale approval")


async def test_failure_requires_a_reason(uow: FakeUnitOfWork) -> None:
    """Every failure edge is guarded by a reason, so a failure is never unexplained."""
    job = _job(uow, status=JobStatus.TESTING)

    with pytest.raises(InvalidJobTransition):
        await apply_transition(uow, job, JobStatus.FAILED)

    advanced = await apply_transition(
        uow, job, JobStatus.FAILED, reason="pytest exited 1", failure_reason="pytest exited 1"
    )
    assert advanced.status is JobStatus.FAILED
    assert advanced.failure_reason == "pytest exited 1"


async def test_every_transition_is_audited(uow: FakeUnitOfWork) -> None:
    """A status change without a ledger entry is an incident nobody can reconstruct."""
    job = _job(uow, status=JobStatus.RECEIVED)

    await apply_transition(uow, job, JobStatus.QUEUED, reason="hand off to the queue")

    assert uow.audit_actions() == ["job.transition"]
    event = uow.audit_events_for()[0]
    assert event.metadata["from"] == JobStatus.RECEIVED.value
    assert event.metadata["to"] == JobStatus.QUEUED.value


# --------------------------------------------------------------------------------------
# The resume invariant
# --------------------------------------------------------------------------------------


def test_resume_invariant_accepts_the_documented_states(uow: FakeUnitOfWork) -> None:
    """The equal-status, pre-pipeline, and frontier cases are all legal."""
    job = _job(uow, status=JobStatus.PREPARING)

    # The stage about to run is the job's own status.
    assert_resume_invariant(job, JobStatus.PREPARING, resolved_stages=frozenset())

    # A pre-pipeline state is entering the pipeline.
    queued = _job(uow, status=JobStatus.QUEUED)
    assert_resume_invariant(queued, JobStatus.PREPARING, resolved_stages=frozenset())

    # The job is sitting on the most recently resolved stage, which is the state the orchestrator
    # itself leaves behind between stages.
    resolved = frozenset({JobStatus.PREPARING, JobStatus.ANALYZING})
    analyzing = _job(uow, status=JobStatus.ANALYZING)
    assert_resume_invariant(analyzing, JobStatus.REVIEWING, resolved_stages=resolved)

    # A completed pipeline has nothing to assert.
    assert_resume_invariant(job, None, resolved_stages=resolved)


def test_resume_invariant_rejects_a_stale_status(uow: FakeUnitOfWork) -> None:
    """The architecture document's gap: resolved stages ahead of the recorded status.

    With ``PREPARING`` and ``ANALYZING`` resolved, a job still recorded as ``PREPARING`` disagrees
    with its own derivation, and running ``REVIEWING`` from there would produce a report whose
    lifecycle does not match its stages.
    """
    job = _job(uow, status=JobStatus.PREPARING)
    resolved = frozenset({JobStatus.PREPARING, JobStatus.ANALYZING})

    with pytest.raises(ResumeInvariantViolation) as excinfo:
        assert_resume_invariant(job, JobStatus.REVIEWING, resolved_stages=resolved)

    assert excinfo.value.context["status"] == JobStatus.PREPARING.value
    assert excinfo.value.context["next_stage"] == JobStatus.REVIEWING.value
    assert excinfo.value.context["latest_resolved_stage"] == JobStatus.ANALYZING.value


def test_latest_resolved_stage_ignores_non_pipeline_statuses() -> None:
    """Only ordered pipeline members have a frontier position."""
    assert latest_resolved_stage(frozenset()) is None
    assert latest_resolved_stage({JobStatus.COMPLETED}) is None
    assert latest_resolved_stage({JobStatus.PREPARING, JobStatus.TESTING}) is JobStatus.TESTING
