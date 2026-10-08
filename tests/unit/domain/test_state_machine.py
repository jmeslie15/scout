"""Tests for the job state machine (spec sections 11, 50).

The state machine is the single invariant that keeps a verification job honest, so it is
tested more thoroughly than anything else in the domain layer.
"""

from __future__ import annotations

import pytest

from scout_domain.enums import ApprovalDecision, JobStatus
from scout_domain.errors import InvalidJobTransition
from scout_domain.state import (
    VERIFICATION_PIPELINE,
    TransitionContext,
    allowed_transitions,
    assert_transition,
    can_transition,
    is_forward_progress,
    next_pipeline_stage,
    pipeline_index,
    transition,
)


def ctx(**kwargs: object) -> TransitionContext:
    return TransitionContext(**kwargs)  # type: ignore[arg-type]


class TestPipelineShape:
    def test_pipeline_is_the_documented_order(self) -> None:
        assert VERIFICATION_PIPELINE == (
            JobStatus.PREPARING,
            JobStatus.ANALYZING,
            JobStatus.REVIEWING,
            JobStatus.SECURITY_SCANNING,
            JobStatus.TESTING,
            JobStatus.FEATURE_VERIFYING,
            JobStatus.INVESTIGATING,
        )

    def test_pre_and_post_pipeline_states_are_not_in_the_pipeline(self) -> None:
        for status in (
            JobStatus.RECEIVED,
            JobStatus.QUEUED,
            JobStatus.READY_FOR_REVIEW,
            JobStatus.WAITING_FOR_HUMAN,
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        ):
            assert pipeline_index(status) is None

    def test_forward_progress_is_strictly_ordered(self) -> None:
        assert is_forward_progress(JobStatus.PREPARING, JobStatus.TESTING)
        assert not is_forward_progress(JobStatus.TESTING, JobStatus.PREPARING)
        assert not is_forward_progress(JobStatus.TESTING, JobStatus.TESTING)
        assert not is_forward_progress(JobStatus.QUEUED, JobStatus.TESTING)


class TestHappyPath:
    def test_full_linear_pipeline(self) -> None:
        """The linear chain from spec section 11 must be traversable end to end."""
        status = transition(JobStatus.RECEIVED, JobStatus.QUEUED)
        resolved: set[JobStatus] = set()
        for stage in VERIFICATION_PIPELINE:
            status = transition(
                status, stage, TransitionContext(resolved_stages=frozenset(resolved))
            )
            # The stage has now run to completion, so it counts as resolved for the next
            # hop. Note the guard for entering a stage never requires that stage itself.
            resolved.add(stage)
        status = transition(
            status,
            JobStatus.READY_FOR_REVIEW,
            TransitionContext(resolved_stages=frozenset(resolved), open_questions=0),
        )
        assert status is JobStatus.READY_FOR_REVIEW

        status = transition(
            status,
            JobStatus.COMPLETED,
            TransitionContext(human_decision=ApprovalDecision.APPROVE),
        )
        assert status is JobStatus.COMPLETED

    def test_received_to_queued(self) -> None:
        assert transition(JobStatus.RECEIVED, JobStatus.QUEUED) is JobStatus.QUEUED


class TestPrerequisiteGuard:
    """Forward progress requires every earlier stage to be resolved.

    This is the guard that makes resumability safe: it is what stops a bug from running
    tests against a workspace that was never checked out.
    """

    def test_cannot_enter_first_stage_without_anything(self) -> None:
        assert can_transition(JobStatus.QUEUED, JobStatus.PREPARING, ctx())

    def test_cannot_skip_a_prerequisite_stage(self) -> None:
        assert not can_transition(JobStatus.QUEUED, JobStatus.TESTING, ctx())

    def test_resuming_after_a_crash_can_skip_resolved_stages(self) -> None:
        """Spec section 50: a restart must not repeat completed work."""
        resolved = frozenset({JobStatus.PREPARING, JobStatus.ANALYZING, JobStatus.REVIEWING})
        assert can_transition(
            JobStatus.QUEUED, JobStatus.SECURITY_SCANNING, ctx(resolved_stages=resolved)
        )

    def test_explicitly_skipped_stages_count_as_resolved(self) -> None:
        """Spec section 41: a low-risk change legitimately skips security scanning."""
        resolved = frozenset(
            {
                JobStatus.PREPARING,
                JobStatus.ANALYZING,
                JobStatus.REVIEWING,
                JobStatus.SECURITY_SCANNING,  # skipped, recorded with a reason
            }
        )
        assert can_transition(JobStatus.QUEUED, JobStatus.TESTING, ctx(resolved_stages=resolved))

    def test_guard_failure_message_names_the_missing_stage(self) -> None:
        with pytest.raises(InvalidJobTransition) as excinfo:
            assert_transition(JobStatus.QUEUED, JobStatus.TESTING, ctx())
        assert "PREPARING" in str(excinfo.value)

    def test_forward_guard_also_applies_within_the_pipeline(self) -> None:
        resolved = frozenset({JobStatus.PREPARING})
        assert not can_transition(
            JobStatus.PREPARING, JobStatus.TESTING, ctx(resolved_stages=resolved)
        )
        assert can_transition(
            JobStatus.PREPARING, JobStatus.ANALYZING, ctx(resolved_stages=resolved)
        )


class TestPipelineExitGuards:
    def test_investigating_to_ready_requires_no_open_questions(self) -> None:
        assert can_transition(JobStatus.INVESTIGATING, JobStatus.READY_FOR_REVIEW, ctx())
        assert not can_transition(
            JobStatus.INVESTIGATING, JobStatus.READY_FOR_REVIEW, ctx(open_questions=2)
        )

    def test_investigating_to_waiting_requires_open_questions(self) -> None:
        assert can_transition(
            JobStatus.INVESTIGATING, JobStatus.WAITING_FOR_HUMAN, ctx(open_questions=1)
        )
        assert not can_transition(JobStatus.INVESTIGATING, JobStatus.WAITING_FOR_HUMAN, ctx())

    def test_waiting_for_human_resumes_investigation_once_answered(self) -> None:
        assert can_transition(JobStatus.WAITING_FOR_HUMAN, JobStatus.INVESTIGATING, ctx())
        assert not can_transition(
            JobStatus.WAITING_FOR_HUMAN, JobStatus.INVESTIGATING, ctx(open_questions=1)
        )

    def test_reviewer_can_raise_a_new_question(self) -> None:
        assert can_transition(
            JobStatus.READY_FOR_REVIEW, JobStatus.WAITING_FOR_HUMAN, ctx(open_questions=1)
        )

    def test_completion_requires_a_recorded_human_decision(self) -> None:
        """Spec section 46: Scout cannot approve its own work."""
        assert not can_transition(JobStatus.READY_FOR_REVIEW, JobStatus.COMPLETED, ctx())
        for decision in ApprovalDecision:
            assert can_transition(
                JobStatus.READY_FOR_REVIEW,
                JobStatus.COMPLETED,
                ctx(human_decision=decision),
            )

    def test_waiting_for_human_can_be_closed_with_a_decision(self) -> None:
        assert can_transition(
            JobStatus.WAITING_FOR_HUMAN,
            JobStatus.COMPLETED,
            ctx(human_decision=ApprovalDecision.REJECT, open_questions=3),
        )


class TestRetry:
    def test_failed_job_can_be_requeued_when_budget_remains(self) -> None:
        assert can_transition(JobStatus.FAILED, JobStatus.QUEUED, ctx(retries_remaining=2))

    def test_failed_job_cannot_be_requeued_without_budget(self) -> None:
        assert not can_transition(JobStatus.FAILED, JobStatus.QUEUED, ctx(retries_remaining=0))

    def test_retry_reason_is_optional_but_recorded(self) -> None:
        assert can_transition(
            JobStatus.FAILED, JobStatus.QUEUED, ctx(retries_remaining=1, reason="operator retry")
        )


class TestFailureAndCancellation:
    @pytest.mark.parametrize(
        "source",
        [
            status
            for status in JobStatus
            if status not in {JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED}
        ],
    )
    def test_failure_is_reachable_from_every_live_state(self, source: JobStatus) -> None:
        """Rule 12: no failure may be unrepresentable."""
        assert can_transition(source, JobStatus.FAILED, ctx(reason="boom"))

    def test_failure_requires_a_reason(self) -> None:
        assert not can_transition(JobStatus.TESTING, JobStatus.FAILED, ctx())

    @pytest.mark.parametrize(
        "source",
        [
            status
            for status in JobStatus
            if status not in {JobStatus.COMPLETED, JobStatus.CANCELLED}
        ],
    )
    def test_cancellation_is_reachable_from_every_non_terminal(self, source: JobStatus) -> None:
        assert can_transition(source, JobStatus.CANCELLED, ctx(reason="operator cancelled"))

    @pytest.mark.parametrize("source", [JobStatus.COMPLETED, JobStatus.CANCELLED])
    def test_terminals_are_dead_ends(self, source: JobStatus) -> None:
        assert allowed_transitions(source) == frozenset()

    def test_failed_can_be_cancelled(self) -> None:
        assert can_transition(JobStatus.FAILED, JobStatus.CANCELLED, ctx(reason="abandoned"))


class TestIllegalTransitions:
    def test_self_transition_is_rejected(self) -> None:
        with pytest.raises(InvalidJobTransition, match="already occupies"):
            assert_transition(JobStatus.TESTING, JobStatus.TESTING)

    def test_backwards_transition_is_rejected(self) -> None:
        with pytest.raises(InvalidJobTransition):
            assert_transition(JobStatus.TESTING, JobStatus.ANALYZING, ctx())

    def test_error_lists_permitted_targets(self) -> None:
        with pytest.raises(InvalidJobTransition) as excinfo:
            assert_transition(JobStatus.COMPLETED, JobStatus.QUEUED)
        assert "permitted" in str(excinfo.value)

    def test_error_context_is_structured(self) -> None:
        with pytest.raises(InvalidJobTransition) as excinfo:
            assert_transition(JobStatus.QUEUED, JobStatus.TESTING, ctx())
        assert excinfo.value.context["source"] == "QUEUED"
        assert excinfo.value.context["target"] == "TESTING"
        assert "guard_failure" in excinfo.value.context


class TestNextPipelineStage:
    def test_returns_first_unresolved(self) -> None:
        assert next_pipeline_stage(set()) is JobStatus.PREPARING
        assert next_pipeline_stage({JobStatus.PREPARING}) is JobStatus.ANALYZING
        assert (
            next_pipeline_stage({JobStatus.PREPARING, JobStatus.ANALYZING}) is JobStatus.REVIEWING
        )

    def test_returns_none_when_complete(self) -> None:
        assert next_pipeline_stage(set(VERIFICATION_PIPELINE)) is None

    def test_from_stage_starts_the_search_at_or_after_it(self) -> None:
        """`from_stage` bounds the search; it does not force a resolved stage to re-run."""
        resolved = {JobStatus.PREPARING, JobStatus.ANALYZING, JobStatus.REVIEWING}
        assert (
            next_pipeline_stage(resolved, from_stage=JobStatus.ANALYZING)
            is JobStatus.SECURITY_SCANNING
        )

    def test_forcing_a_stage_to_re_run_means_removing_it_from_resolved(self) -> None:
        """The single mechanism for re-running a stage: it is no longer resolved."""
        resolved = {JobStatus.PREPARING, JobStatus.REVIEWING}
        assert next_pipeline_stage(resolved, from_stage=JobStatus.ANALYZING) is JobStatus.ANALYZING

    def test_skipped_stages_are_not_revisited(self) -> None:
        resolved = set(VERIFICATION_PIPELINE) - {JobStatus.TESTING}
        assert next_pipeline_stage(resolved) is JobStatus.TESTING
