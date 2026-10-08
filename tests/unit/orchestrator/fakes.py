"""An in-memory :class:`scout_persistence.unit_of_work.UnitOfWork` double.

Why a hand-written fake rather than a database: the behaviour under test is the orchestrator's
*use* of the state machine -- that it reads the resolved-stage set and the open-question count from
persistence, hands them to ``assert_transition``, and never writes a status the machine refused.
That is control flow, and proving it needs a store that records what it was asked to do, not a
PostgreSQL instance.

The rows are the **real** SQLAlchemy row classes from ``scout_persistence.tables``, and the
conversions go through the **real** mapping functions. That matters: a fake built from ad-hoc
dictionaries would pass while the production path failed, whereas this one fails when a column is
renamed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from scout_domain.enums import ApprovalDecision, JobStatus, QuestionStatus
from scout_domain.models.approval import Approval
from scout_domain.models.artifact import ArtifactKind
from scout_domain.models.base import utcnow
from scout_domain.models.job import JobStageOutcome
from scout_persistence.mapping import (
    from_approval,
    to_agent_run,
    to_approval,
    to_artifact,
    to_finding,
    to_job_stage,
    to_question,
    to_tool_result,
    to_verification_job,
)
from scout_persistence.repositories.stage import RESOLVED_OUTCOMES
from scout_persistence.tables import (
    AgentRunRow,
    ApprovalRow,
    ArtifactRow,
    AuditEventRow,
    FindingRow,
    JobRow,
    JobStageRow,
    NotificationRow,
    QuestionRow,
    SandboxRunRow,
    ToolRunRow,
)

__all__ = ["FakeUnitOfWork", "RecordingSleep", "make_job_row"]


def make_job_row(
    *,
    job_id: str = "job_01J8ZQ4K2M7X3N9PQRSTVWXY0",
    status: JobStatus = JobStatus.RECEIVED,
    head_sha: str = "b" * 40,
    base_sha: str = "a" * 40,
    attempt: int = 1,
    max_attempts: int = 3,
    **overrides: Any,
) -> JobRow:
    """Build a ``verification_jobs`` row without a session."""
    fields: dict[str, Any] = {
        "id": job_id,
        "repository": {"full_name": "acme/widgets"},
        "repository_full_name": "acme/widgets",
        "pull_request_number": 42,
        "pull_request_title": "Add widget export",
        "pull_request_author": "octocat",
        "commits": {"base_sha": base_sha, "head_sha": head_sha},
        "head_sha": head_sha,
        "base_sha": base_sha,
        "status": status.value,
        "risk_level": "MEDIUM",
        "verification_status": "PENDING",
        "attempt": attempt,
        "max_attempts": max_attempts,
        "correlation_id": "corr-test",
        "created_at": utcnow(),
        "updated_at": utcnow(),
    }
    fields.update(overrides)
    return JobRow(**fields)


class _Jobs:
    """The job repository surface the orchestrator uses."""

    def __init__(self, state: _State) -> None:
        self._state = state

    async def get(self, job_id: str) -> JobRow | None:
        return self._state.jobs.get(job_id)

    def to_domain(self, row: JobRow) -> Any:
        return to_verification_job(row)

    async def update_status(self, job_id: str, new_status: JobStatus, **fields: Any) -> JobRow:
        row = self._require(job_id)
        self._state.status_writes.append((job_id, new_status, dict(fields)))
        row.status = new_status.value
        for name, value in fields.items():
            setattr(row, name, value)
        self._state.flush()
        return row

    async def record_attempt(self, job_id: str, *, attempt: int) -> JobRow:
        row = self._require(job_id)
        row.attempt = attempt
        self._state.flush()
        return row

    async def claim_lease(
        self,
        job_id: str,
        owner: str,
        ttl_seconds: int = 900,
        *,
        now: datetime | None = None,
    ) -> bool:
        row = self._require(job_id)
        moment = now or utcnow()
        if (
            row.lease_expires_at is not None
            and row.lease_expires_at > moment
            and row.lease_owner != owner
        ):
            return False
        row.lease_owner = owner
        row.lease_expires_at = moment + timedelta(seconds=ttl_seconds)
        return True

    async def release_lease(self, job_id: str, *, now: datetime | None = None) -> None:
        row = self._require(job_id)
        row.lease_owner = None
        row.lease_expires_at = None
        del now

    async def list_expired_leases(self, now: datetime, *, limit: int = 100) -> list[JobRow]:
        return [
            row
            for row in self._state.jobs.values()
            if row.lease_expires_at is not None
            and row.lease_expires_at <= now
            and JobStatus(row.status).is_active
        ][:limit]

    def _require(self, job_id: str) -> JobRow:
        row = self._state.jobs.get(job_id)
        if row is None:
            raise LookupError(f"no verification job {job_id!r}")
        return row


class _Stages:
    """The stage repository surface the orchestrator and the worker use."""

    def __init__(self, state: _State) -> None:
        self._state = state
        self._counter = 0

    def to_domain(self, row: JobStageRow) -> Any:
        return to_job_stage(row)

    async def start_stage(
        self, job_id: str, stage: JobStatus, attempt: int = 1, *, started_at: datetime | None = None
    ) -> JobStageRow:
        self._counter += 1
        row = JobStageRow(
            id=f"stage_{self._counter:04d}",
            job_id=job_id,
            stage=stage.value,
            outcome=JobStageOutcome.RUNNING.value,
            attempt=attempt,
            started_at=started_at or utcnow(),
            artifact_ids=[],
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        self._state.stages.append(row)
        return row

    async def complete_stage(
        self, stage_id: str, outcome: JobStageOutcome, **fields: Any
    ) -> JobStageRow:
        row = next(row for row in self._state.stages if row.id == stage_id)
        row.outcome = outcome.value
        for name, value in fields.items():
            setattr(row, name, value)
        if outcome.is_resolved and row.completed_at is None:
            row.completed_at = utcnow()
        return row

    async def resolved_stages(self, job_id: str) -> frozenset[JobStatus]:
        return frozenset(
            JobStatus(row.stage)
            for row in self._state.stages
            if row.job_id == job_id and row.outcome in {o.value for o in RESOLVED_OUTCOMES}
        )

    async def latest_attempt(self, job_id: str, stage: JobStatus) -> JobStageRow | None:
        rows = [
            row for row in self._state.stages if row.job_id == job_id and row.stage == stage.value
        ]
        return max(rows, key=lambda row: row.attempt, default=None)

    async def list_for_job(self, job_id: str) -> list[JobStageRow]:
        return [row for row in self._state.stages if row.job_id == job_id]

    async def mark_running_as_failed(
        self, job_id: str, *, error: str, now: datetime | None = None
    ) -> int:
        changed = 0
        for row in self._state.stages:
            if row.job_id == job_id and row.outcome == JobStageOutcome.RUNNING.value:
                row.outcome = JobStageOutcome.FAILED.value
                row.error = error
                row.completed_at = now or utcnow()
                changed += 1
        return changed


class _Questions:
    """The question repository surface used by the orchestrator."""

    def __init__(self, state: _State) -> None:
        self._state = state

    def to_domain(self, row: QuestionRow, answers: Any = ()) -> Any:
        return to_question(row, answers)

    async def count_open_for_job(self, job_id: str) -> int:
        return sum(
            1
            for row in self._state.questions
            if row.job_id == job_id and row.status == QuestionStatus.OPEN.value
        )

    async def list_for_job(self, job_id: str, *, status: Any = None) -> list[QuestionRow]:
        return [
            row
            for row in self._state.questions
            if row.job_id == job_id and (status is None or row.status == status.value)
        ]

    async def list_answers(self, question_id: str) -> list[Any]:
        del question_id
        return []

    async def create(self, question: Any) -> QuestionRow:
        self._state.created_questions.append(question)
        row = QuestionRow(
            id=question.id,
            job_id=question.job_id,
            title=question.title,
            description=question.description,
            context=question.context,
            investigation_summary=question.investigation_summary,
            evidence=[],
            severity=question.severity.value,
            options=list(question.options),
            default_assumption=question.default_assumption,
            status=question.status.value,
            created_by=question.created_by,
            created_by_type=question.created_by_type.value,
            created_at=question.created_at,
            updated_at=utcnow(),
        )
        self._state.questions.append(row)
        return row


class _Approvals:
    """The approval repository surface used by the transition context."""

    def __init__(self, state: _State) -> None:
        self._state = state

    def to_domain(self, row: ApprovalRow) -> Any:
        return to_approval(row)

    async def list_valid_for_commit(self, job_id: str, commit_sha: str) -> list[Any]:
        return [
            row
            for row in self._state.approvals
            if row.job_id == job_id and row.commit_sha == commit_sha and row.invalidated_at is None
        ]


class _Audit:
    """Append-only audit ledger."""

    def __init__(self, state: _State) -> None:
        self._state = state

    async def append(self, event: Any) -> AuditEventRow:
        self._state.audit_events.append(event)
        return AuditEventRow(
            id=event.id,
            actor=event.actor,
            actor_type=event.actor_type.value,
            action=event.action,
            resource=event.resource,
            resource_id=event.resource_id,
            occurred_at=event.timestamp,
            metadata={},
            job_id=event.job_id,
        )


class _Findings:
    """Finding writes and reads."""

    def __init__(self, state: _State) -> None:
        self._state = state

    def to_domain(self, row: FindingRow) -> Any:
        return to_finding(row)

    async def create_many(self, findings: list[Any]) -> list[FindingRow]:
        self._state.findings.extend(findings)
        return []

    async def list_for_job(self, job_id: str, **_: Any) -> list[FindingRow]:
        return []


class _ToolRuns:
    """Tool-run writes and reads."""

    def __init__(self, state: _State) -> None:
        self._state = state

    def to_domain(self, row: ToolRunRow) -> Any:
        return to_tool_result(row)

    async def record(self, result: Any) -> ToolRunRow:
        self._state.tool_runs.append(result)
        return ToolRunRow(
            id=result.id,
            job_id=result.job_id,
            stage=result.stage,
            tool=result.tool,
            capability="test.run",
            invocation={},
            version=result.version,
            command=result.command,
            argv=list(result.argv),
            exit_code=result.exit_code,
            status=result.status.value,
            started_at=result.started_at,
            completed_at=result.completed_at,
            duration_seconds=result.duration_seconds,
            summary=dict(result.summary.values),
            error=result.error,
            infrastructure_failure=result.infrastructure_failure,
            metadata={},
        )

    async def list_for_job(self, job_id: str, **_: Any) -> list[ToolRunRow]:
        return []


class _Artifacts:
    """Artifact locator writes and reads."""

    def __init__(self, state: _State) -> None:
        self._state = state

    def to_domain(self, row: ArtifactRow) -> Any:
        return to_artifact(row)

    async def record(self, artifact: Any) -> ArtifactRow:
        self._state.artifacts.append(artifact)
        return ArtifactRow(
            id=artifact.id,
            job_id=artifact.job_id,
            kind=artifact.kind.value
            if isinstance(artifact.kind, ArtifactKind)
            else str(artifact.kind),
            content_type=artifact.content_type,
            size_bytes=artifact.size_bytes,
            sha256=artifact.sha256,
            storage_key=artifact.storage_key,
            filename=artifact.filename,
            created_at=artifact.created_at,
            updated_at=utcnow(),
        )

    async def list_for_job(self, job_id: str, **_: Any) -> list[ArtifactRow]:
        return []


class _AgentRuns:
    """Agent-run writes and reads."""

    def __init__(self, state: _State) -> None:
        self._state = state

    def to_domain(self, row: AgentRunRow) -> Any:
        return to_agent_run(row)

    async def create(self, run: Any) -> AgentRunRow:
        self._state.agent_runs.append(run)
        return AgentRunRow(
            id=run.id,
            job_id=run.job_id,
            role=run.role.value,
            status=run.status.value,
            prompt_version=run.prompt_version,
            provider=run.provider,
            model=run.model,
            started_at=run.started_at,
            completed_at=run.completed_at,
            input_tokens=run.input_tokens,
            output_tokens=run.output_tokens,
            estimated_cost_usd=run.estimated_cost_usd,
            context_digest=run.context_digest,
            created_at=run.created_at,
            updated_at=utcnow(),
        )

    async def list_for_job(self, job_id: str, **_: Any) -> list[AgentRunRow]:
        return []


class _SandboxRuns:
    """Sandbox-run rows, which the orchestrator and the worker both write."""

    def __init__(self, state: _State) -> None:
        self._state = state

    async def create(self, run: Any) -> SandboxRunRow:
        self._state.sandbox_runs[run.id] = run
        return SandboxRunRow(
            id=run.id,
            job_id=run.job_id,
            backend=run.backend.value,
            image=run.image,
            limits=run.limits.model_dump(mode="json"),
            policy=run.policy.model_dump(mode="json"),
            status=run.status.value,
            workspace_path=run.workspace_path,
            started_at=run.started_at,
            tool_run_ids=[],
            created_at=utcnow(),
            updated_at=utcnow(),
        )

    async def mark_running(
        self,
        sandbox_run_id: str,
        *,
        native_id: str | None = None,
        workspace_path: str | None = None,
    ) -> Any:
        run = self._state.sandbox_runs[sandbox_run_id]
        run.status = run.status.__class__.RUNNING
        if workspace_path is not None:
            run.workspace_path = workspace_path
        del native_id
        return run

    async def complete(self, sandbox_run_id: str, *, status: Any, error: str | None = None) -> Any:
        run = self._state.sandbox_runs[sandbox_run_id]
        # ``completed_at`` is set before the terminal status because the domain model validates each
        # assignment: a terminal status without a completion time is rejected on the spot.
        run.completed_at = utcnow()
        run.error = error
        run.status = status
        return run


class _Notifications:
    """Notification records."""

    def __init__(self, state: _State) -> None:
        self._state = state

    async def record(self, record: Any) -> NotificationRow:
        self._state.notifications.append(record)
        return NotificationRow(
            id=record.id,
            job_id=record.job_id,
            channel=record.channel.value,
            severity=record.severity.value,
            subject=record.subject,
            body=record.body,
            dedup_key=record.dedup_key,
            status=record.status.value,
            attempts=record.attempts,
            created_at=record.created_at,
            updated_at=utcnow(),
        )


@dataclass
class _State:
    """Everything the fake stores between units of work."""

    jobs: dict[str, JobRow] = field(default_factory=dict)
    stages: list[JobStageRow] = field(default_factory=list)
    questions: list[QuestionRow] = field(default_factory=list)
    approvals: list[Any] = field(default_factory=list)
    audit_events: list[Any] = field(default_factory=list)
    findings: list[Any] = field(default_factory=list)
    tool_runs: list[Any] = field(default_factory=list)
    artifacts: list[Any] = field(default_factory=list)
    agent_runs: list[Any] = field(default_factory=list)
    sandbox_runs: dict[str, Any] = field(default_factory=dict)
    notifications: list[Any] = field(default_factory=list)
    created_questions: list[Any] = field(default_factory=list)
    #: Every ``update_status`` call, in order. The orchestrator tests assert on this.
    status_writes: list[tuple[str, JobStatus, dict[str, Any]]] = field(default_factory=list)

    def flush(self) -> None:
        """No-op: the fake has no transaction to flush."""


class FakeUnitOfWork:
    """A ``UnitOfWork`` whose repositories are dictionaries.

    Usable as an async context manager, and re-enterable, because the orchestrator opens a fresh
    unit of work per operation and a test needs to observe state across all of them.
    """

    def __init__(self, state: _State | None = None, *, fail_on_exit: bool = False) -> None:
        self.state = state or _State()
        self.session = None
        self.fail_on_exit = fail_on_exit
        self.entered = 0
        self.jobs = _Jobs(self.state)
        self.stages = _Stages(self.state)
        self.questions = _Questions(self.state)
        self.approvals = _Approvals(self.state)
        self.audit_events = _Audit(self.state)
        self.findings = _Findings(self.state)
        self.tool_runs = _ToolRuns(self.state)
        self.artifacts = _Artifacts(self.state)
        self.agent_runs = _AgentRuns(self.state)
        self.sandbox_runs = _SandboxRuns(self.state)
        self.notifications = _Notifications(self.state)

    async def __aenter__(self) -> FakeUnitOfWork:
        self.entered += 1
        return self

    async def __aexit__(self, *exc_info: object) -> bool:
        if self.fail_on_exit and exc_info[0] is None:
            raise RuntimeError("simulated commit failure")
        return False

    def statuses_written(self) -> list[JobStatus]:
        """The statuses the orchestrator asked to write, in order."""
        return [status for _job_id, status, _fields in self.state.status_writes]

    def audit_actions(self) -> list[str]:
        """The audit actions recorded, in order."""
        return [event.action for event in self.state.audit_events]

    def audit_events_for(self) -> list[Any]:
        """The recorded audit events, in order."""
        return list(self.state.audit_events)

    def add_question(self, job_id: str, status: QuestionStatus = QuestionStatus.OPEN) -> None:
        """Seed an open (or closed) question row."""
        self.state.questions.append(
            QuestionRow(
                id=f"qst_{len(self.state.questions):04d}",
                job_id=job_id,
                title="Should export metadata be visible to managers?",
                description="The PR adds manager-visible export metadata.",
                context="",
                investigation_summary="",
                evidence=[],
                severity="HIGH",
                options=[],
                status=status.value,
                created_by="investigation",
                created_by_type="AGENT",
                created_at=utcnow(),
                updated_at=utcnow(),
            )
        )

    def add_stage(
        self,
        job_id: str,
        stage: JobStatus,
        outcome: JobStageOutcome,
        *,
        attempt: int = 1,
        error: str | None = None,
        skip_reason: str | None = None,
    ) -> JobStageRow:
        """Seed a completed stage row."""
        row = JobStageRow(
            id=f"stage_seed_{len(self.state.stages):04d}",
            job_id=job_id,
            stage=stage.value,
            outcome=outcome.value,
            attempt=attempt,
            started_at=utcnow(),
            completed_at=utcnow() if outcome.is_resolved or error else None,
            error=error,
            skip_reason=skip_reason,
            artifact_ids=[],
            created_at=utcnow(),
            updated_at=utcnow(),
        )
        self.state.stages.append(row)
        return row

    def add_job(self, row: JobRow) -> JobRow:
        """Seed a job row."""
        self.state.jobs[row.id] = row
        return row

    def job_row(self, job_id: str) -> JobRow:
        """Return the stored job row, so a test can assert on the persisted column values."""
        return self.state.jobs[job_id]

    def add_approval(
        self,
        *,
        job_id: str,
        commit_sha: str,
        decision: ApprovalDecision = ApprovalDecision.APPROVE,
    ) -> Approval:
        """Seed a still-valid human approval, as the approvals table would hold it."""
        approval = Approval(
            id=f"apv_{len(self.state.approvals):04d}",
            job_id=job_id,
            commit_sha=commit_sha,
            decision=decision,
            decided_by="dana",
            comment="Reviewed by hand.",
        )
        self.state.approvals.append(from_approval(approval))
        return approval

    def sandbox_run_for(self, job_id: str) -> Any:
        """The most recently created sandbox run for a job."""
        runs = [run for run in self.state.sandbox_runs.values() if run.job_id == job_id]
        return max(runs, key=lambda run: run.started_at, default=None)

    @classmethod
    def with_job(cls, row: JobRow) -> FakeUnitOfWork:
        """Build a fake already holding ``row``."""
        uow = cls()
        uow.add_job(row)
        return uow


class RecordingSleep:
    """An injectable replacement for :func:`asyncio.sleep`.

    Retry backoff is real behaviour worth asserting on -- the delays are exponential and bounded --
    but a test that actually waited would be slow and flaky. Recording the requested delays makes
    them observable without costing wall-clock time.
    """

    def __init__(self) -> None:
        """Start with no recorded delays."""
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        """Record ``delay`` instead of sleeping."""
        self.delays.append(delay)

    @property
    def total(self) -> float:
        """The total delay a real sleep would have taken."""
        return sum(self.delays)
