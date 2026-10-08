"""The first vertical slice (spec section 83).

    GitHub PR -> webhook -> create job -> checkout -> run tests -> store results
              -> generate report -> GitHub check

This is the test that answers "does Scout actually work?" rather than "do its parts work?". It
drives the real control plane -- the real :class:`~scout_orchestrator.orchestrator.Orchestrator`,
the real stage implementations, the real local sandbox, the real repository schema, and the real
Redis Streams transport -- over a real git repository and a real PostgreSQL.

Three properties make it honest rather than a demonstration:

* the **repository is real**, built by :mod:`support` with two commits and a bare remote that
  accepts a fetch by SHA, so the checkout path Scout uses in production is the one under test;
* the **job is created from a webhook payload** parsed by ``scout_github``, not inserted by hand, so
  the SHA the report names is the SHA GitHub told Scout about;
* the **tests are the repository's own**, executed by ``pytest`` inside a sandbox, and the artifact
  bytes are read back out of the store to prove the evidence is really there.

Scope note: section 83 does **not** include acceptance-criteria verification. That is section
85's slice, and the feature-verification agent that would derive a procedure per criterion is not
wired yet, so this slice switches the stage off by configuration -- recorded as a skip, with a
reason, and listed in the report as a limitation. The third test re-enables it to prove the gate
that makes ``REQUIRES_HUMAN_REVIEW`` unavoidable is intact.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
import support

from scout_domain.enums import JobStatus, Recommendation, ToolStatus
from scout_domain.models.artifact import ArtifactKind
from scout_domain.models.job import CommitRange, JobStageOutcome, VerificationJob
from scout_domain.models.report import VerificationReport
from scout_github import (
    PULL_REQUEST_EVENT_TYPE,
    CheckRunConclusion,
    FakeGitHubClient,
    parse_pull_request_event,
)
from scout_orchestrator.orchestrator import Orchestrator
from scout_orchestrator.settings import Settings
from scout_persistence.mapping import to_artifact
from scout_persistence.queue import JobMessage, RedisJobQueue
from scout_persistence.unit_of_work import UnitOfWork
from scout_tools.artifacts import LocalArtifactStore

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from scout_domain.models.artifact import Artifact
    from scout_persistence.tables import JobStageRow

pytestmark = pytest.mark.e2e

HEAD_SHA_LENGTH = 40


@dataclass
class Slice:
    """Everything the slice produced, for the assertions to interrogate."""

    job_id: str
    head_sha: str
    stages: dict[str, list[JobStageRow]] = field(default_factory=dict)
    tools: dict[str, object] = field(default_factory=dict)
    artifacts: list[Artifact] = field(default_factory=list)
    report: VerificationReport | None = None
    github: FakeGitHubClient | None = None
    job_status: str = ""
    audit_actions: list[str] = field(default_factory=list)

    def stage_outcomes(self, stage: JobStatus) -> list[str]:
        """The recorded outcomes for ``stage``, in order."""
        return [str(row.outcome) for row in self.stages.get(stage.value, [])]

    def stage_skip_reason(self, stage: JobStatus) -> str | None:
        """The recorded skip reason for ``stage``, or ``None``."""
        rows = self.stages.get(stage.value, [])
        return None if not rows else rows[0].skip_reason

    def artifact_of_kind(self, kind: ArtifactKind) -> Artifact:
        """The artifact of ``kind``, asserting exactly one was recorded."""
        matches = [artifact for artifact in self.artifacts if artifact.kind is kind]
        assert len(matches) == 1, f"expected exactly one {kind.value} artifact, got {len(matches)}"
        return matches[0]


def _webhook_payload(fixture: support.GitFixture) -> dict[str, object]:
    """Build a GitHub ``pull_request`` delivery for the fixture's real SHAs.

    Shaped like the real payload rather than like Scout's model, so the parser is genuinely
    exercised: a parser that only accepts its own output proves nothing.
    """
    return {
        "action": "opened",
        "installation": {"id": 4242},
        "repository": {
            "id": 99,
            "full_name": fixture.full_name,
            "name": fixture.full_name.split("/", 1)[1],
            "owner": {"login": fixture.full_name.split("/", 1)[0]},
            "default_branch": "main",
            "private": False,
            "html_url": f"https://github.com/{fixture.full_name}",
        },
        "pull_request": {
            "number": fixture.pull_request_number,
            "title": "Add a multiply() helper",
            "body": fixture.pr_body,
            "state": "open",
            "draft": False,
            "merged": False,
            "html_url": (
                f"https://github.com/{fixture.full_name}/pull/{fixture.pull_request_number}"
            ),
            "user": {"login": "octocat"},
            "base": {"ref": "main", "sha": fixture.base_sha},
            "head": {"ref": "scout/multiply", "sha": fixture.head_sha},
        },
    }


async def _create_job_from_webhook(
    session_factory: async_sessionmaker[AsyncSession],
    fixture: support.GitFixture,
    *,
    delivery_id: str,
) -> str:
    """Parse a delivery, create the job idempotently, and return its id.

    ``create_unless_exists`` is used rather than ``create`` because that is what the webhook path
    does, and the unique ``(repository, pull request, head SHA)`` constraint is the arbiter under a
    concurrent redelivery.
    """
    event = parse_pull_request_event(
        _webhook_payload(fixture),
        event_type=PULL_REQUEST_EVENT_TYPE,
        action="opened",
    ).model_copy(update={"delivery_id": delivery_id})

    job = VerificationJob(
        id="job_e2e_vertical_slice",
        repository=event.repository,
        repository_full_name=fixture.full_name,
        pull_request_number=fixture.pull_request_number,
        pull_request_title=event.title,
        pull_request_author=event.author,
        pull_request_url=event.html_url,
        commits=CommitRange(base_sha=event.base_sha, head_sha=event.head_sha),
        github_delivery_id=event.delivery_id,
        status=JobStatus.QUEUED,
        correlation_id="corr-e2e",
    )

    async with UnitOfWork(session_factory) as uow:
        row, created = await uow.jobs.create_unless_exists(job)
        assert created is True, "the delivery should create exactly one job"
        return row.id


async def _run_slice(
    *,
    fixture: support.GitFixture,
    session_factory: async_sessionmaker[AsyncSession],
    artifact_root: Path,
    workspace_root: Path,
    redis_server_url: str,
    stream_suffix: str,
) -> Slice:
    """Drive the whole slice for one repository fixture."""
    delivery_id = f"delivery-{stream_suffix}"
    job_id = await _create_job_from_webhook(session_factory, fixture, delivery_id=delivery_id)

    settings = Settings(
        env="test",
        log_level="WARNING",
        database_url="postgresql+asyncpg://scout:scout@localhost:5433/scout",
        redis_url=redis_server_url,
        artifact_root=artifact_root,
        workspace_root=workspace_root,
        sandbox_backend="local",
        sandbox_network="none",
        notifications_enabled=False,
        execution_mode="in_process",
        # The remote is a local path, which is what makes the slice offline: the checkout path is
        # the production one, only the transport differs.
        git_base_url=f"file://{fixture.root / 'remotes'}",
        dashboard_url="http://dashboard.test",
        lease_ttl_seconds=120,
        worker_poll_interval_seconds=1.0,
    )

    github = FakeGitHubClient()
    support.register_pull_request(github, fixture)

    stream = f"scout:e2e:{stream_suffix}"
    group = f"orchestrator-{stream_suffix}"
    queue = RedisJobQueue(redis_server_url, stream=stream, group=group)
    artifacts = LocalArtifactStore(artifact_root)

    orchestrator = Orchestrator(
        settings=settings,
        session_factory=session_factory,
        artifacts=artifacts,
        github=github,
        queue=queue,
        consumer=f"orchestrator:e2e:{stream_suffix}",
    )

    # The webhook path enqueues; this is that enqueue, through the real transport.
    await queue.enqueue(
        JobMessage(
            job_id=job_id,
            repository_full_name=fixture.full_name,
            head_sha=fixture.head_sha,
            attempt=1,
        )
    )

    try:
        worked = await orchestrator.run_once(block_ms=1)
        assert worked is True, "the orchestrator should have reserved the queued job"
    finally:
        await queue.aclose()

    return await _collect(
        session_factory=session_factory,
        store=artifacts,
        github=github,
        job_id=job_id,
        head_sha=fixture.head_sha,
    )


async def _collect(
    *,
    session_factory: async_sessionmaker[AsyncSession],
    store: LocalArtifactStore,
    github: FakeGitHubClient,
    job_id: str,
    head_sha: str,
) -> Slice:
    """Read everything the run produced back out of the database and the artifact store."""
    slice_result = Slice(job_id=job_id, head_sha=head_sha, github=github)

    async with UnitOfWork(session_factory) as uow:
        job_row = await uow.jobs.get(job_id)
        assert job_row is not None
        slice_result.job_status = str(job_row.status)

        for stage_row in await uow.stages.list_for_job(job_id):
            slice_result.stages.setdefault(str(stage_row.stage), []).append(stage_row)

        for tool_row in await uow.tool_runs.list_for_job(job_id):
            slice_result.tools[str(tool_row.tool)] = tool_row

        slice_result.artifacts = [
            to_artifact(row) for row in await uow.artifacts.list_for_job(job_id)
        ]
        slice_result.audit_actions = [
            str(event.action) for event in await uow.audit_events.list_for_job(job_id)
        ]

    report_artifact = slice_result.artifact_of_kind(ArtifactKind.VERIFICATION_REPORT)
    payload = json.loads(store.get_bytes(report_artifact).decode("utf-8"))
    slice_result.report = VerificationReport.model_validate(payload)
    return slice_result


# --------------------------------------------------------------------------------------
# Case 1: the change works
# --------------------------------------------------------------------------------------


async def test_vertical_slice_passing_change(
    passing_repo: support.GitFixture,
    session_factory: async_sessionmaker[AsyncSession],
    artifact_root: Path,
    workspace_root: Path,
    redis_server_url: str,
) -> None:
    """A covered, passing change reaches PASS and a successful GitHub check."""
    result = await _run_slice(
        fixture=passing_repo,
        session_factory=session_factory,
        artifact_root=artifact_root,
        workspace_root=workspace_root,
        redis_server_url=redis_server_url,
        stream_suffix="passing",
    )

    # 1. A job row exists with the exact head SHA GitHub reported.
    assert result.head_sha == passing_repo.head_sha
    assert len(result.head_sha) == HEAD_SHA_LENGTH

    # 2. PREPARING and TESTING succeeded. Both are recorded rows, not in-memory assertions.
    assert result.stage_outcomes(JobStatus.PREPARING) == [JobStageOutcome.SUCCEEDED.value]
    assert result.stage_outcomes(JobStatus.TESTING) == [JobStageOutcome.SUCCEEDED.value]
    assert result.stage_outcomes(JobStatus.INVESTIGATING) == [JobStageOutcome.SUCCEEDED.value]

    # The two checks this slice does not cover are skipped *with a recorded reason*: an
    # unexplained omission is exactly what spec section 41 forbids.
    assert result.stage_outcomes(JobStatus.SECURITY_SCANNING) == [JobStageOutcome.SKIPPED.value]
    assert "engineering.yaml" in (result.stage_skip_reason(JobStatus.SECURITY_SCANNING) or "")
    assert result.stage_outcomes(JobStatus.FEATURE_VERIFYING) == [JobStageOutcome.SKIPPED.value]
    assert "engineering.yaml" in (result.stage_skip_reason(JobStatus.FEATURE_VERIFYING) or "")

    # 3. The test runner really ran and reported a pass.
    pytest_row = result.tools["pytest"]
    assert pytest_row.status == ToolStatus.PASSED.value  # type: ignore[attr-defined]
    assert pytest_row.exit_code == 0  # type: ignore[attr-defined]
    assert pytest_row.stdout_artifact_id  # type: ignore[attr-defined]
    assert pytest_row.report_artifact_id  # type: ignore[attr-defined]

    # 4. The artifact bytes are readable from the store and contain pytest's own output.
    store = LocalArtifactStore(artifact_root)
    stdout_text = store.get_bytes(result.artifact_of_kind(ArtifactKind.TOOL_STDOUT)).decode(
        "utf-8", errors="replace"
    )
    assert "passed" in stdout_text
    # The pinned diff is evidence too, and it names the files the report talks about.
    assert b"widget.py" in store.get_bytes(result.artifact_of_kind(ArtifactKind.DIFF))

    # 5. The report was produced from the evidence, validated, and recommends PASS.
    assert result.report is not None
    report = result.report
    assert report.recommendation is Recommendation.PASS
    assert report.commit_sha == passing_repo.head_sha
    assert report.base_sha == passing_repo.base_sha
    assert report.tests.passed >= 2
    assert report.tests.failed == 0
    assert report.tests.change_is_covered is True
    assert report.unverified_areas == []
    assert report.open_questions == []
    assert report.all_findings == []
    # No criterion was checked, so none is claimed -- and the opt-out is stated, not hidden.
    assert report.feature_verification == []
    assert any("feature verification is disabled" in note for note in report.known_limitations)

    # 6. The GitHub check was created with the conclusion the recommendation implies.
    assert result.github is not None
    assert len(result.github.check_runs) == 1
    check_run = result.github.check_runs[0]
    assert check_run.conclusion == CheckRunConclusion.SUCCESS.value
    assert check_run.head_sha == passing_repo.head_sha
    assert "PASS" in (check_run.output.summary or "")

    # The lifecycle ended where the pipeline says it should, and the ledger records why.
    assert result.job_status == JobStatus.READY_FOR_REVIEW.value
    assert "github.check_run.created" in result.audit_actions
    assert "report.generated" in result.audit_actions
    assert "job.stage_skipped" in result.audit_actions


# --------------------------------------------------------------------------------------
# Case 2: the change is broken
# --------------------------------------------------------------------------------------


async def test_vertical_slice_failing_change(
    failing_repo: support.GitFixture,
    session_factory: async_sessionmaker[AsyncSession],
    artifact_root: Path,
    workspace_root: Path,
    redis_server_url: str,
) -> None:
    """A change whose own suite fails reaches FAIL and a failing GitHub check.

    This is the case that proves the slice is real: the same code path, the same stages, and the
    same derivation, with a repository that behaves differently -- and a different verdict.
    """
    result = await _run_slice(
        fixture=failing_repo,
        session_factory=session_factory,
        artifact_root=artifact_root,
        workspace_root=workspace_root,
        redis_server_url=redis_server_url,
        stream_suffix="failing",
    )

    assert result.stage_outcomes(JobStatus.PREPARING) == [JobStageOutcome.SUCCEEDED.value]
    assert result.stage_outcomes(JobStatus.TESTING) == [JobStageOutcome.SUCCEEDED.value]

    pytest_row = result.tools["pytest"]
    assert pytest_row.status == ToolStatus.FAILED.value  # type: ignore[attr-defined]
    assert pytest_row.exit_code == 1  # type: ignore[attr-defined]

    store = LocalArtifactStore(artifact_root)
    stdout = store.get_bytes(result.artifact_of_kind(ArtifactKind.TOOL_STDOUT))
    assert b"failed" in stdout

    assert result.report is not None
    report = result.report
    assert report.recommendation in {Recommendation.FAIL, Recommendation.REQUEST_CHANGES}
    assert report.tests.failed >= 1, "the failing test must be counted, not smoothed over"
    # The finding about the failure is machine-backed, so it is a fact rather than a judgement.
    assert any(finding.category.value == "TESTS" for finding in report.all_findings)
    assert any(finding.is_machine_verified for finding in report.all_findings)
    # Spec section 67: the change touched source without touching a test, and the report says so.
    assert report.tests.change_is_covered is False
    assert report.tests.uncovered_changed_paths == ["widget.py"]

    assert result.github is not None
    check_run = result.github.check_runs[0]
    assert check_run.conclusion == CheckRunConclusion.FAILURE.value


# --------------------------------------------------------------------------------------
# Case 3: the acceptance-criteria gate is real, not bypassed
# --------------------------------------------------------------------------------------


async def test_vertical_slice_prose_criteria_require_human_review(
    verifying_repo: support.GitFixture,
    session_factory: async_sessionmaker[AsyncSession],
    artifact_root: Path,
    workspace_root: Path,
    redis_server_url: str,
) -> None:
    """A clean change whose criteria exist only as prose cannot reach PASS.

    This is the test that would catch anyone weakening the gate. The repository is the *passing*
    one with feature verification switched on, so the only thing between a clean run and ``PASS``
    is that the pull-request description states requirements Scout has no procedure for. The
    correct answer is ``REQUIRES_HUMAN_REVIEW`` with a ``neutral`` check -- never ``success``,
    which would let an unverified change look verified (spec sections 23, 25, and 2).
    """
    result = await _run_slice(
        fixture=verifying_repo,
        session_factory=session_factory,
        artifact_root=artifact_root,
        workspace_root=workspace_root,
        redis_server_url=redis_server_url,
        stream_suffix="verifying",
    )

    # The stage ran, because the repository asked for it.
    assert result.stage_outcomes(JobStatus.FEATURE_VERIFYING) == [JobStageOutcome.SUCCEEDED.value]
    # The suite still passed: this is not a failing-change test.
    assert result.tools["pytest"].status == ToolStatus.PASSED.value  # type: ignore[attr-defined]

    assert result.report is not None
    report = result.report

    # Criteria were extracted from the pull-request description, and none of them passed.
    assert report.feature_verification, "the PR body states requirements"
    assert all(
        criterion.status.value == "not_verified" for criterion in report.feature_verification
    )
    assert not any(criterion.status.value == "passed" for criterion in report.feature_verification)
    # A passed criterion must be impossible without evidence; the validator is what enforces it.
    assert all(criterion.evidence == [] for criterion in report.feature_verification)

    # The unverified criteria become an explicit unverified area, which bars PASS.
    assert report.unverified_areas
    assert any("criterion" in area for area in report.unverified_areas)
    assert report.recommendation is Recommendation.REQUIRES_HUMAN_REVIEW

    # The overlapping test is recorded as a *lead*, not as a verification.
    assert any(
        "overlaps this criterion" in criterion.notes for criterion in report.feature_verification
    )

    assert result.github is not None
    check_run = result.github.check_runs[0]
    assert check_run.conclusion == CheckRunConclusion.NEUTRAL.value
    # A neutral conclusion must not read as a pass anywhere in the summary.
    assert "REQUIRES_HUMAN_REVIEW" in (check_run.output.summary or "")
