"""The gateway's accounting record satisfies the domain contract.

``ModelRequestRecord`` is owned by ``scout-domain``; the gateway merely produces it. This
test proves the produced rows validate, round-trip, and serialise, so a persistence layer or
dashboard can consume them without a translation step.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from scout_domain.enums import AgentRole
from scout_domain.models.model_request import ModelRequestRecord, ModelRequestStatus
from scout_domain.state.retry import RetryPolicy
from scout_llm.errors import LLMInvalidRequestError, LLMTransientError
from scout_llm.gateway import InMemoryRecorder, LLMGateway
from scout_llm.prompts import PromptLibrary
from scout_llm.providers.fake import FakeProvider
from scout_llm.types import ModelTarget

pytestmark = pytest.mark.contract

ROLE = AgentRole.CODE_REVIEWER
BODY = "Review this diff:\n$diff\n"

DOMAIN_FIELDS = {
    "id",
    "job_id",
    "agent_run_id",
    "agent",
    "provider",
    "model",
    "prompt_version",
    "input_tokens",
    "output_tokens",
    "latency_seconds",
    "status",
    "estimated_cost_usd",
    "prompt_digest",
    "attempt",
    "error",
    "created_at",
}


def build_gateway(root: Path, *, recorder: InMemoryRecorder | None = None) -> LLMGateway:
    """Build a gateway whose fake provider can be scripted by the caller."""
    directory = root / ROLE.value
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "v1.md").write_text(BODY, encoding="utf-8")

    return LLMGateway(
        providers={"fake": FakeProvider()},
        default_target=ModelTarget(provider="fake", model="fake-deterministic"),
        prompts=PromptLibrary(root),
        recorder=recorder,
    )


def scripted_provider(gateway: LLMGateway) -> FakeProvider:
    """Return the gateway's fake provider, narrowing its type for the test."""
    provider = gateway.providers["fake"]
    assert isinstance(provider, FakeProvider)
    return provider


async def test_a_successful_record_round_trips_through_the_domain_model(tmp_path: Path) -> None:
    recorder = InMemoryRecorder()
    gateway = build_gateway(tmp_path, recorder=recorder)

    result = await gateway.complete(ROLE, "v1", {"diff": "+x"}, job_id="job_1")
    record = result.record

    assert isinstance(record, ModelRequestRecord)
    assert ModelRequestRecord.model_validate(record.model_dump()) == record
    assert recorder.records == [record]


async def test_the_serialised_record_has_exactly_the_domain_fields(tmp_path: Path) -> None:
    gateway = build_gateway(tmp_path)

    result = await gateway.complete(ROLE, "v1", {"diff": "+x"})
    payload = result.record.model_dump(mode="json")

    assert set(payload) == DOMAIN_FIELDS
    assert payload["status"] == "SUCCEEDED"
    assert payload["agent"] == ROLE.value
    assert payload["prompt_digest"] is not None


async def test_a_failed_record_round_trips_and_carries_an_error(tmp_path: Path) -> None:
    gateway = build_gateway(tmp_path)
    scripted_provider(gateway).fail_with(LLMInvalidRequestError("nope"), times=1)

    with pytest.raises(LLMInvalidRequestError):
        await gateway.complete(ROLE, "v1", {"diff": "+x"})

    failed = gateway.records[0]
    assert failed.status is ModelRequestStatus.REJECTED
    assert failed.error
    assert ModelRequestRecord.model_validate(failed.model_dump()) == failed


async def test_every_attempt_of_a_retried_call_is_a_distinct_valid_record(tmp_path: Path) -> None:
    gateway = build_gateway(tmp_path)
    scripted_provider(gateway).fail_with(LLMTransientError("down"), times=1)

    result = await gateway.complete(ROLE, "v1", {"diff": "+x"})

    assert len(result.attempts) == 2
    assert [record.attempt for record in result.attempts] == [1, 2]
    assert len({record.id for record in result.attempts}) == 2
    for record in result.attempts:
        assert ModelRequestRecord.model_validate(record.model_dump()) == record
    assert result.attempts[0].status is ModelRequestStatus.FAILED
    assert result.attempts[1].status is ModelRequestStatus.SUCCEEDED
    assert RetryPolicy().max_attempts >= 2


async def test_records_use_the_domain_identifier_convention(tmp_path: Path) -> None:
    gateway = build_gateway(tmp_path)

    result = await gateway.complete(ROLE, "v1", {"diff": "+x"})

    assert result.record.id.startswith("mreq_")
    assert len(result.record.id) > len("mreq_")


def test_the_domain_rejects_a_record_with_an_unexpected_field() -> None:
    with pytest.raises(ValidationError):
        ModelRequestRecord.model_validate(
            {
                "id": "mreq_1",
                "provider": "fake",
                "model": "m",
                "prompt_version": "v1",
                "status": "SUCCEEDED",
                "output_tokens": 1,
                "surprise": True,
            }
        )


def test_the_domain_rejects_a_succeeded_record_with_no_output_tokens() -> None:
    """This is exactly the shape the gateway refuses to produce."""
    with pytest.raises(ValidationError, match="must report output tokens"):
        ModelRequestRecord(
            id="mreq_1",
            provider="fake",
            model="m",
            prompt_version="v1",
            status=ModelRequestStatus.SUCCEEDED,
            output_tokens=0,
        )


def test_the_domain_rejects_a_failed_record_without_an_error() -> None:
    with pytest.raises(ValidationError, match="must record an error"):
        ModelRequestRecord(
            id="mreq_1",
            provider="fake",
            model="m",
            prompt_version="v1",
            status=ModelRequestStatus.FAILED,
        )
