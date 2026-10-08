"""Tests for the recommendation gates and the job/approval/question models.

The recommendation gates in :class:`VerificationReport` are how spec section 2's
"Scout should never silently transition from analysis to deployment authority" becomes
enforced behaviour. These tests exist to make weakening them a visible, failing change.

The approval and job tests cover two more load-bearing rules: Scout cannot authorize its
own work (section 46), and an approval is bound to an exact commit (section 74).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from scout_domain.enums import (
    ActorType,
    ApprovalDecision,
    CriterionStatus,
    EvidenceKind,
    FindingCategory,
    JobStatus,
    QuestionStatus,
    Recommendation,
    Severity,
    VerificationStatus,
)
from scout_domain.ids import IdKind, new_id, new_ulid, validate_ulid
from scout_domain.models.approval import Approval
from scout_domain.models.evidence import EvidenceReference
from scout_domain.models.finding import Finding, FindingOrigin
from scout_domain.models.job import CommitRange, JobStage, JobStageOutcome, VerificationJob
from scout_domain.models.question import Question, QuestionAnswer
from scout_domain.models.report import (
    CriterionVerification,
    ReportSection,
    ReportSectionStatus,
    TestOutcomeSummary,
    VerificationReport,
)
from scout_domain.models.repository import RepositoryRef


def evidence() -> list[EvidenceReference]:
    return [
        EvidenceReference(
            kind=EvidenceKind.TEST_RUN,
            reference_id="trun_1",
            summary="pytest: 183 passed, 0 failed",
        )
    ]


def finding(severity: Severity, **overrides: object) -> Finding:
    base: dict[str, object] = {
        "id": new_id(IdKind.FINDING),
        "job_id": "job_1",
        "severity": severity,
        "category": FindingCategory.SECURITY,
        "title": "f",
        "description": "d",
        "repository": "edrevel/scout",
        "origin": FindingOrigin.TOOL,
        "source": "bandit",
        "confidence": 1.0,
        "evidence": evidence(),
    }
    base.update(overrides)
    return Finding(**base)  # type: ignore[arg-type]


def question(status: QuestionStatus = QuestionStatus.OPEN) -> Question:
    base: dict[str, object] = {
        "id": new_id(IdKind.QUESTION),
        "job_id": "job_1",
        "title": "Should managers see export metadata?",
        "description": "The new endpoint returns metadata to the manager role.",
        "created_by": "investigation",
        "status": status,
    }
    if status is QuestionStatus.ANSWERED:
        base["answers"] = [
            QuestionAnswer(
                id=new_id(IdKind.ANSWER),
                question_id=str(base["id"]),
                body="No.",
                answered_by="eng-lead",
            )
        ]
    return Question(**base)  # type: ignore[arg-type]


def criterion(status: CriterionStatus, **overrides: object) -> CriterionVerification:
    base: dict[str, object] = {
        "criterion": "Only administrators can export employee data",
        "status": status,
        "actual_result": "admin 200, employee 403",
        "evidence": evidence(),
    }
    base.update(overrides)
    return CriterionVerification(**base)  # type: ignore[arg-type]


def report(**overrides: object) -> VerificationReport:
    base: dict[str, object] = {
        "job_id": "job_1",
        "repository_full_name": "edrevel/scout",
        "pull_request_number": 42,
        "commit_sha": "b" * 40,
        "base_sha": "a" * 40,
        "executive_summary": "Verified against commit " + "b" * 7,
        "recommendation": Recommendation.PASS,
        "verification_status": VerificationStatus.PASSED,
    }
    base.update(overrides)
    return VerificationReport(**base)  # type: ignore[arg-type]


class TestRecommendationGates:
    def test_clean_change_may_pass(self) -> None:
        assert report().recommendation is Recommendation.PASS

    def test_open_questions_block_pass(self) -> None:
        for recommendation in (Recommendation.PASS, Recommendation.PASS_WITH_WARNINGS):
            with pytest.raises(ValueError, match="open question"):
                report(open_questions=[question()], recommendation=recommendation)

    def test_open_questions_allow_human_review(self) -> None:
        assert report(
            open_questions=[question()], recommendation=Recommendation.REQUIRES_HUMAN_REVIEW
        ).requires_human

    def test_critical_findings_block_pass(self) -> None:
        for recommendation in (Recommendation.PASS, Recommendation.PASS_WITH_WARNINGS):
            with pytest.raises(ValueError, match="CRITICAL"):
                report(security=[finding(Severity.CRITICAL)], recommendation=recommendation)

    def test_critical_findings_allow_request_changes(self) -> None:
        assert report(
            security=[finding(Severity.CRITICAL)], recommendation=Recommendation.REQUEST_CHANGES
        ).blocking_findings

    def test_high_findings_block_a_bare_pass(self) -> None:
        with pytest.raises(ValueError, match="HIGH"):
            report(code_review=[finding(Severity.HIGH)], recommendation=Recommendation.PASS)

    def test_high_findings_allow_pass_with_warnings(self) -> None:
        assert (
            report(
                code_review=[finding(Severity.HIGH)],
                recommendation=Recommendation.PASS_WITH_WARNINGS,
            ).recommendation
            is Recommendation.PASS_WITH_WARNINGS
        )

    def test_failing_tests_block_pass(self) -> None:
        with pytest.raises(ValueError, match="failing test"):
            report(tests=TestOutcomeSummary(total=10, passed=8, failed=2))

    def test_failed_criterion_blocks_pass(self) -> None:
        with pytest.raises(ValueError, match="failed acceptance criterion"):
            report(feature_verification=[criterion(CriterionStatus.FAILED)])

    def test_blocked_criterion_requires_an_open_question(self) -> None:
        with pytest.raises(ValueError, match="blocked acceptance criterion"):
            report(
                feature_verification=[criterion(CriterionStatus.BLOCKED, evidence=[])],
                recommendation=Recommendation.REQUIRES_HUMAN_REVIEW,
            )

    def test_a_criterion_cannot_pass_without_evidence(self) -> None:
        """Spec section 25: every verification step must produce evidence."""
        with pytest.raises(ValueError, match="without evidence"):
            criterion(CriterionStatus.PASSED, evidence=[])

    def test_a_criterion_cannot_pass_without_an_observed_result(self) -> None:
        with pytest.raises(ValueError, match="without an observed result"):
            criterion(CriterionStatus.PASSED, actual_result="")


class TestReportDerivedViews:
    def test_findings_are_aggregated_across_sections(self) -> None:
        r = report(
            code_review=[finding(Severity.LOW)],
            security=[finding(Severity.HIGH)],
            static_analysis=[finding(Severity.MEDIUM)],
            recommendation=Recommendation.REQUEST_CHANGES,
        )
        assert len(r.all_findings) == 3
        assert r.findings_by_severity()[Severity.HIGH] == 1

    def test_high_and_critical_are_blocking(self) -> None:
        r = report(
            code_review=[finding(Severity.HIGH), finding(Severity.LOW)],
            security=[finding(Severity.CRITICAL)],
            recommendation=Recommendation.REQUEST_CHANGES,
        )
        assert len(r.blocking_findings) == 2

    def test_check_run_lines_include_tests_and_questions(self) -> None:
        r = report(
            tests=TestOutcomeSummary(total=5, passed=5),
            open_questions=[question()],
            recommendation=Recommendation.REQUIRES_HUMAN_REVIEW,
        )
        lines = " ".join(r.check_run_summary_lines())
        assert "5 passed" in lines
        assert "question" in lines

    def test_unverified_section_must_explain_itself(self) -> None:
        with pytest.raises(ValueError, match="UNVERIFIED section"):
            ReportSection(
                key="security",
                title="Security",
                status=ReportSectionStatus.UNVERIFIED,
            )

    def test_unverified_areas_are_surfaced(self) -> None:
        r = report(
            unverified_areas=["dependency scan could not reach the registry"],
            recommendation=Recommendation.REQUIRES_HUMAN_REVIEW,
        )
        assert any("could not be verified" in line for line in r.check_run_summary_lines())


class TestVerificationJob:
    def job(self, **overrides: object) -> VerificationJob:
        base: dict[str, object] = {
            "id": new_id(IdKind.JOB),
            "repository": RepositoryRef(full_name="edrevel/scout"),
            "repository_full_name": "edrevel/scout",
            "pull_request_number": 42,
            "commits": CommitRange(base_sha="a" * 40, head_sha="b" * 40),
        }
        base.update(overrides)
        return VerificationJob(**base)  # type: ignore[arg-type]

    def test_commit_range_rejects_identical_shas(self) -> None:
        with pytest.raises(ValueError, match="identical"):
            CommitRange(base_sha="a" * 40, head_sha="a" * 40)

    def test_repository_name_must_match(self) -> None:
        with pytest.raises(ValueError, match="repository_full_name"):
            self.job(repository_full_name="other/repo")

    def test_failed_job_must_record_a_reason(self) -> None:
        with pytest.raises(ValueError, match="failure_reason"):
            self.job(status=JobStatus.FAILED)

    def test_cancelled_job_must_record_a_reason(self) -> None:
        with pytest.raises(ValueError, match="cancel_reason"):
            self.job(status=JobStatus.CANCELLED)

    def test_retries_remaining_arithmetic(self) -> None:
        assert self.job(attempt=1, max_attempts=3).retries_remaining == 2
        assert self.job(attempt=3, max_attempts=3).retries_remaining == 0

    def test_attempt_cannot_exceed_budget(self) -> None:
        with pytest.raises(ValueError, match="exceeds max_attempts"):
            self.job(attempt=4, max_attempts=3)

    def test_resumability(self) -> None:
        assert self.job(status=JobStatus.TESTING).is_resumable is True
        assert self.job(status=JobStatus.COMPLETED).is_resumable is False
        assert (
            self.job(status=JobStatus.FAILED, failure_reason="x", attempt=3).is_resumable is False
        )
        assert self.job(status=JobStatus.FAILED, failure_reason="x", attempt=1).is_resumable is True

    def test_lease_expiry(self) -> None:
        now = datetime.now(UTC)
        assert self.job().lease_is_expired(now=now) is True
        assert (
            self.job(lease_expires_at=now + timedelta(minutes=5)).lease_is_expired(now=now) is False
        )
        assert (
            self.job(lease_expires_at=now - timedelta(minutes=5)).lease_is_expired(now=now) is True
        )

    def test_head_sha_accessor(self) -> None:
        assert self.job().head_sha == "b" * 40


class TestJobStage:
    def stage(self, **overrides: object) -> JobStage:
        base: dict[str, object] = {
            "id": new_id(IdKind.STAGE),
            "job_id": "job_1",
            "stage": JobStatus.TESTING,
        }
        base.update(overrides)
        return JobStage(**base)  # type: ignore[arg-type]

    def test_skipped_stage_requires_a_reason(self) -> None:
        """Spec section 41: a skip must be explained, never silent."""
        with pytest.raises(ValueError, match="skip_reason"):
            self.stage(outcome=JobStageOutcome.SKIPPED)

    def test_failed_stage_requires_an_error(self) -> None:
        with pytest.raises(ValueError, match="error"):
            self.stage(outcome=JobStageOutcome.FAILED)

    def test_resolved_stage_requires_a_completion_time(self) -> None:
        with pytest.raises(ValueError, match="completed_at"):
            self.stage(outcome=JobStageOutcome.SUCCEEDED)

    def test_skipped_is_resolved_but_not_succeeded(self) -> None:
        assert JobStageOutcome.SKIPPED.is_resolved is True
        assert JobStageOutcome.SUCCEEDED.is_resolved is True
        assert JobStageOutcome.FAILED.is_resolved is False


class TestApproval:
    def approval(self, **overrides: object) -> Approval:
        base: dict[str, object] = {
            "id": new_id(IdKind.APPROVAL),
            "job_id": "job_1",
            "commit_sha": "b" * 40,
            "decision": ApprovalDecision.APPROVE,
            "decided_by": "eng-lead",
        }
        base.update(overrides)
        return Approval(**base)  # type: ignore[arg-type]

    def test_scout_cannot_approve_its_own_work(self) -> None:
        """Spec section 46, enforced in the type system."""
        for actor in (ActorType.AGENT, ActorType.SYSTEM, ActorType.WEBHOOK, ActorType.WORKER):
            with pytest.raises(ValueError, match="HUMAN"):
                self.approval(decided_by_type=actor)

    def test_approval_applies_only_to_its_commit(self) -> None:
        approval = self.approval()
        assert approval.applies_to("b" * 40) is True
        assert approval.applies_to("c" * 40) is False

    def test_invalidation_preserves_the_record(self) -> None:
        """Spec section 74: an approval is invalidated, not deleted."""
        approval = self.approval().invalidate(reason="new commit pushed")
        assert approval.is_valid is False
        assert approval.invalidated_reason == "new commit pushed"
        assert approval.applies_to("b" * 40) is False
        assert approval.decision is ApprovalDecision.APPROVE

    def test_invalidation_fields_must_be_set_together(self) -> None:
        with pytest.raises(ValueError, match="must be set together"):
            self.approval(invalidated_reason="orphaned reason")


class TestQuestion:
    def test_answered_question_requires_an_answer(self) -> None:
        with pytest.raises(ValueError, match="at least one answer"):
            Question(
                id=new_id(IdKind.QUESTION),
                job_id="job_1",
                title="t",
                description="d",
                created_by="investigation",
                status=QuestionStatus.ANSWERED,
            )

    def test_open_question_cannot_carry_answers(self) -> None:
        """A question with answers must be closed; status and answers cannot disagree."""
        answer = QuestionAnswer(
            id=new_id(IdKind.ANSWER),
            question_id="qst_1",
            body="No.",
            answered_by="eng-lead",
        )
        with pytest.raises(ValueError, match="must not remain OPEN"):
            Question(
                id="qst_1",
                job_id="job_1",
                title="t",
                description="d",
                created_by="investigation",
                status=QuestionStatus.OPEN,
                answers=[answer],
            )

    def test_with_answer_closes_the_question(self) -> None:
        q = question()
        answer = QuestionAnswer(
            id=new_id(IdKind.ANSWER), question_id=q.id, body="No.", answered_by="eng-lead"
        )
        updated = q.with_answer(answer)
        assert updated.status is QuestionStatus.ANSWERED
        assert updated.accepted_answer is not None
        assert updated.accepted_answer.body == "No."
        assert updated.is_open is False

    def test_accepted_answer_must_reference_a_real_answer(self) -> None:
        with pytest.raises(ValueError, match="accepted_answer_id"):
            Question(
                id=new_id(IdKind.QUESTION),
                job_id="job_1",
                title="t",
                description="d",
                created_by="a",
                accepted_answer_id="ans_missing",
            )


class TestIdentifiers:
    def test_ulid_shape(self) -> None:
        value = new_ulid()
        assert len(value) == 26
        assert validate_ulid(value)
        assert value == value.upper()

    def test_ulids_are_lexicographically_sortable_by_time(self) -> None:
        earlier = new_ulid(timestamp_ms=1_000_000_000_000)
        later = new_ulid(timestamp_ms=1_000_000_001_000)
        assert earlier < later

    def test_ulids_within_one_millisecond_are_monotonic(self) -> None:
        """A tight loop must not produce duplicate or out-of-order identifiers."""
        values = [new_ulid(timestamp_ms=1_700_000_000_000) for _ in range(500)]
        assert len(set(values)) == len(values)
        assert values == sorted(values)

    def test_clock_going_backwards_stays_monotonic(self) -> None:
        forward = new_ulid(timestamp_ms=1_700_000_050_000)
        backwards = new_ulid(timestamp_ms=1_700_000_000_000)
        assert backwards > forward

    def test_prefixed_ids(self) -> None:
        job_id = new_id(IdKind.JOB)
        assert job_id.startswith("job_")
        assert validate_ulid(job_id.removeprefix("job_"))
        assert new_id(IdKind.TOOL_RUN).startswith("trun_")

    def test_out_of_range_timestamp_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="range"):
            new_ulid(timestamp_ms=1 << 60)

    def test_garbage_is_not_a_valid_ulid(self) -> None:
        assert validate_ulid("not-a-ulid") is False
        assert validate_ulid("0" * 25) is False
        assert validate_ulid("I" * 26) is False  # I is not in the Crockford alphabet
