"""The LLM gateway: Scout's single guarded path to a model (spec sections 37, 38, 51).

Everything the rest of Scout knows about models goes through :class:`LLMGateway`. It owns:

* **routing** — agent role + prompt version + optional overrides resolve to one
  :class:`~scout_llm.types.ModelTarget`;
* **prompt loading** — a versioned template is loaded and rendered strictly;
* **retry** — classification via ``classify_exception``, backoff via ``RetryPolicy``, and
  *no* retry logic inside any provider;
* **accounting** — one ``ModelRequestRecord`` per attempt, including the failed ones,
  because spec section 51 requires retries to be recorded rather than inferred from logs;
* **concurrency** — a semaphore bounds simultaneous provider calls.

What it deliberately does **not** do is decide anything. It returns text, optional parsed
JSON, and an accounting record. No verification outcome, status, or risk level is computed
here (Rule 9): LLM output is ``EvidenceStrength.INFERENCE`` and the domain layer is what
keeps it out of deterministic decisions.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from scout_domain.enums import AgentRole
from scout_domain.errors import ScoutDomainError
from scout_domain.ids import IdKind, new_id
from scout_domain.models.model_request import ModelRequestRecord, ModelRequestStatus
from scout_domain.state.retry import DEFAULT_RETRY_POLICY, RetryPolicy, classify_exception
from scout_llm.errors import (
    LLMAuthenticationError,
    LLMContextTooLargeError,
    LLMGatewayError,
    LLMInvalidRequestError,
    LLMRateLimitError,
    LLMResponseFormatError,
    LLMTimeoutError,
    LLMTransientError,
)
from scout_llm.json_extraction import json_object_failure_reason, try_parse_json_object
from scout_llm.observability import get_logger
from scout_llm.pricing import price_for
from scout_llm.prompts import PromptLibrary
from scout_llm.providers.base import LLMProvider
from scout_llm.types import (
    Completion,
    CompletionRequest,
    Message,
    ModelPricing,
    ModelTarget,
    ResponseFormat,
)

__all__ = [
    "GatewayResult",
    "InMemoryRecorder",
    "LLMGateway",
    "ModelRequestRecorder",
    "NullRecorder",
]

logger = get_logger(__name__)

#: An injectable sleep, so a retry test can run instantly instead of waiting for real
#: backoff. Production uses :func:`asyncio.sleep`.
type Sleeper = Callable[[float], Awaitable[None]]

#: An injectable monotonic clock, so latency assertions do not depend on wall time.
type Clock = Callable[[], float]

#: Ceiling on raw model text attached to an exception. The text is for diagnosis, and an
#: unbounded attachment would be copied into a bounded database column.
_MAX_RAW_TEXT_CHARS = 2000
#: Ceiling on the recorded error string, matching ``ModelRequestRecord.error``.
_MAX_ERROR_CHARS = 4000


class GatewayResult(BaseModel):
    """Everything one gateway call produced.

    ``record`` is the **final** attempt's record; ``attempts`` holds all of them, so a caller
    (or a dashboard) can see that a result took three tries without recomputing anything.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    completion: Completion
    text: str
    prompt_version: str
    #: SHA-256 of the prompt *template body*, identifying the versioned artefact.
    prompt_sha256: str
    record: ModelRequestRecord
    attempts: tuple[ModelRequestRecord, ...] = ()
    #: Populated only for ``response_format="json"``, and only when the text actually
    #: contained a JSON object. ``complete_json`` is the strict variant that raises instead.
    parsed: dict[str, Any] | None = None


@runtime_checkable
class ModelRequestRecorder(Protocol):
    """Persistence seam for model-request accounting.

    Declared as a protocol so the gateway never imports the persistence layer (Rule 13).
    """

    async def record(self, record: ModelRequestRecord) -> None:
        """Persist one attempt."""
        ...


class NullRecorder:
    """Recorder that discards records. Used when accounting is not wired up."""

    async def record(self, record: ModelRequestRecord) -> None:
        """Discard ``record`` without persisting it."""
        del record  # the argument exists only to satisfy ModelRequestRecorder


class InMemoryRecorder:
    """Recorder that keeps records in a list, for tests and for offline runs."""

    def __init__(self) -> None:
        self.records: list[ModelRequestRecord] = []

    async def record(self, record: ModelRequestRecord) -> None:
        """Append ``record`` in arrival order."""
        self.records.append(record)


async def _asyncio_sleep(seconds: float) -> None:
    """Default backoff sleeper."""
    await asyncio.sleep(seconds)


def _default_id_factory() -> str:
    """Mint a ``mreq_...`` identifier, matching the domain's identifier convention."""
    return new_id(IdKind.MODEL_REQUEST)


class LLMGateway:
    """Routes, retries, records, and returns. Never decides."""

    def __init__(
        self,
        *,
        providers: Mapping[str, LLMProvider],
        default_target: ModelTarget,
        prompts: PromptLibrary,
        pricing: Mapping[str, ModelPricing] | None = None,
        recorder: ModelRequestRecorder | None = None,
        retry_policy: RetryPolicy = DEFAULT_RETRY_POLICY,
        max_concurrent_calls: int = 4,
        acquire_timeout_seconds: float = 30.0,
        sleep: Sleeper | None = None,
        clock: Clock | None = None,
        id_factory: Callable[[], str] | None = None,
    ) -> None:
        if not providers:
            raise ValueError("LLMGateway requires at least one provider")
        if max_concurrent_calls < 1:
            raise ValueError("max_concurrent_calls must be at least 1")
        if acquire_timeout_seconds <= 0:
            raise ValueError("acquire_timeout_seconds must be positive")

        self._providers = dict(providers)
        self._default_target = default_target
        self._prompts = prompts
        self._pricing = pricing
        self._recorder: ModelRequestRecorder = recorder if recorder is not None else NullRecorder()
        self._retry = retry_policy
        self._max_concurrent_calls = max_concurrent_calls
        self._acquire_timeout = acquire_timeout_seconds
        self._semaphore = asyncio.Semaphore(max_concurrent_calls)
        self._sleep: Sleeper = sleep if sleep is not None else _asyncio_sleep
        self._clock: Clock = clock if clock is not None else time.perf_counter
        self._new_id: Callable[[], str] = (
            id_factory if id_factory is not None else _default_id_factory
        )
        #: Every record this gateway produced, in order, independent of the recorder. This
        #: is what makes ``usage_summary`` work offline and after a recorder failure.
        self._records: list[ModelRequestRecord] = []

    @property
    def records(self) -> tuple[ModelRequestRecord, ...]:
        """All records produced so far, oldest first."""
        return tuple(self._records)

    @property
    def providers(self) -> Mapping[str, LLMProvider]:
        """The registered providers, keyed by name."""
        return dict(self._providers)

    @property
    def max_concurrent_calls(self) -> int:
        """The concurrency ceiling enforced with a semaphore."""
        return self._max_concurrent_calls

    async def complete(
        self,
        role: AgentRole,
        prompt_version: str,
        variables: Mapping[str, str],
        *,
        model: str | None = None,
        provider: str | None = None,
        job_id: str | None = None,
        agent_run_id: str | None = None,
        max_output_tokens: int | None = None,
        response_format: ResponseFormat = "text",
    ) -> GatewayResult:
        """Load a prompt, call a model, and return the result plus accounting.

        Retries are attempted per ``RetryPolicy`` and **every** attempt is recorded.

        Raises:
            PromptNotFoundError: the prompt version does not exist (nothing is recorded —
                no provider call happened).
            PromptRenderError: the template and the supplied variables disagree.
            LLMInvalidRequestError: no provider is registered under the target name.
            LLMGatewayError: the original typed provider failure, re-raised unchanged once
                retries are exhausted (Rule 12 — never wrapped in something generic).
        """
        template = self._prompts.load(role, prompt_version)
        rendered = template.render(**dict(variables))
        target = self._resolve_target(provider=provider, model=model)

        provider_impl = self._providers.get(target.provider)
        if provider_impl is None:
            raise LLMInvalidRequestError(
                f"no LLM provider registered for {target.provider!r}",
                provider=target.provider,
                registered=sorted(self._providers),
            )

        request = CompletionRequest(
            # The rendered template is the user turn. Anthropic's system-prompt hoisting is
            # exercised by providers that receive an explicit system message; the prompt
            # library deliberately does not invent a second, implicit "system" convention.
            messages=[Message(role="user", content=rendered)],
            model=target.model,
            max_output_tokens=max_output_tokens,
            response_format=response_format,
            metadata=self._call_metadata(
                role=role,
                prompt_version=template.version,
                prompt_sha256=template.sha256,
                job_id=job_id,
                agent_run_id=agent_run_id,
            ),
        )

        completion, records = await self._call_with_retry(
            provider_impl=provider_impl,
            target=target,
            request=request,
            role=role,
            prompt_version=template.version,
            rendered_digest=hashlib.sha256(rendered.encode("utf-8")).hexdigest(),
            priced_as=price_for(target.provider, target.model, table=self._pricing),
            job_id=job_id,
            agent_run_id=agent_run_id,
        )

        parsed = try_parse_json_object(completion.text) if response_format == "json" else None

        return GatewayResult(
            completion=completion,
            text=completion.text,
            prompt_version=template.version,
            prompt_sha256=template.sha256,
            record=records[-1],
            attempts=tuple(records),
            parsed=parsed,
        )

    async def complete_json(
        self,
        role: AgentRole,
        prompt_version: str,
        variables: Mapping[str, str],
        *,
        model: str | None = None,
        provider: str | None = None,
        job_id: str | None = None,
        agent_run_id: str | None = None,
        max_output_tokens: int | None = None,
    ) -> tuple[GatewayResult, dict[str, Any]]:
        """Like :meth:`complete`, but require a JSON object in the response.

        Raises:
            LLMResponseFormatError: the response contained no JSON object, or contained a
                JSON value that was not an object. The raw text is attached.
        """
        result = await self.complete(
            role,
            prompt_version,
            variables,
            model=model,
            provider=provider,
            job_id=job_id,
            agent_run_id=agent_run_id,
            max_output_tokens=max_output_tokens,
            response_format="json",
        )
        if result.parsed is None:
            # Re-diagnose strictly so the error says *why* (no JSON at all, or JSON that was
            # not an object) instead of repeating the lenient parse's silence.
            reason = json_object_failure_reason(result.text) or "the response was not a JSON object"
            raise LLMResponseFormatError(
                f"model response did not contain a JSON object: {reason}",
                provider=result.record.provider,
                model=result.record.model,
                prompt_version=result.prompt_version,
                raw_text=result.text[:_MAX_RAW_TEXT_CHARS],
            )
        return result, result.parsed

    async def usage_summary(self) -> dict[str, Any]:
        """Aggregate recorded usage by provider/model, for the cost dashboard.

        Counts **attempts**, not logical calls: a call that retried three times contributes
        three rows and three times its tokens, which is what the provider will bill.
        """
        buckets: dict[str, dict[str, Any]] = {}
        for record in self._records:
            key = f"{record.provider}/{record.model}"
            bucket = buckets.setdefault(
                key,
                {
                    "provider": record.provider,
                    "model": record.model,
                    "attempts": 0,
                    "succeeded": 0,
                    "failed": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "estimated_cost_usd": 0.0,
                },
            )
            bucket["attempts"] += 1
            if record.status is ModelRequestStatus.SUCCEEDED:
                bucket["succeeded"] += 1
            else:
                bucket["failed"] += 1
            bucket["input_tokens"] += record.input_tokens
            bucket["output_tokens"] += record.output_tokens
            bucket["estimated_cost_usd"] += record.estimated_cost_usd

        return {
            "total_attempts": len(self._records),
            "total_input_tokens": sum(record.input_tokens for record in self._records),
            "total_output_tokens": sum(record.output_tokens for record in self._records),
            "total_estimated_cost_usd": sum(record.estimated_cost_usd for record in self._records),
            "by_model": buckets,
        }

    async def aclose(self) -> None:
        """Close every registered provider once. Safe to call more than once."""
        closed: set[int] = set()
        for provider in self._providers.values():
            if id(provider) in closed:
                continue
            closed.add(id(provider))
            await provider.aclose()

    async def __aenter__(self) -> LLMGateway:
        """Enter an async context; the gateway closes providers on exit."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Close providers on context exit."""
        await self.aclose()

    # ------------------------------------------------------------------ internals

    def _resolve_target(self, *, provider: str | None, model: str | None) -> ModelTarget:
        """Apply per-call overrides on top of the configured default target."""
        return ModelTarget(
            provider=provider or self._default_target.provider,
            model=model or self._default_target.model,
        )

    def _call_metadata(
        self,
        *,
        role: AgentRole,
        prompt_version: str,
        prompt_sha256: str,
        job_id: str | None,
        agent_run_id: str | None,
    ) -> dict[str, str]:
        """Correlation data attached to the transport request but never transmitted."""
        metadata = {
            "agent_role": role.value,
            "prompt_version": prompt_version,
            "prompt_sha256": prompt_sha256,
        }
        if job_id:
            metadata["job_id"] = job_id
        if agent_run_id:
            metadata["agent_run_id"] = agent_run_id
        return metadata

    async def _call_with_retry(
        self,
        *,
        provider_impl: LLMProvider,
        target: ModelTarget,
        request: CompletionRequest,
        role: AgentRole,
        prompt_version: str,
        rendered_digest: str,
        priced_as: ModelPricing,
        job_id: str | None,
        agent_run_id: str | None,
    ) -> tuple[Completion, list[ModelRequestRecord]]:
        """Call the provider, retrying per policy and recording every attempt."""
        records: list[ModelRequestRecord] = []

        for attempt in range(1, self._retry.max_attempts + 1):
            started = self._clock()
            try:
                completion = await self._invoke(provider_impl, request, model=target.model)
                self._reject_hollow_completion(completion)
            except Exception as exc:
                latency = max(0.0, self._clock() - started)
                record = self._build_record(
                    attempt=attempt,
                    status=self._status_for(exc),
                    role=role,
                    target=target,
                    prompt_version=prompt_version,
                    rendered_digest=rendered_digest,
                    job_id=job_id,
                    agent_run_id=agent_run_id,
                    latency_seconds=latency,
                    error=self._describe_error(exc),
                )
                records.append(record)
                await self._emit(record)

                if not self._retry.should_retry(
                    attempt=attempt,
                    failure_class=classify_exception(exc),
                ):
                    # Re-raise the original typed error untouched (Rule 12). It is already
                    # registered with its own retry classification for the orchestrator.
                    raise

                delay = self._retry_delay(exc, attempt)
                logger.warning(
                    "retrying LLM call: provider=%s model=%s attempt=%d/%d delay=%.2fs error=%s",
                    target.provider,
                    target.model,
                    attempt,
                    self._retry.max_attempts,
                    delay,
                    self._describe_error(exc),
                )
                await self._sleep(delay)
                continue

            latency = (
                completion.latency_seconds
                if completion.latency_seconds > 0.0
                else max(0.0, self._clock() - started)
            )
            record = self._build_record(
                attempt=attempt,
                status=ModelRequestStatus.SUCCEEDED,
                role=role,
                target=target,
                prompt_version=prompt_version,
                rendered_digest=rendered_digest,
                job_id=job_id,
                agent_run_id=agent_run_id,
                latency_seconds=latency,
                input_tokens=completion.usage.input_tokens,
                output_tokens=completion.usage.output_tokens,
                estimated_cost_usd=priced_as.estimate_cost(completion.usage),
            )
            records.append(record)
            await self._emit(record)
            return completion, records

        # Unreachable while RetryPolicy enforces max_attempts >= 1, and worth a typed
        # failure rather than a bare ``assert`` if that invariant ever changes.
        raise LLMGatewayError(
            "retry loop exited without a result",
            provider=target.provider,
            model=target.model,
            attempts=len(records),
        )

    async def _invoke(
        self,
        provider_impl: LLMProvider,
        request: CompletionRequest,
        *,
        model: str,
    ) -> Completion:
        """Acquire a concurrency slot and perform exactly one provider call.

        The slot is held only for the call itself — never across backoff — so a retrying
        call does not starve others.
        """
        try:
            async with asyncio.timeout(self._acquire_timeout):
                await self._semaphore.acquire()
        except TimeoutError as exc:
            raise LLMTransientError(
                f"timed out after {self._acquire_timeout:g}s waiting for one of "
                f"{self._max_concurrent_calls} model-call slots",
                max_concurrent_calls=self._max_concurrent_calls,
                acquire_timeout_seconds=self._acquire_timeout,
            ) from exc

        try:
            return await provider_impl.complete(request, model=model)
        finally:
            self._semaphore.release()

    def _reject_hollow_completion(self, completion: Completion) -> None:
        """Refuse a "successful" completion that reported no output tokens.

        ``ModelRequestRecord`` rejects a ``SUCCEEDED`` row with zero output tokens, because
        that shape almost always means a silent upstream failure. Raising the typed error
        here keeps the contract visible instead of failing inside record validation.
        """
        if completion.usage.output_tokens > 0:
            return
        raise LLMResponseFormatError(
            "provider reported a successful completion with zero output tokens",
            provider=completion.provider,
            model=completion.model,
            raw_text=completion.text[:_MAX_RAW_TEXT_CHARS] if completion.text else None,
        )

    async def _emit(self, record: ModelRequestRecord) -> None:
        """Keep the record in memory and hand it to the recorder.

        A recorder that fails is logged loudly but does not replace the provider outcome:
        losing an accounting row must not lose a verification result, and the record is
        still available through :attr:`records` and :meth:`usage_summary`.
        """
        self._records.append(record)
        try:
            await self._recorder.record(record)
        except Exception:
            logger.exception(
                "failed to persist model request record id=%s status=%s",
                record.id,
                record.status.value,
            )

    def _retry_delay(self, exc: BaseException, attempt: int) -> float:
        """Backoff for the next attempt, honouring a provider's ``retry-after``.

        The provider's requested delay is treated as a *floor*, not a replacement: waiting
        less than asked is how a rate-limited client turns a hiccup into a ban. The policy's
        ceiling still applies so a hostile header cannot park a worker for an hour.
        """
        delay = self._retry.delay_for_attempt(attempt)
        retry_after = getattr(exc, "retry_after_seconds", None)
        if isinstance(retry_after, (int, float)) and not isinstance(retry_after, bool):
            delay = max(delay, float(retry_after))
        return min(delay, self._retry.max_delay_seconds)

    @staticmethod
    def _status_for(exc: BaseException) -> ModelRequestStatus:
        """Map a failure onto the domain's ``ModelRequestStatus``.

        Ordered most-specific first: rate limit and timeout are subclasses of the generic
        transient error, so a check for the parent first would mislabel them.
        """
        if isinstance(exc, LLMRateLimitError):
            return ModelRequestStatus.RATE_LIMITED
        if isinstance(exc, (LLMTimeoutError, TimeoutError)):
            return ModelRequestStatus.TIMEOUT
        if isinstance(
            exc,
            (LLMAuthenticationError, LLMInvalidRequestError, LLMContextTooLargeError),
        ):
            return ModelRequestStatus.REJECTED
        return ModelRequestStatus.FAILED

    @staticmethod
    def _describe_error(exc: BaseException) -> str:
        """Return a bounded, credential-free description of ``exc``.

        ``ScoutDomainError.__str__`` renders the exception's whole context, which can
        include a model's raw response. The bare ``message`` is used instead so the recorded
        error is bounded and readable, while the full context stays on the exception itself
        for a caller that wants it.
        """
        text = exc.message if isinstance(exc, ScoutDomainError) else str(exc) or type(exc).__name__
        return text[:_MAX_ERROR_CHARS]

    def _build_record(
        self,
        *,
        attempt: int,
        status: ModelRequestStatus,
        role: AgentRole,
        target: ModelTarget,
        prompt_version: str,
        rendered_digest: str,
        job_id: str | None,
        agent_run_id: str | None,
        latency_seconds: float,
        input_tokens: int = 0,
        output_tokens: int = 0,
        estimated_cost_usd: float = 0.0,
        error: str | None = None,
    ) -> ModelRequestRecord:
        """Build the accounting row for one attempt."""
        return ModelRequestRecord(
            id=self._new_id(),
            job_id=job_id,
            agent_run_id=agent_run_id,
            agent=role,
            provider=target.provider,
            model=target.model,
            prompt_version=prompt_version,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_seconds=latency_seconds,
            status=status,
            estimated_cost_usd=estimated_cost_usd,
            # The *rendered* digest, because that is what the model actually saw; the
            # template's own digest travels on GatewayResult.prompt_sha256.
            prompt_digest=rendered_digest,
            attempt=attempt,
            error=error,
        )
