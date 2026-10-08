"""Repository coverage for the remaining aggregates, plus the unit of work.

Each repository has a small set of behaviours the platform depends on, and this file
exercises exactly those rather than restating the schema:

* ``RepositoryRepository`` converges on what GitHub says instead of duplicating a
  repository on rename.
* ``QuestionRepository`` records an answer and closes the question in one unit of work, so
  the half-applied state (answer stored, question still ``OPEN``) cannot be observed.
* ``NotificationRepository.was_recently_sent`` suppresses repeats of a *delivered* fact but
  never suppresses a retry after a failure — the distinction that keeps an alerting system
  from going permanently silent after one SMTP hiccup.
* ``UnitOfWork`` commits once and rolls everything back on failure.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from scout_domain.enums import (
    NotificationChannel,
    QuestionStatus,
    Severity,
    ToolStatus,
)
from scout_domain.ids import IdKind, new_id
from scout_domain.models.base import utcnow
from scout_domain.models.notification import NotificationRecord, NotificationStatus
from scout_domain.models.question import QuestionAnswer
from scout_persistence.repositories.artifact import ArtifactRepository
from scout_persistence.repositories.decision import EngineeringDecisionRepository
from scout_persistence.repositories.finding import FindingRepository
from scout_persistence.repositories.model_request import ModelRequestRepository
from scout_persistence.repositories.notification import NotificationRepository
from scout_persistence.repositories.question import QuestionRepository
from scout_persistence.repositories.repository import RepositoryRepository
from scout_persistence.repositories.sandbox import (
    TERMINAL_STATUSES,
    SandboxRunRepository,
)
from scout_persistence.repositories.tool_run import ToolRunRepository
from scout_persistence.session import async_session_scope
from scout_persistence.unit_of_work import UnitOfWork

pytestmark = pytest.mark.integration


async def _job(session: AsyncSession, job_factory: object, **overrides: object) -> object:
    """Create a parent job; most aggregates reference one."""
    from scout_persistence.repositories.job import JobRepository

    job = job_factory(**overrides)  # type: ignore[operator]
    await JobRepository(session).create(job)
    return job


# ---------------------------------------------------------------------------
# repositories
# ---------------------------------------------------------------------------


async def test_repository_upsert_creates_then_updates(session: AsyncSession) -> None:
    """The first upsert inserts; the second converges on the current state."""
    repository = RepositoryRepository(session)
    created = await repository.upsert_by_full_name(
        "acme/widgets", github_id=991, default_branch="main", installation_id=7
    )
    assert created.id.startswith("repo_")
    assert created.is_active is True

    updated = await repository.upsert_by_full_name(
        "acme/widgets", github_id=991, default_branch="trunk"
    )
    assert updated.id == created.id, "an upsert must not create a second identity"
    assert updated.default_branch == "trunk"
    assert updated.installation_id == 7, "None means 'leave unchanged', not 'clear'"


async def test_repository_rename_keeps_one_identity(session: AsyncSession) -> None:
    """A rename is matched by ``github_id``, so history is not split across two rows.

    Without this, the old row would keep being treated as an active repository and jobs,
    findings, and audit events would reference a name that no longer exists.
    """
    repository = RepositoryRepository(session)
    original = await repository.upsert_by_full_name("acme/widgets", github_id=991)

    renamed = await repository.upsert_by_full_name("acme/gadgets", github_id=991)

    assert renamed.id == original.id
    assert renamed.full_name == "acme/gadgets"
    assert await repository.get_by_full_name("acme/widgets") is None
    assert await repository.list_all() != []


async def test_set_active_deactivates_without_deleting(session: AsyncSession) -> None:
    """Deactivation is a flag: jobs and findings referencing the repo stay readable."""
    repository = RepositoryRepository(session)
    await repository.upsert_by_full_name("acme/widgets")

    deactivated = await repository.set_active("acme/widgets", False)

    assert deactivated.is_active is False
    assert await repository.list_active() == []
    assert len(await repository.list_all()) == 1


async def test_set_active_on_an_unknown_repository_raises(session: AsyncSession) -> None:
    """Acting on a repository that does not exist is an error, not a silent no-op."""
    with pytest.raises(LookupError):
        await RepositoryRepository(session).set_active("acme/nope", True)


# ---------------------------------------------------------------------------
# questions
# ---------------------------------------------------------------------------


async def test_answer_records_the_answer_and_closes_the_question(
    session: AsyncSession, job_factory: object, question_factory: object
) -> None:
    """``answer`` is one unit of work: the answer row and the status move together.

    A half-applied answer — stored, but the question still ``OPEN`` — would either
    re-prompt a human who already answered or hide the answer from the report.
    """
    job = await _job(session, job_factory)
    repository = QuestionRepository(session)
    question = question_factory(job_id=job.id)  # type: ignore[operator]
    await repository.create(question)

    answer, updated = await repository.answer(
        question.id,
        "Yes, managers may see export metadata; the gateway enforces tenant scope.",
        answered_by="dana",
        promote_to_knowledge=True,
        knowledge_scope="acme/widgets",
    )

    assert answer.id.startswith("ans_")
    assert updated.status is QuestionStatus.ANSWERED
    assert updated.answers == [answer]
    assert updated.accepted_answer_id == answer.id
    assert updated.accepted_answer == answer


async def test_list_open_for_job_returns_only_open_questions(
    session: AsyncSession, job_factory: object, question_factory: object
) -> None:
    """Only open questions are surfaced; an answered one stops prompting."""
    job = await _job(session, job_factory)
    repository = QuestionRepository(session)
    open_question = question_factory(job_id=job.id)  # type: ignore[operator]
    answered = question_factory(job_id=job.id, title="Already settled")  # type: ignore[operator]
    await repository.create(open_question)
    await repository.create(answered)
    await repository.answer(answered.id, "Settled by the gateway.", answered_by="dana")

    open_rows = await repository.list_open_for_job(job.id)

    assert [row.id for row in open_rows] == [open_question.id]
    assert await repository.count_open_for_job(job.id) == 1


async def test_open_questions_are_ordered_most_severe_first(
    session: AsyncSession, job_factory: object, question_factory: object
) -> None:
    """Severity drives the order, using the domain ranking rather than text order.

    Alphabetically ``MEDIUM`` precedes ``INFO``, so a text sort would put an informational
    question ahead of a medium one.
    """
    job = await _job(session, job_factory)
    repository = QuestionRepository(session)
    for severity in (Severity.INFO, Severity.HIGH, Severity.LOW):
        await repository.create(  # type: ignore[operator]
            question_factory(job_id=job.id, severity=severity, title=f"{severity.value} question")
        )

    rows = await repository.list_open_for_job(job.id)

    assert [row.severity for row in rows] == [
        Severity.HIGH.value,
        Severity.LOW.value,
        Severity.INFO.value,
    ]


async def test_close_rejects_answered_and_open(session: AsyncSession) -> None:
    """``ANSWERED`` is only reachable by recording an answer.

    Letting a caller set it directly would claim a decision with no answer row behind it.
    """
    repository = QuestionRepository(session)
    with pytest.raises(ValueError, match=r"record_answer|answer"):
        await repository.close("qst_1", QuestionStatus.ANSWERED)
    with pytest.raises(ValueError, match="OPEN"):
        await repository.close("qst_1", QuestionStatus.OPEN)


async def test_close_dismisses_a_question(
    session: AsyncSession, job_factory: object, question_factory: object
) -> None:
    """A dismissed question is closed and no longer open."""
    job = await _job(session, job_factory)
    repository = QuestionRepository(session)
    question = question_factory(job_id=job.id)  # type: ignore[operator]
    await repository.create(question)

    closed = await repository.close(question.id, QuestionStatus.DISMISSED)

    assert closed.status == QuestionStatus.DISMISSED.value
    assert await repository.list_open_for_job(job.id) == []


async def test_get_reconstructs_the_full_aggregate(
    session: AsyncSession, job_factory: object, question_factory: object
) -> None:
    """``get`` loads the answers, so the report sees the real aggregate."""
    job = await _job(session, job_factory)
    repository = QuestionRepository(session)
    question = question_factory(job_id=job.id)  # type: ignore[operator]
    await repository.create(question)
    await repository.answer(question.id, "Because the gateway enforces it.", answered_by="dana")

    loaded = await repository.get(question.id)

    assert loaded is not None
    assert len(loaded.answers) == 1
    assert loaded.status is QuestionStatus.ANSWERED


async def test_record_answer_from_a_domain_model(
    session: AsyncSession, job_factory: object, question_factory: object
) -> None:
    """A pre-built answer can be persisted without re-closing the question."""
    job = await _job(session, job_factory)
    repository = QuestionRepository(session)
    question = question_factory(job_id=job.id)  # type: ignore[operator]
    await repository.create(question)

    answer = QuestionAnswer(
        id=new_id(IdKind.ANSWER),
        question_id=question.id,
        body="Promoted from an earlier answer.",
        answered_by="dana",
    )
    row = await repository.record_answer(answer)

    assert row.id == answer.id
    assert await repository.list_answers(question.id) == [row]


# ---------------------------------------------------------------------------
# findings
# ---------------------------------------------------------------------------


async def test_findings_are_ordered_by_severity_not_alphabetically(
    session: AsyncSession, job_factory: object, finding_factory: object
) -> None:
    """Worst first, using the domain's ranking.

    As text, ``CRITICAL`` < ``HIGH`` < ``INFO``, so a text sort would put informational
    findings above high ones in the middle of the list.
    """
    job = await _job(session, job_factory)
    repository = FindingRepository(session)
    for severity in (Severity.INFO, Severity.CRITICAL, Severity.MEDIUM):
        await repository.create(  # type: ignore[operator]
            finding_factory(job_id=job.id, severity=severity, title=f"{severity.value} finding")
        )

    rows = await repository.list_for_job(job.id)

    assert [row.severity for row in rows] == [
        Severity.CRITICAL.value,
        Severity.MEDIUM.value,
        Severity.INFO.value,
    ]


async def test_findings_filter_by_severity_and_category(
    session: AsyncSession, job_factory: object, finding_factory: object
) -> None:
    """The report's section queries filter in SQL rather than in Python."""
    from scout_domain.enums import FindingCategory

    job = await _job(session, job_factory)
    repository = FindingRepository(session)
    await repository.create(  # type: ignore[operator]
        finding_factory(job_id=job.id, severity=Severity.HIGH)
    )
    await repository.create(  # type: ignore[operator]
        finding_factory(
            job_id=job.id,
            severity=Severity.LOW,
            category=FindingCategory.DOCUMENTATION,
            title="Missing docstring",
        )
    )

    assert len(await repository.list_for_job(job.id, severity=Severity.HIGH)) == 1
    assert len(await repository.list_for_job(job.id, category=FindingCategory.DOCUMENTATION)) == 1
    assert await repository.list_blocking(job.id) != []


async def test_finding_counts_are_zero_filled(
    session: AsyncSession, job_factory: object, finding_factory: object
) -> None:
    """ "No CRITICAL findings" must be stated, not implied by an absent key."""
    job = await _job(session, job_factory)
    repository = FindingRepository(session)
    await repository.create(finding_factory(job_id=job.id, severity=Severity.LOW))  # type: ignore[operator]

    counts = await repository.count_by_severity(job.id)

    assert set(counts) == set(Severity)
    assert counts[Severity.LOW] == 1
    assert counts[Severity.CRITICAL] == 0


async def test_create_many_persists_evidence_as_locators(
    session: AsyncSession, job_factory: object, finding_factory: object
) -> None:
    """Evidence is stored as JSON references, never as raw output (Rule 15).

    The excerpt is bounded by the domain model; the bytes live in an artifact, and the
    reference is what the row keeps.
    """
    from scout_domain.enums import EvidenceKind
    from scout_domain.models.evidence import EvidenceReference

    job = await _job(session, job_factory)
    repository = FindingRepository(session)
    finding = finding_factory(  # type: ignore[operator]
        job_id=job.id,
        evidence=[
            EvidenceReference(
                kind=EvidenceKind.FILE_RANGE,
                reference_id="src/export.py",
                summary="No authorization check before the query.",
                file_path="src/export.py",
                line_start=48,
                line_end=61,
            )
        ],
    )

    rows = await repository.create_many([finding])

    assert rows[0].evidence[0]["kind"] == EvidenceKind.FILE_RANGE.value
    reloaded = await repository.get(rows[0].id)
    assert reloaded is not None
    assert reloaded.evidence[0]["file_path"] == "src/export.py"


# ---------------------------------------------------------------------------
# artifacts and tool runs
# ---------------------------------------------------------------------------


async def test_artifact_locator_round_trips(
    session: AsyncSession, job_factory: object, artifact_factory: object
) -> None:
    """An artifact row holds a locator: digest, size, key — and no bytes."""
    job = await _job(session, job_factory)
    repository = ArtifactRepository(session)
    artifact = artifact_factory(job_id=job.id)  # type: ignore[operator]

    row = await repository.record(artifact)

    assert row.sha256 == "c" * 64
    assert row.size_bytes == 4096
    assert row.storage_key.startswith("sha256/")
    assert not hasattr(row, "content")
    assert ArtifactRepository.to_domain(row) == artifact


async def test_artifact_lookup_by_digest(session: AsyncSession, artifact_factory: object) -> None:
    """Content addressing: the digest identifies the bytes, not the id."""
    repository = ArtifactRepository(session)
    await repository.record(artifact_factory(job_id=None))  # type: ignore[operator]

    found = await repository.get_by_sha256("c" * 64)

    assert found is not None


async def test_artifact_expiry_sweep_finds_old_rows(
    session: AsyncSession, job_factory: object, artifact_factory: object
) -> None:
    """Retention is enforced by querying ``expires_at``, not by deleting eagerly."""
    job = await _job(session, job_factory)
    repository = ArtifactRepository(session)
    await repository.record(artifact_factory(job_id=job.id))  # type: ignore[operator]

    future = utcnow() + timedelta(days=365 * 8)
    expired = await repository.list_expired(future)

    assert len(expired) == 1


async def test_tool_run_records_the_invocation(
    session: AsyncSession, job_factory: object, tool_result_factory: object
) -> None:
    """The recorded invocation answers "what did Scout actually execute?"."""
    from scout_domain.enums import Capability
    from scout_domain.models.tool import ToolInvocation

    job = await _job(session, job_factory)
    repository = ToolRunRepository(session)
    invocation = ToolInvocation(
        tool="pytest",
        capability=Capability.TEST_RUN,
        argv=["pytest", "-q", "--maxfail=1"],
        working_directory="/var/workspaces/job_1",
    )

    row = await repository.record_result(tool_result_factory(job_id=job.id), invocation=invocation)  # type: ignore[operator]

    assert row.capability == Capability.TEST_RUN.value
    assert row.invocation["argv"] == ["pytest", "-q", "--maxfail=1"]
    assert ToolRunRepository.to_domain(row).summary.values["passed"] == 183


async def test_tool_run_latest_prefers_the_retry(
    session: AsyncSession, job_factory: object, tool_result_factory: object
) -> None:
    """A retried tool reports its newest attempt, with earlier ones still available."""
    job = await _job(session, job_factory)
    repository = ToolRunRepository(session)
    started = utcnow()
    await repository.record(  # type: ignore[operator]
        tool_result_factory(
            job_id=job.id,
            status=ToolStatus.FAILED,
            exit_code=1,
            started_at=started,
            completed_at=started + timedelta(seconds=5),
            tool_run_id="trun_first",
        )
    )
    await repository.record(  # type: ignore[operator]
        tool_result_factory(
            job_id=job.id,
            status=ToolStatus.PASSED,
            exit_code=0,
            started_at=started + timedelta(minutes=1),
            completed_at=started + timedelta(minutes=1, seconds=5),
            tool_run_id="trun_second",
        )
    )

    latest = await repository.latest_for_tool(job.id, "pytest")

    assert latest is not None
    assert latest.id == "trun_second"
    assert len(await repository.list_for_job(job.id)) == 2


# ---------------------------------------------------------------------------
# notifications
# ---------------------------------------------------------------------------


async def test_was_recently_sent_only_counts_delivered(
    session: AsyncSession,
) -> None:
    """A ``PENDING`` or ``FAILED`` notification does not suppress a fresh attempt.

    This is the distinction that keeps an alerting system from going permanently silent
    after one transient failure.
    """
    repository = NotificationRepository(session)
    pending = await repository.enqueue(
        channel=NotificationChannel.EMAIL,
        subject="S",
        body="B",
        dedup_key="question:qst_1:severity",
        severity=Severity.HIGH,
    )

    assert (
        await repository.was_recently_sent(
            "question:qst_1:severity", NotificationChannel.EMAIL, timedelta(minutes=30)
        )
        is False
    )

    await repository.mark_failed(pending.id, error="smtp timeout")
    assert (
        await repository.was_recently_sent(
            "question:qst_1:severity", NotificationChannel.EMAIL, timedelta(minutes=30)
        )
        is False
    )

    await repository.mark_sent(pending.id)
    assert (
        await repository.was_recently_sent(
            "question:qst_1:severity", NotificationChannel.EMAIL, timedelta(minutes=30)
        )
        is True
    )


async def test_was_recently_sent_respects_the_window(session: AsyncSession) -> None:
    """A delivery older than the window does not suppress a new one."""
    repository = NotificationRepository(session)
    row = await repository.enqueue(
        channel=NotificationChannel.GOOGLE_CHAT,
        subject="S",
        body="B",
        dedup_key="job:job_1:failed",
        severity=Severity.HIGH,
    )
    await repository.mark_sent(row.id, sent_at=utcnow() - timedelta(hours=2))

    assert (
        await repository.was_recently_sent(
            "job:job_1:failed", NotificationChannel.GOOGLE_CHAT, timedelta(minutes=30)
        )
        is False
    )
    assert (
        await repository.was_recently_sent(
            "job:job_1:failed", NotificationChannel.GOOGLE_CHAT, timedelta(hours=3)
        )
        is True
    )


async def test_was_recently_sent_is_channel_scoped(session: AsyncSession) -> None:
    """Delivering by email does not suppress the chat message."""
    repository = NotificationRepository(session)
    row = await repository.enqueue(
        channel=NotificationChannel.EMAIL,
        subject="S",
        body="B",
        dedup_key="shared-key",
        severity=Severity.HIGH,
    )
    await repository.mark_sent(row.id)

    assert (
        await repository.was_recently_sent(
            "shared-key", NotificationChannel.GOOGLE_CHAT, timedelta(minutes=30)
        )
        is False
    )


async def test_dedup_key_channel_is_unique(session: AsyncSession) -> None:
    """The constraint makes a duplicate message impossible, not merely unlikely."""
    from sqlalchemy.exc import IntegrityError

    repository = NotificationRepository(session)
    await repository.enqueue(
        channel=NotificationChannel.EMAIL, subject="S", body="B", dedup_key="k"
    )
    with pytest.raises(IntegrityError):
        await repository.enqueue(
            channel=NotificationChannel.EMAIL, subject="S2", body="B2", dedup_key="k"
        )
    await session.rollback()


async def test_suppress_records_why(session: AsyncSession) -> None:
    """A suppressed alert with no reason is indistinguishable from a dropped one."""
    repository = NotificationRepository(session)
    row = await repository.enqueue(
        channel=NotificationChannel.EMAIL, subject="S", body="B", dedup_key="k2"
    )
    suppressed = await repository.suppress(row.id, reason="below the severity threshold")
    assert suppressed.status == NotificationStatus.SUPPRESSED.value
    assert suppressed.error == "below the severity threshold"


async def test_notification_domain_model_rejects_sent_without_a_timestamp() -> None:
    """The domain refuses a ``SENT`` record with no delivery time (Rule 12 in spirit).

    A notification claiming delivery with no timestamp cannot be used to debug a delivery
    problem, which is the only reason the record exists.
    """
    with pytest.raises(ValueError, match="sent_at"):
        NotificationRecord(
            id="ntf_1",
            channel=NotificationChannel.EMAIL,
            subject="S",
            body="B",
            dedup_key="k",
            status=NotificationStatus.SENT,
        )


# ---------------------------------------------------------------------------
# model requests, sandbox runs, decisions
# ---------------------------------------------------------------------------


async def test_model_request_totals(session: AsyncSession, job_factory: object) -> None:
    """Cost accounting answers "what did this job cost?" from the rows."""
    from scout_domain.models.model_request import ModelRequestRecord, ModelRequestStatus

    job = await _job(session, job_factory)
    repository = ModelRequestRepository(session)
    for index in range(3):
        await repository.record(
            ModelRequestRecord(
                id=f"mreq_{index}",
                job_id=job.id,
                provider="openai",
                model="gpt-5",
                prompt_version="v1",
                input_tokens=1000,
                output_tokens=200,
                latency_seconds=1.5,
                status=ModelRequestStatus.SUCCEEDED,
                estimated_cost_usd=0.01,
            )
        )

    totals = await repository.totals_for_job(job.id)

    assert totals["input_tokens"] == 3000.0
    assert totals["output_tokens"] == 600.0
    assert totals["total_tokens"] == 3600.0
    assert totals["estimated_cost_usd"] == pytest.approx(0.03)
    assert await repository.job_cost_usd(job.id) == pytest.approx(0.03)


async def test_sandbox_run_lifecycle(
    session: AsyncSession, job_factory: object, sandbox_factory: object
) -> None:
    """A sandbox run records its policy snapshot and closes with an outcome."""
    from scout_domain.models.sandbox import SandboxStatus

    job = await _job(session, job_factory)
    repository = SandboxRunRepository(session)
    run = sandbox_factory(job_id=job.id, status=SandboxStatus.PENDING)  # type: ignore[operator]
    created = await repository.create(run)

    running = await repository.mark_running(
        created.id, native_id="container-abc", workspace_path="/var/workspaces/job_1"
    )
    assert running.status == SandboxStatus.RUNNING.value
    assert running.native_id == "container-abc"
    assert running.policy["network_mode"] == "none", "the policy snapshot is stored"

    completed = await repository.complete(created.id, status=SandboxStatus.SUCCEEDED)
    assert completed.status == SandboxStatus.SUCCEEDED.value
    assert completed.completed_at is not None
    assert completed.duration_seconds is not None


async def test_sandbox_complete_rejects_a_non_terminal_status(
    session: AsyncSession, job_factory: object, sandbox_factory: object
) -> None:
    """Completing into ``PENDING`` is refused: the row would never be reaped."""
    from scout_domain.models.sandbox import SandboxStatus

    job = await _job(session, job_factory)
    repository = SandboxRunRepository(session)
    created = await repository.create(sandbox_factory(job_id=job.id))  # type: ignore[operator]

    with pytest.raises(ValueError, match=r"not a terminal sandbox status"):
        await repository.complete(created.id, status=SandboxStatus.PENDING)


def test_sandbox_terminal_statuses_match_domain() -> None:
    """The locally listed terminal statuses agree with ``SandboxRun.is_terminal``."""
    from scout_domain.enums import SandboxBackendKind
    from scout_domain.models.sandbox import (
        DEFAULT_RESOURCE_LIMITS,
        SandboxPolicy,
        SandboxRun,
        SandboxStatus,
    )

    for status in TERMINAL_STATUSES:
        run = SandboxRun(
            id="srun_1",
            job_id="job_1",
            backend=SandboxBackendKind.LOCAL,
            limits=DEFAULT_RESOURCE_LIMITS,
            policy=SandboxPolicy(),
            status=status,
            # A terminal sandbox run must record when it finished, and an ERROR run must
            # also say what went wrong; both are domain rules, not database ones.
            completed_at=utcnow(),
            error="sandbox backend unavailable" if status is SandboxStatus.ERROR else None,
        )
        assert run.is_terminal is True, f"{status.value} should be terminal"

    non_terminal = [status for status in SandboxStatus if status not in TERMINAL_STATUSES]
    assert non_terminal, "at least one status must be non-terminal"


async def test_sandbox_append_tool_run_is_ordered_and_deduplicated(
    session: AsyncSession, job_factory: object, sandbox_factory: object
) -> None:
    """The sandbox's command list preserves execution order and never repeats an id."""
    job = await _job(session, job_factory)
    repository = SandboxRunRepository(session)
    created = await repository.create(sandbox_factory(job_id=job.id))  # type: ignore[operator]

    await repository.append_tool_run(created.id, "trun_1")
    await repository.append_tool_run(created.id, "trun_2")
    updated = await repository.append_tool_run(created.id, "trun_1")

    assert updated.tool_run_ids == ["trun_1", "trun_2"]


async def test_engineering_decision_round_trips(session: AsyncSession) -> None:
    """A decision keeps its provenance, so "why did Scout stop flagging this?" is answerable."""
    from scout_domain.models.knowledge import EngineeringDecision

    repository = EngineeringDecisionRepository(session)
    decision = EngineeringDecision(
        id="dec_01J8ZQ4K2M7X3N9PQRSTVWXY0",
        statement="The reporting service intentionally bypasses user authorization.",
        rationale="Access is enforced at the gateway.",
        decided_by="dana",
        scope="acme/widgets",
        source_question_id="qst_1",
        source_answer_id="ans_1",
        source_job_id="job_1",
    )

    row = await repository.create(decision)

    assert EngineeringDecisionRepository.to_domain(row) == decision
    assert [item.id for item in await repository.list_active_for_scope("acme/widgets")] == [
        decision.id
    ]


async def test_decision_supersession_is_bidirectional(session: AsyncSession) -> None:
    """A reversal is recorded, not overwritten, and the chain walks both ways."""
    from scout_domain.models.knowledge import EngineeringDecision

    repository = EngineeringDecisionRepository(session)
    original = EngineeringDecision(
        id="dec_old", statement="Bypass authorization.", decided_by="dana"
    )
    replacement = EngineeringDecision(
        id="dec_new", statement="Enforce authorization.", decided_by="dana"
    )
    await repository.create(original)
    await repository.create(replacement)

    await repository.supersede(original.id, superseded_by_id=replacement.id)

    old = await repository.get(original.id)
    new = await repository.get(replacement.id)
    assert old is not None
    assert old.superseded_by_id == "dec_new"
    assert new is not None
    assert new.supersedes_id == "dec_old"
    assert [row.id for row in await repository.list_active_for_scope("*")] == ["dec_new"]


async def test_linking_a_knowledge_document_is_idempotent(session: AsyncSession) -> None:
    """The same document is not listed twice on a decision."""
    from scout_domain.models.knowledge import EngineeringDecision

    repository = EngineeringDecisionRepository(session)
    await repository.create(EngineeringDecision(id="dec_1", statement="S", decided_by="dana"))

    await repository.link_knowledge_document("dec_1", "kdoc_1")
    row = await repository.link_knowledge_document("dec_1", "kdoc_1")

    assert row.knowledge_document_ids == ["kdoc_1"]


# ---------------------------------------------------------------------------
# unit of work
# ---------------------------------------------------------------------------


async def test_unit_of_work_commits_once(
    session_factory: async_sessionmaker[AsyncSession],
    verification_session: AsyncSession,
    job_factory: object,
    approval_factory: object,
) -> None:
    """Writes across several repositories land together on one commit."""
    job = job_factory()  # type: ignore[operator]

    async with UnitOfWork(session_factory) as uow:
        await uow.jobs.create(job)
        await uow.approvals.record(  # type: ignore[operator]
            approval_factory(job_id=job.id, commit_sha=job.commits.head_sha)
        )
        await uow.audit_events.record(
            actor="dana",
            actor_type="HUMAN",
            action="approval.record",
            resource="verification_job",
            resource_id=job.id,
            job_id=job.id,
        )

    from scout_persistence.repositories.approval import ApprovalRepository
    from scout_persistence.repositories.job import JobRepository

    assert await JobRepository(verification_session).get(job.id) is not None
    assert len(await ApprovalRepository(verification_session).list_for_job(job.id)) == 1


async def test_unit_of_work_rolls_back_on_failure(
    session_factory: async_sessionmaker[AsyncSession],
    verification_session: AsyncSession,
    job_factory: object,
) -> None:
    """A failure inside the block leaves nothing behind.

    This is the property that makes multi-aggregate writes safe: a job is either created
    with its audit event or not created at all.
    """
    from scout_persistence.repositories.job import JobRepository

    job = job_factory()  # type: ignore[operator]

    # The exception must escape the unit of work *after* the rollback (Rule 12): swallowing
    # it here would turn a failed verification write into a job that appears to have
    # succeeded. ``pytest.raises`` therefore wraps the single awaitable that performs the
    # write and the raise.
    with pytest.raises(RuntimeError, match="boom"):
        await _write_job_then_fail(session_factory, job)

    assert await JobRepository(verification_session).get(job.id) is None


async def test_unit_of_work_exposes_every_repository(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Every repository is an attribute, bound to one session."""
    async with UnitOfWork(session_factory) as uow:
        attributes = (
            uow.repositories,
            uow.webhook_events,
            uow.jobs,
            uow.stages,
            uow.findings,
            uow.questions,
            uow.approvals,
            uow.artifacts,
            uow.tool_runs,
            uow.agent_runs,
            uow.model_requests,
            uow.notifications,
            uow.audit_events,
            uow.knowledge,
            uow.decisions,
            uow.sandbox_runs,
        )
        assert all(attribute.session is uow.session for attribute in attributes)


async def test_unit_of_work_cannot_be_reentered(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A ``UnitOfWork`` is single-use, so a session cannot outlive its block unnoticed."""
    uow = UnitOfWork(session_factory)
    async with uow:
        pass
    with pytest.raises(RuntimeError, match="already been entered"):
        async with uow:
            pass


async def test_session_scope_commits_on_success(
    session_factory: async_sessionmaker[AsyncSession],
    verification_session: AsyncSession,
    job_factory: object,
) -> None:
    """``async_session_scope`` commits what the block wrote."""
    from scout_persistence.repositories.job import JobRepository

    job = job_factory()  # type: ignore[operator]

    async with async_session_scope(session_factory) as session:
        await JobRepository(session).create(job)

    assert await JobRepository(verification_session).get(job.id) is not None


async def test_session_scope_rolls_back_and_propagates(
    session_factory: async_sessionmaker[AsyncSession],
    verification_session: AsyncSession,
    job_factory: object,
) -> None:
    """The exception is re-raised after the rollback (Rule 12).

    Swallowing it would turn a failed verification write into a job that appears to have
    succeeded.
    """
    from scout_persistence.repositories.job import JobRepository

    job = job_factory()  # type: ignore[operator]

    with pytest.raises(ValueError, match="nope"):
        await _write_job_then_fail_in_scope(session_factory, job)

    assert await JobRepository(verification_session).get(job.id) is None


async def test_check_database_succeeds_against_a_live_server(engine: object) -> None:
    """The readiness probe returns normally when PostgreSQL answers."""
    from scout_persistence.session import check_database

    await check_database(engine)  # type: ignore[arg-type]


async def _write_job_then_fail(
    session_factory: async_sessionmaker[AsyncSession],
    job: object,
) -> None:
    """Write a job inside a unit of work and then raise, to prove the rollback.

    Extracted so the call site can wrap a single awaitable in ``pytest.raises`` and keep the
    assertion about *what was rolled back* next to the expectation.
    """
    async with UnitOfWork(session_factory) as uow:
        await uow.jobs.create(job)  # type: ignore[arg-type]
        raise RuntimeError("boom")


async def _write_job_then_fail_in_scope(
    session_factory: async_sessionmaker[AsyncSession],
    job: object,
) -> None:
    """Same as :func:`_write_job_then_fail`, for ``async_session_scope``."""
    async with async_session_scope(session_factory) as session:
        from scout_persistence.repositories.job import JobRepository

        await JobRepository(session).create(job)  # type: ignore[arg-type]
        raise ValueError("nope")
