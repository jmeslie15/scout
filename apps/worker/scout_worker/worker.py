"""The worker: consumes one stage per message, executes it, and reports what happened.

**The worker never decides a job status.** It records ``JobStage`` rows, tool runs, findings, and
questions, and then tells the orchestrator that the stage is done. The status transition, the retry
decision, the risk-based skip, and the recommendation all belong to the control plane, and the
single most useful property of this file is that it contains no call to ``update_status`` and no
reference to a recommendation.

Delivery is **at-least-once** (see :mod:`scout_persistence.queue`), so the stage it runs may already
be resolved: that is checked first and answered with an acknowledgement plus a fresh scheduling
message, which makes redelivery a no-op rather than a second execution.

Failure handling is equally restrained. A stage that raises is recorded with ``outcome=FAILED`` and
its error, the sandbox is still torn down, and a scheduling message is published so the orchestrator
can classify the failure and decide between a retry and a failed job. The worker does not guess.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from scout_domain.enums import FailureClass, JobStatus
from scout_domain.ids import IdKind, new_id
from scout_domain.models.artifact import ArtifactKind
from scout_domain.models.job import JobStageOutcome
from scout_domain.state.machine import next_pipeline_stage
from scout_domain.state.retry import classify_exception
from scout_observability import get_logger, instrument_span
from scout_orchestrator.execution import StageExecution
from scout_orchestrator.reporting import derive_evidence
from scout_orchestrator.stages import StageInputs
from scout_orchestrator.stages.artifacts import PREPARED_ARTIFACT_FILENAME
from scout_orchestrator.stages.prepared import PreparedState, find_artifact
from scout_persistence.queue import JobMessage, consumer_name
from scout_persistence.unit_of_work import UnitOfWork
from scout_tools.artifacts import LocalArtifactStore
from scout_tools.registry import ToolRegistry
from scout_worker.errors import WorkerError, WorkerProtocolError
from scout_worker.executor import StageExecutor
from scout_worker.settings import Settings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from scout_domain.models.job import VerificationJob
    from scout_github import GitHubClient
    from scout_persistence.queue import JobQueue
    from scout_tools.artifacts import ArtifactStore

__all__ = ["Worker"]

_log = get_logger(__name__)

#: Longest error string recorded on a stage row, matching the column width.
_MAX_ERROR: Final = 8000

#: Backoff between loop iterations after an unexpected error.
_LOOP_ERROR_BACKOFF_SECONDS: Final = 2.0


@dataclass(frozen=True, slots=True)
class _StageWork:
    """One stage to execute, resolved from persisted state."""

    job: VerificationJob
    stage: JobStatus


class Worker:
    """Executes exactly one pipeline stage per queue message."""

    def __init__(
        self,
        *,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        github: GitHubClient,
        queue: JobQueue,
        tools: ToolRegistry | None = None,
        executor: StageExecutor | None = None,
        artifacts: ArtifactStore | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        consumer: str | None = None,
    ) -> None:
        """Bind the process-owned collaborators.

        Args:
            settings: Process settings.
            session_factory: Async session factory; every unit of work is short-lived.
            github: GitHub client, used by PREPARING to read the pinned checkout's file list.
            queue: The transport stage messages arrive on, and scheduling messages leave on.
            tools: Tool registry; defaults to every tool Scout ships.
            executor: Stage executor; injectable so a test can substitute one.
            artifacts: Artifact store; defaults to the content-addressed local store.
            sleep: Injectable delay for the loop's error backoff, so a test is instant.
            consumer: Consumer name; defaults to :func:`consumer_name`, which is unique per host
                and PID so two workers are distinguishable in ``XPENDING``.
        """
        self._settings = settings
        self._session_factory = session_factory
        self._github = github
        self._queue = queue
        self._tools = tools if tools is not None else _build_tool_registry()
        self._stage_settings = settings.to_stage_settings()
        self._artifacts: ArtifactStore = artifacts or LocalArtifactStore(settings.artifact_root)
        self._executor = executor or StageExecutor(
            settings=self._stage_settings,
            session_factory=session_factory,
            tools=self._tools,
            github=github,
            artifacts=self._artifacts,
        )
        self._sleep: Callable[[float], Awaitable[None]] = sleep or asyncio.sleep
        self._consumer = consumer or consumer_name("worker")
        self._must_stop = False

    @property
    def consumer(self) -> str:
        """This process's queue consumer name."""
        return self._consumer

    def request_stop(self) -> None:
        """Ask the loop to finish the in-flight stage and return."""
        self._must_stop = True

    # -- Main loop -------------------------------------------------------------------

    async def run_forever(self, *, stop: asyncio.Event | None = None) -> None:
        """Consume stage messages until asked to stop."""
        block_ms = int(self._settings.poll_interval_seconds * 1000)
        consecutive_errors = 0
        _log.info(
            "worker starting",
            consumer=self._consumer,
            stream=self._settings.worker_stream,
        )
        while not self._should_stop(stop):
            try:
                await self.run_once(block_ms=block_ms)
                consecutive_errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                consecutive_errors += 1
                _log.error(
                    "worker loop iteration failed",
                    consecutive_errors=consecutive_errors,
                    error=f"{type(exc).__name__}: {exc}",
                )
                if consecutive_errors >= 5:
                    raise WorkerError(
                        "the worker loop failed repeatedly; exiting so a supervisor can restart it",
                        consecutive_errors=consecutive_errors,
                        last_error=f"{type(exc).__name__}: {exc}",
                    ) from exc
                await self._sleep(_LOOP_ERROR_BACKOFF_SECONDS)
        _log.info("worker stopped", consumer=self._consumer)

    def _should_stop(self, stop: asyncio.Event | None) -> bool:
        """Whether the loop should exit."""
        if self._must_stop:
            return True
        return stop is not None and stop.is_set()

    async def run_once(self, *, block_ms: int | None = None) -> bool:
        """Reserve one batch of stage messages and handle each."""
        reserved = await self._queue.reserve(
            self._consumer,
            block_ms=block_ms if block_ms is not None else 0,
            count=1,
        )
        for message_id, message in reserved:
            await self.handle_message(message_id, message)
        return bool(reserved)

    # -- Message handling ------------------------------------------------------------

    async def handle_message(self, message_id: str, message: JobMessage) -> None:
        """Execute the stage ``message`` names, ack on success, nack on a retryable failure."""
        try:
            await self.process_message(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if classify_exception(exc) is FailureClass.RETRYABLE:
                await self._queue.nack(message_id, reason=f"{type(exc).__name__}: {exc}"[:500])
                _log.warning(
                    "stage message left unacknowledged for redelivery",
                    message_id=message_id,
                    job_id=message.job_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                return
            _log.error(
                "stage message failed with a non-retryable error",
                message_id=message_id,
                job_id=message.job_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            await self._queue.ack(message_id)
            return

        await self._queue.ack(message_id)

    async def process_message(self, message: JobMessage) -> None:
        """Resolve, execute, and record one stage. Never touches the job's status."""
        job = await self._load_job(message.job_id)
        if job is None:
            _log.warning("stage message references an unknown job", job_id=message.job_id)
            return
        if job.status.is_terminal or job.status is JobStatus.CANCELLED:
            _log.info("ignoring work for a finished job", job_id=job.id, status=job.status.value)
            return

        work = await self._resolve_stage(job, message)
        if work is None:
            # Every stage is resolved: the orchestrator owns the finish, so just hand it back.
            await self._hand_back(job, attempt=job.attempt)
            return

        if await self._stage_is_resolved(work.job.id, work.stage):
            # At-least-once delivery: this exact stage already succeeded, so re-running it would
            # duplicate work and evidence. Hand the job back to the orchestrator instead.
            _log.info(
                "stage is already resolved; acknowledging without re-running",
                job_id=work.job.id,
                job_stage=work.stage.value,
            )
            await self._hand_back(work.job, attempt=work.job.attempt)
            return

        if not await self._claim_lease(work.job.id):
            _log.info("another worker holds the lease; leaving the job alone", job_id=work.job.id)
            return

        try:
            stage_row_id, execution = await self._execute_stage(work)
            await self._record_stage(work, stage_row_id=stage_row_id, execution=execution)
        finally:
            await self._release_lease(work.job.id)

        # The orchestrator decides what happens next; tell it the stage is done either way.
        await self._hand_back(work.job, attempt=work.job.attempt)

    async def _resolve_stage(self, job: VerificationJob, message: JobMessage) -> _StageWork | None:
        """Determine which stage to run, preferring the orchestrator's explicit instruction."""
        if message.resume_stage is not None:
            if job.status is not message.resume_stage:
                # The orchestrator advances the status *before* dispatching, so a mismatch means
                # the two components disagree about the contract. Failing loudly here is the only
                # safe response: guessing would run a stage the job's own status contradicts.
                raise WorkerProtocolError(
                    "the dispatched stage does not match the job's status",
                    job_id=job.id,
                    dispatched_stage=message.resume_stage.value,
                    job_status=job.status.value,
                )
            return _StageWork(job=job, stage=message.resume_stage)

        async with UnitOfWork(self._session_factory) as uow:
            resolved = await uow.stages.resolved_stages(job.id)
        stage = next_pipeline_stage(resolved)
        if stage is None:
            return None
        if job.status is not stage and job.status.is_active:
            # Resumption without an explicit stage: the derived stage is authoritative, and the
            # orchestrator will have advanced the status on its next pass.
            _log.info(
                "resuming a stage derived from the resolved set",
                job_id=job.id,
                derived_stage=stage.value,
                job_status=job.status.value,
            )
        return _StageWork(job=job, stage=stage)

    async def _execute_stage(self, work: _StageWork) -> tuple[str, StageExecution]:
        """Record the attempt, run the stage in a sandbox, and return its row id and result.

        The ``RUNNING`` row is committed before the sandbox starts, so a worker that dies mid-stage
        leaves evidence of what it was doing; the id travels back to the caller rather than living
        on the instance, which keeps the return value the complete answer.
        """
        inputs = await self._load_stage_inputs(work.job.id)
        sandbox_run_id = new_id(IdKind.SANDBOX_RUN)
        with instrument_span(
            "worker.stage",
            attributes={
                "job.id": work.job.id,
                "job.stage": work.stage.value,
                "sandbox.run_id": sandbox_run_id,
            },
        ):
            async with UnitOfWork(self._session_factory) as uow:
                stage_row = await uow.stages.start_stage(
                    work.job.id, work.stage, attempt=work.job.attempt
                )
                stage_row_id = stage_row.id

            execution = await self._executor.execute(
                job=work.job,
                stage=work.stage,
                inputs=inputs,
                sandbox_run_id=sandbox_run_id,
            )
        return stage_row_id, execution

    async def _record_stage(
        self, work: _StageWork, *, stage_row_id: str, execution: StageExecution
    ) -> None:
        """Persist the stage's results and close its row. Never writes a job status."""
        result = execution.result
        async with UnitOfWork(self._session_factory) as uow:
            for tool_result in result.tool_results:
                await uow.tool_runs.record(tool_result)
            if result.findings:
                await uow.findings.create_many(list(result.findings))
            for question in result.questions:
                await uow.questions.create(question)
            for agent_run in result.agent_runs:
                await uow.agent_runs.create(agent_run)
            await uow.stages.complete_stage(
                stage_row_id,
                JobStageOutcome.SUCCEEDED if execution.succeeded else JobStageOutcome.FAILED,
                artifact_ids=list(result.artifact_ids),
                error=None if result.error is None else result.error[:_MAX_ERROR],
            )
        _log.info(
            "stage recorded",
            job_id=work.job.id,
            job_stage=work.stage.value,
            succeeded=execution.succeeded,
            findings=len(result.findings),
            questions=len(result.questions),
        )

    # -- Scheduling hand-back --------------------------------------------------------

    async def _hand_back(self, job: VerificationJob, *, attempt: int) -> None:
        """Publish a scheduling message so the orchestrator advances the job.

        The worker deliberately does not advance anything itself. If this publish fails the caller
        nacks the stage message, so the at-least-once transport retries the whole step rather than
        losing the completion.
        """
        await self._queue.enqueue(
            JobMessage(
                job_id=job.id,
                repository_full_name=job.repository_full_name,
                head_sha=job.head_sha,
                attempt=attempt,
                resume_stage=None,
            )
        )

    # -- State ----------------------------------------------------------------------

    async def _load_job(self, job_id: str) -> VerificationJob | None:
        """Return the job value, or ``None`` when it does not exist.

        Bound inside the unit of work and returned outside it because ``UnitOfWork.__aexit__`` can
        swallow an exception, which stops mypy carrying a narrowing (or a return) out of the block.
        """
        async with UnitOfWork(self._session_factory) as uow:
            row = await uow.jobs.get(job_id)
            job = None if row is None else uow.jobs.to_domain(row)
        return job  # noqa: RET504 - bound outside the unit of work so mypy sees the exit path

    async def _stage_is_resolved(self, job_id: str, stage: JobStatus) -> bool:
        """Whether ``stage`` already has a resolved attempt, making a redelivery a no-op."""
        async with UnitOfWork(self._session_factory) as uow:
            resolved = await uow.stages.resolved_stages(job_id)
        return stage in resolved

    async def _load_stage_inputs(self, job_id: str) -> StageInputs:
        """Rebuild the stage inputs from persisted state, exactly as the orchestrator does."""
        async with UnitOfWork(self._session_factory) as uow:
            artifacts = tuple(
                uow.artifacts.to_domain(row) for row in await uow.artifacts.list_for_job(job_id)
            )
            tool_results = tuple(
                uow.tool_runs.to_domain(row) for row in await uow.tool_runs.list_for_job(job_id)
            )
            findings = tuple(
                uow.findings.to_domain(row) for row in await uow.findings.list_for_job(job_id)
            )
            prepared_artifact = find_artifact(
                artifacts,
                kind=ArtifactKind.REPOSITORY_SUMMARY,
                filename=PREPARED_ARTIFACT_FILENAME,
            )
            text = (
                None
                if prepared_artifact is None
                else self._artifacts.get_bytes(prepared_artifact).decode("utf-8", errors="replace")
            )

        if text is None:
            return StageInputs(tool_results=tool_results, findings=findings, artifacts=artifacts)

        prepared = PreparedState.from_text(text)
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
        )

    # -- Lease ---------------------------------------------------------------------

    async def _claim_lease(self, job_id: str) -> bool:
        """Try to take the job's lease."""
        async with UnitOfWork(self._session_factory) as uow:
            claimed = await uow.jobs.claim_lease(
                job_id, self._consumer, ttl_seconds=self._settings.lease_ttl_seconds
            )
        return claimed  # noqa: RET504 - bound outside the unit of work so mypy sees the exit path

    async def _release_lease(self, job_id: str) -> None:
        """Clear the lease so the orchestrator can pick the job back up immediately."""
        async with UnitOfWork(self._session_factory) as uow:
            await uow.jobs.release_lease(job_id)


def _build_tool_registry() -> ToolRegistry:
    """Build the tool registry, using the orchestrator's documented fallback.

    Imported lazily because :mod:`scout_orchestrator.orchestrator` pulls in the whole control-plane
    scheduling surface, and a worker has no business importing that at startup.
    """
    from scout_orchestrator.orchestrator import build_tool_registry

    return build_tool_registry()
