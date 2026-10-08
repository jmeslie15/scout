"""The orchestrator: the component that owns job state and stage sequencing.

Spec section 50 is the requirement that shapes this file: **jobs survive an API restart, an
orchestrator restart, and a worker restart.** That is why there is no in-memory job state here.
Every decision is taken from persisted facts -- ``job_stages`` rows, ``questions`` rows, prepared
artifacts, approval records -- and every decision is written back through the state machine. A job
that is half-processed when the process dies is resumed from its first unresolved stage, which is
exactly what ``next_pipeline_stage`` is for.

The pieces, in the order they run:

1. :meth:`Orchestrator.run_forever` reserves queue messages and, on **every** iteration, sweeps
   expired leases and reclaims abandoned messages. The sweep is the half of crash recovery that
   runs when nothing else does; the pending-list reclaim is the other half.
2. :meth:`Orchestrator.process_job` claims the job lease, refreshes it while a slow stage runs,
   and drives the pipeline to a resting state.
3. :meth:`Orchestrator._drive_pipeline` derives the next stage from the resolved-stage set, records
   risk-based skips *before* moving the status (so the forward guard sees them), asserts the resume
   invariant, advances the status **only** through
   :func:`scout_orchestrator.state.apply_transition`, and runs the stage.
4. On failure, ``classify_exception`` decides whether the failure is retryable. The decision is
   never made by a model and never inferred from a message.

Two execution planes, one scheduler
-----------------------------------

``SCOUT_EXECUTION_MODE`` chooses who runs a stage:

* ``in_process`` (the default) runs it here, through
  :class:`~scout_orchestrator.runner.InProcessStageRunner`. One process, no queue hop, which is what
  makes the whole control plane exercisable offline.
* ``worker`` publishes a stage message and a ``scout_worker`` process executes exactly one stage.
  The orchestrator still owns every status transition; the worker only records what the stage
  observed. A worker that dies mid-stage leaves a ``RUNNING`` stage row, the lease sweep marks it
  failed with a marker, and the orchestrator treats that marker as retryable -- see
  :data:`scout_orchestrator.state.RECLAIM_ERROR_PREFIX` for why that one case is recognised by text
  when nothing else is.

One asymmetry worth naming: the orchestrator never executes repository code in its own process
outside a sandbox. Both planes call :func:`scout_orchestrator.execution.execute_stage`, so there is
exactly one place a workspace and a sandbox are created.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Final

from scout_domain.enums import FailureClass, JobStatus
from scout_domain.ids import IdKind, new_id
from scout_domain.models.artifact import ArtifactKind
from scout_domain.models.base import utcnow
from scout_domain.models.job import JobStageOutcome, VerificationJob
from scout_domain.models.report import VerificationReport
from scout_domain.state.machine import next_pipeline_stage
from scout_domain.state.retry import classify_exception
from scout_observability import get_logger, instrument_span
from scout_orchestrator.errors import (
    OrchestratorError,
    PersistenceUnavailableError,
    StageExecutionError,
    StageInfrastructureError,
)
from scout_orchestrator.execution import StageExecution
from scout_orchestrator.github_checks import publish_check_run, summary_artifact
from scout_orchestrator.notifications import NotificationDispatch, NotificationDispatcher
from scout_orchestrator.reporting import build_report, derive_evidence
from scout_orchestrator.runner import InProcessStageRunner, StageRequest, StageRunner
from scout_orchestrator.sandbox_records import complete_sandbox_run, create_sandbox_run
from scout_orchestrator.settings import EXECUTION_MODE_WORKER, Settings
from scout_orchestrator.stages import StageInputs, StageSettings
from scout_orchestrator.stages.artifacts import PREPARED_ARTIFACT_FILENAME
from scout_orchestrator.stages.prepared import PreparedState, find_artifact
from scout_orchestrator.state import (
    PRE_PIPELINE_STATES,
    SkipDecision,
    apply_transition,
    assert_resume_invariant,
    build_transition_context,
    is_reclaim_error,
    reclaim_error,
    record_audit,
    skip_decision,
    stage_timeout_exceeded,
)
from scout_persistence.queue import JobMessage, consumer_name
from scout_persistence.unit_of_work import UnitOfWork
from scout_tools.registry import ToolRegistry, default_registry

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from scout_domain.models.agent import AgentRun
    from scout_domain.models.artifact import Artifact
    from scout_domain.models.finding import Finding
    from scout_domain.models.question import Question
    from scout_domain.models.tool import ToolResult
    from scout_github import GitHubClient
    from scout_persistence.queue import JobQueue
    from scout_tools.artifacts import ArtifactStore

__all__ = ["OrchestrationState", "Orchestrator", "build_tool_registry"]

_log = get_logger(__name__)

#: Longest failure reason recorded on a job. Matches the job table's column width so a long
#: traceback is truncated deliberately rather than by the database.
_MAX_REASON: Final = 8000

#: Backoff between loop iterations after an unexpected error. Bounded, because a datastore that is
#: down for an hour must not produce a log line per millisecond.
_LOOP_ERROR_BACKOFF_SECONDS: Final = 2.0

#: Report filename inside the artifact store.
_REPORT_FILENAME: Final = "verification-report.json"

#: Severity rank at or above which a finding is announced (HIGH and CRITICAL).
_NOTIFY_SEVERITY_RANK: Final = 3


@dataclass(frozen=True, slots=True)
class OrchestrationState:
    """The persisted facts one scheduling decision is made from."""

    job: VerificationJob
    resolved_stages: frozenset[JobStatus]
    #: Stages whose most recent attempt was a recorded skip. The resume invariant needs them to
    #: tell "resolved ahead of the status because the orchestrator skipped it" from "resolved ahead
    #: of the status because something ran the wrong stage".
    skipped_stages: frozenset[JobStatus]
    open_questions: int
    next_stage: JobStatus | None


@dataclass(frozen=True, slots=True)
class _ReportBundle:
    """Every persisted input one report generation needs."""

    prepared: PreparedState
    artifacts: tuple[Artifact, ...]
    tool_results: tuple[ToolResult, ...]
    findings: tuple[Finding, ...]
    questions: tuple[Question, ...]
    agent_provenance: dict[str, str]

    @property
    def open_questions(self) -> tuple[Question, ...]:
        """The questions a human has not answered."""
        return tuple(question for question in self.questions if question.is_open)

    @property
    def serious_findings(self) -> tuple[Finding, ...]:
        """Findings worth announcing to a human (spec section 47)."""
        return tuple(
            finding for finding in self.findings if finding.severity.rank >= _NOTIFY_SEVERITY_RANK
        )


class Orchestrator:
    """Owns job state transitions and stage scheduling."""

    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        artifacts: ArtifactStore,
        github: GitHubClient,
        queue: JobQueue,
        tools: ToolRegistry | None = None,
        runner: StageRunner | None = None,
        stage_queue: JobQueue | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        consumer: str | None = None,
    ) -> None:
        """Bind the process-owned collaborators.

        Args:
            settings: Process settings.
            session_factory: Async session factory; every unit of work is short-lived.
            artifacts: Durable artifact store for evidence bytes.
            github: The GitHub client. A failure here is recorded, never swallowed.
            queue: The scheduling transport this process consumes.
            tools: Tool registry; defaults to every tool Scout ships, with a documented fallback
                when the tool package is incomplete.
            runner: Stage execution strategy for ``in_process`` mode.
            stage_queue: The worker-visible transport. Required in ``worker`` execution mode and
                rejected otherwise, so a misconfiguration fails at start-up rather than silently
                running stages in the wrong plane.
            sleep: Injectable delay used by the retry backoff. Tests pass a no-op so a retry test
                is instant; production uses :func:`asyncio.sleep`.
            consumer: Consumer name for the queue. Defaults to :func:`consumer_name`, which
                includes the host and PID so two orchestrators are distinguishable.
        """
        if settings.execution_mode == EXECUTION_MODE_WORKER and stage_queue is None:
            raise OrchestratorError(
                "worker execution mode requires the worker-visible stage queue; without it the "
                "orchestrator would silently fall back to running repository code itself",
                execution_mode=settings.execution_mode,
            )

        self._settings = settings
        self._session_factory = session_factory
        self._artifacts = artifacts
        self._github = github
        self._queue = queue
        self._stage_queue = stage_queue
        self._tools = tools if tools is not None else build_tool_registry()
        self._stage_settings: StageSettings = settings.to_stage_settings()
        self._runner: StageRunner = (
            runner
            if runner is not None
            else InProcessStageRunner(
                settings=self._stage_settings,
                tools=self._tools,
                github=github,
                artifact_store=artifacts,
            )
        )
        self._sleep: Callable[[float], Awaitable[None]] = sleep or asyncio.sleep
        self._retry_policy = settings.retry_policy()
        self._consumer = consumer or consumer_name("orchestrator")
        self._dispatcher = NotificationDispatcher(settings=settings)
        self._must_stop = False

    # -- Accessors -------------------------------------------------------------------

    @property
    def consumer(self) -> str:
        """This process's queue consumer name."""
        return self._consumer

    @property
    def settings(self) -> Settings:
        """The process settings, read-only."""
        return self._settings

    def request_stop(self) -> None:
        """Ask the loop to finish the in-flight stage and return.

        Called by the signal handler rather than cancelling the task: abandoning a stage
        mid-transaction would leave a ``RUNNING`` stage row and a leased job, and the whole point
        of the bounded grace period is that the in-flight stage completes instead.
        """
        self._must_stop = True

    # -- Main loop -------------------------------------------------------------------

    async def run_forever(self, *, stop: asyncio.Event | None = None) -> None:
        """Reserve, process, and recover until asked to stop.

        Recovery runs on every iteration rather than on a timer of its own, so a process busy with
        messages still notices a job whose owner died. Both sweeps are cheap indexed queries.
        """
        block_ms = int(self._settings.worker_poll_interval_seconds * 1000)
        consecutive_errors = 0
        _log.info(
            "orchestrator starting",
            consumer=self._consumer,
            execution_mode=self._settings.execution_mode,
            required_stages=sorted(stage.value for stage in self._settings.required_stage_set),
        )
        while not self._should_stop(stop):
            try:
                await self.reclaim_stale_messages()
                await self.sweep_expired_leases()
                await self.run_once(block_ms=block_ms)
                consecutive_errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_errors += 1
                _log.error(
                    "orchestrator loop iteration failed",
                    consecutive_errors=consecutive_errors,
                    max_consecutive_errors=self._settings.max_consecutive_loop_errors,
                    error=f"{type(exc).__name__}: {exc}",
                )
                if consecutive_errors >= self._settings.max_consecutive_loop_errors:
                    raise PersistenceUnavailableError(
                        "the orchestrator loop failed repeatedly and is giving up so a supervisor "
                        "can restart it",
                        consecutive_errors=consecutive_errors,
                        last_error=f"{type(exc).__name__}: {exc}",
                    ) from exc
                await self._sleep(_LOOP_ERROR_BACKOFF_SECONDS)
        _log.info("orchestrator stopped", consumer=self._consumer)

    def _should_stop(self, stop: asyncio.Event | None) -> bool:
        """Whether the loop should exit."""
        if self._must_stop:
            return True
        return stop is not None and stop.is_set()

    async def run_once(self, *, block_ms: int | None = None) -> bool:
        """Reserve one batch of messages and handle each. Returns whether any was claimed."""
        reserved = await self._queue.reserve(
            self._consumer,
            block_ms=block_ms if block_ms is not None else 0,
            count=self._settings.reserve_batch_size,
        )
        for message_id, message in reserved:
            await self.handle_message(message_id, message)
        return bool(reserved)

    # -- Recovery --------------------------------------------------------------------

    async def sweep_expired_leases(self, *, now: Any = None) -> int:
        """Re-enqueue every active job whose lease has expired (spec section 50).

        This is what makes "a job survives a worker restart" true. A crashed worker cannot clear
        its own lease -- nothing runs when a process dies -- so the job would otherwise sit
        ``RUNNING`` forever with nobody owning it. The jobs are re-enqueued so the derivation runs
        again and the work resumes at the first *unresolved* stage rather than from the start.
        """
        moment = now or utcnow()
        requeue: list[JobMessage] = []

        async with UnitOfWork(self._session_factory) as uow:
            rows = await uow.jobs.list_expired_leases(moment)
            for row in rows:
                job = uow.jobs.to_domain(row)
                await uow.jobs.release_lease(job.id, now=moment)
                if not job.is_resumable:
                    # Terminal-but-leased, or failed with no retry budget left: releasing the
                    # lease is the honest action, re-enqueueing the job is not.
                    continue
                # A stage left RUNNING by the dead owner is marked failed, so the timeline shows
                # what happened instead of a stage that will never finish. The marker is what
                # tells the next orchestrator that this failure is a reclaim rather than a stage
                # error, and therefore retryable.
                changed = await uow.stages.mark_running_as_failed(
                    job.id,
                    error=reclaim_error(
                        "the worker holding this stage's lease stopped renewing it; the job was "
                        "reclaimed and the stage will be retried"
                    ),
                    now=moment,
                )
                await record_audit(
                    uow,
                    job,
                    "lease.reclaimed",
                    metadata={
                        "previous_owner": row.lease_owner,
                        "lease_expired_at": (
                            None if row.lease_expires_at is None else str(row.lease_expires_at)
                        ),
                        "running_stages_marked_failed": changed,
                    },
                )
                requeue.append(self._message_for(job, attempt=job.attempt))

        for message in requeue:
            await self._queue.enqueue(message)
        if requeue:
            _log.warning("reclaimed jobs with expired leases", count=len(requeue))
        return len(requeue)

    async def reclaim_stale_messages(self, *, min_idle_ms: int | None = None) -> int:
        """Take over queue messages abandoned by a dead consumer.

        The pending-list half of recovery: a message that was delivered but never acknowledged
        stays pending, and this is what brings it back. Distinct from the lease sweep because a
        crash can happen before or after a stage row exists.
        """
        reclaimed = await self._queue.reclaim_stale(
            self._consumer,
            min_idle_ms=(
                min_idle_ms
                if min_idle_ms is not None
                else int(self._settings.lease_ttl_seconds * 1000)
            ),
            count=self._settings.reserve_batch_size,
        )
        for message_id, message in reclaimed:
            _log.warning(
                "reclaimed an abandoned job message",
                message_id=message_id,
                job_id=message.job_id,
            )
            await self.handle_message(message_id, message)
        return len(reclaimed)

    # -- Message handling ------------------------------------------------------------

    async def handle_message(self, message_id: str, message: JobMessage) -> None:
        """Process one message, acknowledging it or leaving it pending for redelivery."""
        try:
            await self.process_job(message.job_id, attempt=message.attempt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failure_class = classify_exception(exc)
            if failure_class is FailureClass.RETRYABLE:
                # Leave the message pending: the transport redelivers it and the audit trail
                # shows an unacknowledged message rather than a silently dropped one.
                await self._queue.nack(message_id, reason=f"{type(exc).__name__}: {exc}"[:500])
                _log.warning(
                    "job message left unacknowledged for redelivery",
                    message_id=message_id,
                    job_id=message.job_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                return
            # Non-retryable: record the failure on the job even though it happened outside a
            # stage, then the message is done.
            await self._fail_job_unexpectedly(message.job_id, exc)
            await self._queue.ack(message_id)
            return

        await self._queue.ack(message_id)

    async def process_job(self, job_id: str, *, attempt: int) -> None:
        """Claim the lease on ``job_id`` and drive its pipeline to a resting state."""
        job = await self._load_job(job_id)
        if job is None:
            _log.warning("queue message references an unknown job", job_id=job_id)
            return
        if job.status.is_terminal:
            _log.info("ignoring work for a terminal job", job_id=job_id, status=job.status.value)
            await self._release_lease(job_id)
            return
        if job.attempt != attempt:
            # The message is stale: a newer attempt was already scheduled, and running it would
            # double-execute a stage whose results the newer attempt supersedes.
            _log.info(
                "ignoring a stale attempt",
                job_id=job_id,
                message_attempt=attempt,
                job_attempt=job.attempt,
            )
            return
        if not await self._claim_lease(job_id):
            _log.info("another owner holds the lease; leaving the job alone", job_id=job_id)
            return

        refresh = asyncio.create_task(self._refresh_lease(job_id))
        try:
            job = await self._enter_pipeline(job)
            if job.status is JobStatus.CANCELLED:
                return
            await self._drive_pipeline(job)
        finally:
            refresh.cancel()
            try:
                await refresh
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                _log.warning(
                    "lease renewal task ended with an error",
                    job_id=job_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
            await self._release_lease(job_id)

    # -- Pipeline --------------------------------------------------------------------

    async def _drive_pipeline(self, job: VerificationJob) -> None:
        """Run stages until the pipeline is complete, escalates, or stops.

        Skip recording happens *before* the status advance on purpose: recording a ``SKIPPED``
        stage is what makes its predecessors resolved, and the forward guard on the transition into
        the next real stage is satisfied by those rows. Moving the status *through* a skipped stage
        instead would leave the job's status and its derived stage disagreeing, which is exactly
        the divergence :func:`assert_resume_invariant` exists to catch.
        """
        prepared = await self._load_prepared(job.id)

        while True:
            state = await self._load_orchestration_state(job.id)
            job = state.job
            if job.status is JobStatus.CANCELLED or job.status.is_terminal:
                _log.info(
                    "job reached a resting state; stopping",
                    job_id=job.id,
                    status=job.status.value,
                )
                return

            exceeded = stage_timeout_exceeded(
                job, max_runtime_seconds=self._settings.max_job_runtime_seconds
            )
            if exceeded is not None:
                await self._fail_job(job, exceeded)
                return

            pending = await self._pending_worker_failure(job.id)
            if pending is not None:
                failed_stage, error, force_retryable = pending
                await self._handle_stage_failure(
                    job, failed_stage, error, force_retryable=force_retryable
                )
                return

            stage, skipped_any = await self._apply_skips(
                job=job, stage=state.next_stage, prepared=prepared
            )
            if skipped_any:
                state = await self._load_orchestration_state(job.id)
                job = state.job
                stage = state.next_stage

            if stage is None:
                await self._finish_pipeline(job)
                return

            assert_resume_invariant(
                job,
                stage,
                resolved_stages=state.resolved_stages,
                skipped_stages=state.skipped_stages,
            )

            if job.status is not stage:
                job = await self._transition(
                    job, stage, reason="entering the first stage that is not yet resolved"
                )

            if self._settings.execution_mode == EXECUTION_MODE_WORKER:
                await self._dispatch_stage(job, stage)
                return

            execution = await self._run_stage(job, stage)
            if execution.error is not None:
                await self._handle_stage_failure(job, stage, execution.error)
                return
            if stage is JobStatus.PREPARING and prepared is None:
                prepared = await self._load_prepared(job.id)

    async def _apply_skips(
        self,
        *,
        job: VerificationJob,
        stage: JobStatus | None,
        prepared: PreparedState | None,
    ) -> tuple[JobStatus | None, bool]:
        """Record ``SKIPPED`` rows for every leading skippable stage.

        Returns the first stage that must actually run, and whether anything was skipped. A skip
        decision requires the prepared state (the repository configuration and the risk
        assessment), so nothing is skippable before PREPARING -- which is also why PREPARING is in
        :data:`scout_orchestrator.state.NEVER_SKIPPABLE`.
        """
        skipped_any = False
        current = stage
        while current is not None and prepared is not None:
            decision = self._skip_decision(current, prepared=prepared)
            if decision.runs:
                break
            await self._record_skip(job, current, decision)
            skipped_any = True
            current = (await self._load_orchestration_state(job.id)).next_stage
        return current, skipped_any

    def _skip_decision(self, stage: JobStatus, *, prepared: PreparedState) -> SkipDecision:
        """Decide whether ``stage`` may be skipped, honouring the operator floor."""
        return skip_decision(
            stage,
            config=prepared.repository_config,
            risk=prepared.risk,
            required_stages=self._settings.required_stage_set,
        )

    async def _record_skip(
        self, job: VerificationJob, stage: JobStatus, decision: SkipDecision
    ) -> None:
        """Record a skipped stage attempt with its reason (spec section 41)."""
        reason = decision.reason or "skipped"
        async with UnitOfWork(self._session_factory) as uow:
            row = await uow.stages.start_stage(job.id, stage, attempt=job.attempt)
            await uow.stages.complete_stage(
                row.id, JobStageOutcome.SKIPPED, skip_reason=reason[:2000]
            )
            await record_audit(
                uow,
                job,
                "job.stage_skipped",
                metadata={"stage": stage.value, "reason": reason},
            )
        _log.info("stage skipped", job_id=job.id, job_stage=stage.value, reason=reason)

    async def _dispatch_stage(self, job: VerificationJob, stage: JobStatus) -> None:
        """Publish one stage message for a worker to execute.

        The status has already advanced to ``stage``; the worker records the ``JobStage`` rows and
        the results, and re-enqueues a scheduling message when it is done. The lease this process
        holds is released by :meth:`process_job` as soon as it stops actively working, and the
        worker claims its own.
        """
        if self._stage_queue is None:  # pragma: no cover - the constructor guarantees one
            raise OrchestratorError(
                "worker execution mode is configured without a stage queue",
                job_id=job.id,
                stage=stage.value,
            )
        async with UnitOfWork(self._session_factory) as uow:
            await record_audit(
                uow,
                job,
                "job.stage_dispatched",
                metadata={"stage": stage.value, "attempt": job.attempt},
            )
        await self._stage_queue.enqueue(
            self._message_for(job, attempt=job.attempt, resume_stage=stage)
        )
        _log.info("stage dispatched to the worker plane", job_id=job.id, job_stage=stage.value)

    async def _pending_worker_failure(
        self, job_id: str
    ) -> tuple[JobStatus, BaseException, bool] | None:
        """Detect a stage a worker recorded as failed, and classify it for the retry policy.

        In ``worker`` mode the exception object does not survive the process boundary, so the
        failure is reconstructed from the recorded ``JobStage``. A reclaim marker is retryable by
        definition (the platform, not the change, failed); anything else is non-retryable, which is
        the fail-closed reading spec section 51 asks for when a failure cannot be classified by
        type.
        """
        if self._settings.execution_mode != EXECUTION_MODE_WORKER:
            return None
        async with UnitOfWork(self._session_factory) as uow:
            row = await uow.jobs.get(job_id)
            if row is None:
                return None
            job = uow.jobs.to_domain(row)
            stage_in_flight = job.status.is_active and (
                next_pipeline_stage(await uow.stages.resolved_stages(job_id)) == job.status
            )
            attempt = (
                await uow.stages.latest_attempt(job_id, job.status) if stage_in_flight else None
            )
            failed = attempt is not None and attempt.outcome == JobStageOutcome.FAILED.value
            recorded = attempt.error if failed and attempt is not None else None

        if recorded is None:
            return None
        message = recorded or f"{job.status.value} failed in the worker"
        if is_reclaim_error(recorded):
            return (job.status, StageInfrastructureError(message, stage=job.status), True)
        return (job.status, StageExecutionError(message, stage=job.status), False)

    async def _run_stage(self, job: VerificationJob, stage: JobStatus) -> StageExecution:
        """Record the attempt, execute the stage, and persist everything it produced.

        Two short transactions rather than one long one: the ``RUNNING`` stage row and the
        ``PENDING`` sandbox row must be durable *before* untrusted code starts, and the results must
        be written only after it finishes. Holding a transaction across sandbox execution would pin
        a database connection for minutes and lose the crash evidence.
        """
        inputs = await self._load_stage_inputs(job.id)
        sandbox_run_id = new_id(IdKind.SANDBOX_RUN)

        async with UnitOfWork(self._session_factory) as uow:
            stage_row = await uow.stages.start_stage(job.id, stage, attempt=job.attempt)
            await create_sandbox_run(
                uow,
                job_id=job.id,
                sandbox_run_id=sandbox_run_id,
                settings=self._stage_settings,
                repository_config=inputs.repository_config,
            )
            await record_audit(
                uow,
                job,
                "job.stage_started",
                metadata={"stage": stage.value, "attempt": job.attempt},
            )
            stage_row_id = stage_row.id

        with instrument_span(
            "orchestrator.stage.dispatch",
            attributes={"job.id": job.id, "job.stage": stage.value},
        ):
            execution = await self._runner.run_stage(
                StageRequest(
                    job=job,
                    stage=stage,
                    inputs=inputs,
                    sandbox_run_id=sandbox_run_id,
                )
            )

        await self._persist_execution(
            job=job, stage=stage, stage_row_id=stage_row_id, execution=execution
        )
        return execution

    async def _persist_execution(
        self,
        *,
        job: VerificationJob,
        stage: JobStatus,
        stage_row_id: str,
        execution: StageExecution,
    ) -> None:
        """Write the stage's outcome and every artifact, result, finding, and question.

        All in one unit of work, because a stage's results are one logical fact: a half-written
        stage would produce a report that disagrees with its own timeline.
        """
        result = execution.result
        async with UnitOfWork(self._session_factory) as uow:
            for artifact in result.artifacts:
                await uow.artifacts.record(artifact)
            for tool_result in result.tool_results:
                await uow.tool_runs.record(tool_result)
            if result.findings:
                await uow.findings.create_many(list(result.findings))
            for question in result.questions:
                await uow.questions.create(question)
            for agent_run in result.agent_runs:
                await uow.agent_runs.create(agent_run)

            await complete_sandbox_run(uow, execution.sandbox_run)
            outcome = JobStageOutcome.SUCCEEDED if execution.succeeded else JobStageOutcome.FAILED
            await uow.stages.complete_stage(
                stage_row_id,
                outcome,
                artifact_ids=list(result.artifact_ids),
                error=None if result.error is None else result.error[:_MAX_REASON],
            )
            await record_audit(
                uow,
                job,
                "job.stage_completed" if execution.succeeded else "job.stage_failed",
                metadata={
                    "stage": stage.value,
                    "outcome": outcome.value,
                    "attempt": job.attempt,
                    "artifact_ids": list(result.artifact_ids),
                    "tool_runs": [tool_result.id for tool_result in result.tool_results],
                    "findings": len(result.findings),
                    "questions": len(result.questions),
                    "error": None if result.error is None else result.error[:2000],
                },
            )

    # -- Failure handling ------------------------------------------------------------

    async def _handle_stage_failure(
        self,
        job: VerificationJob,
        stage: JobStatus,
        error: BaseException,
        *,
        force_retryable: bool | None = None,
    ) -> None:
        """Classify a stage failure and either schedule a retry or fail the job (spec 51).

        The classification is type-driven, so no wording change and no model output can turn a
        permanent failure into a retry storm. ``force_retryable`` exists only for the worker plane,
        where the exception object did not survive the process boundary and the recorded marker is
        the classifier.
        """
        retryable = (
            classify_exception(error) is FailureClass.RETRYABLE
            if force_retryable is None
            else force_retryable
        )
        failure_class = FailureClass.RETRYABLE if retryable else FailureClass.NON_RETRYABLE
        reason = f"{stage.value} failed: {type(error).__name__}: {error}"[:_MAX_REASON]

        if self._retry_policy.should_retry(attempt=job.attempt, failure_class=failure_class):
            attempt = job.attempt + 1
            delay = self._retry_policy.delay_for_attempt(job.attempt)
            async with UnitOfWork(self._session_factory) as uow:
                failed = await apply_transition(
                    uow, job, JobStatus.FAILED, reason=reason, failure_reason=reason
                )
                await uow.jobs.record_attempt(failed.id, attempt=attempt)
                await record_audit(
                    uow,
                    failed,
                    "job.retry_scheduled",
                    metadata={
                        "stage": stage.value,
                        "failure_class": failure_class.value,
                        "previous_attempt": job.attempt,
                        "next_attempt": attempt,
                        "delay_seconds": round(delay, 3),
                        "error": reason[:2000],
                    },
                )
                queued = await apply_transition(
                    uow,
                    failed,
                    JobStatus.QUEUED,
                    reason=f"retry {attempt} of {failed.max_attempts}",
                    failure_reason=None,
                )
            _log.warning(
                "stage failed; scheduling a retry",
                job_id=job.id,
                job_stage=stage.value,
                failure_class=failure_class.value,
                next_attempt=attempt,
                delay_seconds=round(delay, 3),
            )
            # Bounded backoff before the job becomes visible again. Sleeping here rather than
            # inside a transaction is deliberate: nothing is held while waiting.
            await self._sleep(delay)
            await self._queue.enqueue(
                self._message_for(queued, attempt=attempt, resume_stage=stage)
            )
            return

        async with UnitOfWork(self._session_factory) as uow:
            failed = await apply_transition(
                uow, job, JobStatus.FAILED, reason=reason, failure_reason=reason
            )
            await record_audit(
                uow,
                failed,
                "job.failed",
                metadata={
                    "stage": stage.value,
                    "failure_class": failure_class.value,
                    "retryable": False,
                    "error": reason[:2000],
                },
            )
        _log.error(
            "stage failed with a non-retryable error; the job is failed",
            job_id=job.id,
            job_stage=stage.value,
            failure_class=failure_class.value,
        )
        await self._announce_failure(failed, reason)

    async def _fail_job(self, job: VerificationJob, reason: str) -> None:
        """Fail a job for an orchestrator-level reason (a runtime budget, a missing config)."""
        async with UnitOfWork(self._session_factory) as session:
            failed = await apply_transition(
                session,
                job,
                JobStatus.FAILED,
                reason=reason[:_MAX_REASON],
                failure_reason=reason[:_MAX_REASON],
            )
            await record_audit(session, failed, "job.failed", metadata={"reason": reason[:2000]})
        await self._announce_failure(failed, reason)

    async def _fail_job_unexpectedly(self, job_id: str, error: BaseException) -> None:
        """Record a non-retryable failure raised outside a stage, if the job is not done."""
        job = await self._load_job(job_id)
        if job is None or job.status.is_terminal or job.status is JobStatus.FAILED:
            return
        await self._fail_job(job, f"orchestration failed: {type(error).__name__}: {error}")

    # -- Pipeline completion ---------------------------------------------------------

    async def _finish_pipeline(self, job: VerificationJob) -> None:
        """Generate the report, publish the check run, and notify (spec sections 44-47)."""
        job = await self._ensure_ready_for_report(job)
        bundle = await self._load_report_bundle(job.id)
        if bundle is None:
            await self._fail_job(
                job,
                "the prepared state needed to build the report could not be read; the job cannot "
                "be reported on and must be re-run",
            )
            return

        report = build_report(
            job=job,
            prepared=bundle.prepared,
            tool_results=bundle.tool_results,
            findings=bundle.findings,
            questions=bundle.questions,
            artifacts=bundle.artifacts,
            store=self._artifacts,
            agent_provenance=bundle.agent_provenance,
        )
        job = await self._persist_report(job, report, open_questions=len(bundle.open_questions))
        await self._publish_check(job, report)
        await self._notify_completion(job, report, bundle)

    async def _ensure_ready_for_report(self, job: VerificationJob) -> VerificationJob:
        """Move a job whose pipeline is complete into ``INVESTIGATING``.

        INVESTIGATING is where questions are raised, so the report is generated from a job that has
        been through it. A job that arrived here while still ``QUEUED`` -- the crash window between
        the last stage and the final transition -- is moved explicitly rather than being reported
        on from a pre-pipeline state.
        """
        if job.status is JobStatus.INVESTIGATING:
            return job
        if job.status in PRE_PIPELINE_STATES:
            return await self._transition(
                job,
                JobStatus.INVESTIGATING,
                reason="every pipeline stage is resolved; entering investigation",
            )
        return job

    async def _persist_report(
        self,
        job: VerificationJob,
        report: VerificationReport,
        *,
        open_questions: int,
    ) -> VerificationJob:
        """Record the report as evidence and advance the job to its resting state."""
        artifact = self._artifacts.put_text(
            json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True),
            kind=ArtifactKind.VERIFICATION_REPORT,
            content_type="application/json",
            filename=_REPORT_FILENAME,
            job_id=job.id,
        )
        target = JobStatus.WAITING_FOR_HUMAN if open_questions else JobStatus.READY_FOR_REVIEW
        async with UnitOfWork(self._session_factory) as uow:
            await uow.artifacts.record(artifact)
            await record_audit(
                uow,
                job,
                "report.generated",
                metadata={
                    "recommendation": report.recommendation.value,
                    "verification_status": report.verification_status.value,
                    "risk_level": report.risk_level.value,
                    "report_artifact_id": artifact.id,
                    "open_questions": open_questions,
                    "unverified_areas": len(report.unverified_areas),
                    "findings": len(report.all_findings),
                },
            )
            advanced = await apply_transition(
                uow,
                job,
                target,
                reason=(
                    "investigation raised questions a human must answer"
                    if open_questions
                    else "investigation produced no unresolved questions"
                ),
                recommendation=report.recommendation,
                verification_status=report.verification_status,
                risk_level=report.risk_level,
            )
        _log.info(
            "verification report generated",
            job_id=advanced.id,
            recommendation=report.recommendation.value,
            target_status=target.value,
        )
        return advanced

    async def _publish_check(self, job: VerificationJob, report: VerificationReport) -> None:
        """Publish the GitHub check run and record the id on the audit ledger."""
        publication = await publish_check_run(
            github=self._github,
            job=job,
            report=report,
            dashboard_url=self._settings.dashboard_url,
        )
        artifact = summary_artifact(publication, store=self._artifacts, job_id=job.id)
        async with UnitOfWork(self._session_factory) as uow:
            await uow.artifacts.record(artifact)
            await record_audit(
                uow,
                job,
                "github.check_run.created",
                metadata={
                    "check_run_id": publication.check_run_id,
                    "conclusion": publication.conclusion.value,
                    "details_url": publication.details_url,
                    "summary_artifact_id": artifact.id,
                },
            )

    # -- Notifications ---------------------------------------------------------------

    async def _notify_completion(
        self,
        job: VerificationJob,
        report: VerificationReport,
        bundle: _ReportBundle,
    ) -> None:
        """Announce open questions, serious findings, and completion (spec section 47)."""
        if bundle.open_questions:
            await self._record_dispatch(
                job,
                await self._dispatcher.notify_questions(bundle.open_questions, job),
                event="question",
            )
        if bundle.serious_findings:
            await self._record_dispatch(
                job,
                await self._dispatcher.notify_findings(bundle.serious_findings, job),
                event="finding",
            )
        await self._record_dispatch(
            job,
            await self._dispatcher.notify_completion(report, job),
            event="completion",
        )

    async def _announce_failure(self, job: VerificationJob, reason: str) -> None:
        """Record a failed job on the ledger.

        Deliberately audit-only: ``scout_notifications`` exposes question, finding, completion, and
        approval events, and inventing a fifth one here would bypass its routing and deduplication.
        The failure is on the job row, in the ledger, and on the dashboard -- which is where an
        operator looks -- and a missing question/finding announcement is exactly what the ledger
        entry makes visible.
        """
        async with UnitOfWork(self._session_factory) as uow:
            await record_audit(
                uow,
                job,
                "job.failed.announced",
                metadata={"reason": reason[:2000], "channel": "audit-and-dashboard"},
            )

    async def _record_dispatch(
        self, job: VerificationJob, dispatch: NotificationDispatch, *, event: str
    ) -> None:
        """Persist notification records and record why nothing was sent, if nothing was."""
        async with UnitOfWork(self._session_factory) as uow:
            for record in dispatch.records:
                await uow.notifications.record(record)
            if dispatch.skipped_reason:
                await record_audit(
                    uow,
                    job,
                    "notification.skipped",
                    metadata={"event": event, "reason": dispatch.skipped_reason},
                )
            if dispatch.error:
                await record_audit(
                    uow,
                    job,
                    "notification.failed",
                    metadata={"event": event, "error": dispatch.error},
                )

    # -- Loaders ---------------------------------------------------------------------

    async def _load_job(self, job_id: str) -> VerificationJob | None:
        """Return the job value, or ``None`` when it does not exist.

        The value is bound inside the unit of work and returned outside it because
        ``UnitOfWork.__aexit__`` can swallow an exception (it returns a bool), and mypy -- correctly
        -- refuses to treat a ``return`` inside such a block as the function's only exit.
        """
        async with UnitOfWork(self._session_factory) as uow:
            row = await uow.jobs.get(job_id)
            job = None if row is None else uow.jobs.to_domain(row)
        return job  # noqa: RET504 - bound outside the unit of work so mypy sees the exit path

    async def _load_orchestration_state(self, job_id: str) -> OrchestrationState:
        """Read the persisted facts one scheduling decision needs."""
        async with UnitOfWork(self._session_factory) as uow:
            row = await uow.jobs.get(job_id)
            if row is None:
                raise OrchestratorError("the job disappeared mid-verification", job_id=job_id)
            job = uow.jobs.to_domain(row)
            resolved = await uow.stages.resolved_stages(job_id)
            skipped = _skipped_stages(await uow.stages.list_for_job(job_id))
            open_questions = await uow.questions.count_open_for_job(job_id)
        return OrchestrationState(
            job=job,
            resolved_stages=resolved,
            skipped_stages=skipped,
            open_questions=open_questions,
            next_stage=next_pipeline_stage(resolved),
        )

    async def _load_prepared(self, job_id: str) -> PreparedState | None:
        """Read the prepared-state document, or ``None`` before PREPARING has run."""
        async with UnitOfWork(self._session_factory) as uow:
            prepared = await load_prepared_state(uow, job_id, store=self._artifacts)
        return prepared  # noqa: RET504 - bound outside the unit of work so mypy sees the exit path

    async def _load_stage_inputs(self, job_id: str) -> StageInputs:
        """Rebuild everything a stage may see from persisted state (Rule 5)."""
        async with UnitOfWork(self._session_factory) as uow:
            prepared = await load_prepared_state(uow, job_id, store=self._artifacts)
            tool_results = tuple(
                uow.tool_runs.to_domain(row) for row in await uow.tool_runs.list_for_job(job_id)
            )
            findings = tuple(
                uow.findings.to_domain(row) for row in await uow.findings.list_for_job(job_id)
            )
            artifacts = tuple(
                uow.artifacts.to_domain(row) for row in await uow.artifacts.list_for_job(job_id)
            )
            agent_provenance = _provenance_of(
                [uow.agent_runs.to_domain(row) for row in await uow.agent_runs.list_for_job(job_id)]
            )

        if prepared is None:
            return StageInputs(tool_results=tool_results, findings=findings, artifacts=artifacts)

        derived = derive_evidence(
            prepared=prepared,
            tool_results=tool_results,
            artifacts=artifacts,
            store=self._artifacts,
        )
        return StageInputs(
            change_set=prepared.change_set,
            repository_summary=prepared.repository_summary,
            risk=prepared.risk,
            repository_config=prepared.repository_config,
            pr_body=prepared.pr_body,
            requirement_text=prepared.requirement_text,
            commit_messages=prepared.commit_messages,
            tool_results=tool_results,
            findings=findings,
            artifacts=artifacts,
            sections=derived.sections,
            criteria=derived.criteria,
            tests=derived.tests,
            agent_provenance=agent_provenance,
        )

    async def _load_report_bundle(self, job_id: str) -> _ReportBundle | None:
        """Read every persisted input the report needs."""
        async with UnitOfWork(self._session_factory) as uow:
            prepared = await load_prepared_state(uow, job_id, store=self._artifacts)
            artifacts = tuple(
                uow.artifacts.to_domain(row) for row in await uow.artifacts.list_for_job(job_id)
            )
            tool_results = tuple(
                uow.tool_runs.to_domain(row) for row in await uow.tool_runs.list_for_job(job_id)
            )
            findings = tuple(
                uow.findings.to_domain(row) for row in await uow.findings.list_for_job(job_id)
            )
            questions = await load_questions(uow, job_id)
            agent_provenance = _provenance_of(
                [uow.agent_runs.to_domain(row) for row in await uow.agent_runs.list_for_job(job_id)]
            )

        # Checked outside the unit of work: its ``__aexit__`` can swallow an exception, so mypy
        # will not carry a narrowing from inside the block to here.
        if prepared is None:
            return None
        return _ReportBundle(
            prepared=prepared,
            artifacts=artifacts,
            tool_results=tool_results,
            findings=findings,
            questions=questions,
            agent_provenance=agent_provenance,
        )

    # -- Lease ---------------------------------------------------------------------

    async def _claim_lease(self, job_id: str) -> bool:
        """Try to take the job's lease. A live lease held elsewhere means "leave it alone"."""
        async with UnitOfWork(self._session_factory) as uow:
            claimed = await uow.jobs.claim_lease(
                job_id, self._consumer, ttl_seconds=self._settings.lease_ttl_seconds
            )
        return claimed  # noqa: RET504 - bound outside the unit of work so mypy sees the exit path

    async def _release_lease(self, job_id: str) -> None:
        """Clear the lease. Called whenever this process stops actively working."""
        async with UnitOfWork(self._session_factory) as uow:
            await uow.jobs.release_lease(job_id)

    async def _refresh_lease(self, job_id: str) -> None:
        """Renew the lease periodically while a long stage runs.

        A test suite can outlast the lease. Without renewal the reclaim sweep would hand the job to
        a second orchestrator while the first is still working, and two processes would run the same
        stage.
        """
        interval = max(1.0, self._settings.lease_ttl_seconds / 3)
        while True:
            await self._sleep(interval)
            try:
                renewed = await self._claim_lease(job_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                _log.warning(
                    "could not renew the job lease",
                    job_id=job_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                continue
            if not renewed:  # pragma: no cover - only reachable if the lease was taken away
                _log.error("the job lease could not be renewed", job_id=job_id)
                return

    # -- Transitions and queue ------------------------------------------------------

    async def _transition(
        self, job: VerificationJob, target: JobStatus, *, reason: str
    ) -> VerificationJob:
        """Advance the job's status through the state machine, never by assignment."""
        async with UnitOfWork(self._session_factory) as uow:
            context = await build_transition_context(uow, job, reason=reason)
            advanced = await apply_transition(uow, job, target, reason=reason, context=context)
        return advanced  # noqa: RET504 - see the note on _load_job

    async def _enter_pipeline(self, job: VerificationJob) -> VerificationJob:
        """Move the job into a state from which the pipeline can be derived."""
        if job.status is JobStatus.RECEIVED:
            job = await self._transition(
                job, JobStatus.QUEUED, reason="handing the persisted job to the pipeline"
            )
        if job.status is JobStatus.FAILED:
            job = await self._transition(
                job, JobStatus.QUEUED, reason="resuming a failed job with retry budget remaining"
            )
        if job.status is JobStatus.WAITING_FOR_HUMAN:
            async with UnitOfWork(self._session_factory) as uow:
                context = await build_transition_context(uow, job, reason="questions answered")
            if context.open_questions == 0:
                job = await self._transition(
                    job,
                    JobStatus.INVESTIGATING,
                    reason="every open question was answered or dismissed",
                )
        return job

    @staticmethod
    def _message_for(
        job: VerificationJob,
        *,
        attempt: int,
        resume_stage: JobStatus | None = None,
    ) -> JobMessage:
        """Build the queue message describing one unit of work for ``job``."""
        return JobMessage(
            job_id=job.id,
            repository_full_name=job.repository_full_name,
            head_sha=job.head_sha,
            attempt=attempt,
            resume_stage=resume_stage,
        )


def build_tool_registry() -> ToolRegistry:
    """Build Scout's tool registry, with a documented fallback.

    ``scout_tools.tools`` is listed as an incomplete workstream, so its absence must degrade the
    tool set rather than make the orchestrator unimportable. The fallback registers the tools that
    are known to exist; a deployment with no tools still verifies, it simply reports that no
    deterministic check was applicable.
    """
    try:
        return default_registry()
    except Exception as exc:
        _log.warning(
            "the default tool registry could not be built; falling back to the tools that are "
            "importable",
            error=f"{type(exc).__name__}: {exc}",
        )
        return ToolRegistry(_fallback_tools())


def _fallback_tools() -> list[Any]:
    """Import the tools that exist, skipping any that do not."""
    tools: list[Any] = []
    for module_name, class_name in (
        ("scout_tools.tools.pytest", "PytestTool"),
        ("scout_tools.tools.coverage", "CoverageTool"),
    ):
        try:
            module = __import__(module_name, fromlist=[class_name])
            tools.append(getattr(module, class_name)())
        except Exception as exc:
            _log.warning(
                "could not load a fallback tool",
                module=module_name,
                error=f"{type(exc).__name__}: {exc}",
            )
    return tools


async def load_prepared_state(
    uow: UnitOfWork, job_id: str, *, store: ArtifactStore
) -> PreparedState | None:
    """Read the prepared-state document from the newest matching artifact, or ``None``."""
    artifacts = tuple(
        uow.artifacts.to_domain(row) for row in await uow.artifacts.list_for_job(job_id)
    )
    artifact = find_artifact(
        artifacts, kind=ArtifactKind.REPOSITORY_SUMMARY, filename=PREPARED_ARTIFACT_FILENAME
    )
    if artifact is None:
        return None
    try:
        text = store.get_bytes(artifact).decode("utf-8", errors="replace")
        return PreparedState.from_text(text)
    except Exception as exc:
        raise OrchestratorError(
            "the prepared state could not be read, so the job cannot be resumed or reported on",
            job_id=job_id,
            artifact_id=artifact.id,
            error=f"{type(exc).__name__}: {exc}",
        ) from exc


async def load_questions(uow: UnitOfWork, job_id: str) -> tuple[Question, ...]:
    """Load the job's questions with their answers, newest first."""
    rows = await uow.questions.list_for_job(job_id)
    loaded: list[Question] = []
    for row in rows:
        answers = await uow.questions.list_answers(row.id)
        loaded.append(uow.questions.to_domain(row, answers))
    return tuple(loaded)


def _skipped_stages(rows: Sequence[Any]) -> frozenset[JobStatus]:
    """Return the stages whose most recent attempt was a recorded skip.

    Only the latest attempt per stage counts: a stage that failed once and was then deliberately
    skipped is, at the end, a skip -- and the resume invariant has to see the final state, not the
    history.
    """
    latest: dict[str, Any] = {}
    for row in rows:
        previous = latest.get(str(row.stage))
        if previous is None or int(row.attempt) >= int(previous.attempt):
            latest[str(row.stage)] = row
    return frozenset(
        JobStatus(stage)
        for stage, row in latest.items()
        if row.outcome == JobStageOutcome.SKIPPED.value
    )


def _provenance_of(runs: Sequence[AgentRun]) -> dict[str, str]:
    """Render agent runs as ``agent_provenance`` entries (spec section 39)."""
    provenance: dict[str, str] = {}
    for run in runs:
        role = run.role.value
        provenance[f"{role}.prompt_version"] = run.prompt_version
        if run.provider:
            provenance[f"{role}.provider"] = run.provider
        if run.model:
            provenance[f"{role}.model"] = run.model
    return provenance
