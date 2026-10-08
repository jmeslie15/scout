"""Contract tests for notification payloads (Rule 11, spec section 56).

Contracts here are the *external* shapes: the ``NotificationRecord`` domain model that the
dashboard and the audit trail consume, the JSON body Google Chat receives, and the MIME
message SMTP receives. Each is validated with the real parser rather than by string
matching, so a silent shape change fails the suite.
"""

from __future__ import annotations

import email
import email.policy
import json

import httpx
import pytest

from scout_domain.enums import NotificationChannel, Recommendation, Severity
from scout_domain.models.notification import NotificationRecord, NotificationStatus
from scout_notifications.base import NotificationMessage
from scout_notifications.channels.email import build_email_message
from scout_notifications.channels.google_chat import GoogleChatChannelAdapter
from scout_notifications.routing import RoutingPolicy
from scout_notifications.service import NotificationService

pytestmark = pytest.mark.contract

WEBHOOK_URL = "https://chat.googleapis.com/v1/spaces/AAA/messages?key=CONTRACT-KEY&token=T"


def _service(adapters) -> NotificationService:
    return NotificationService(
        adapters=adapters,
        routing=RoutingPolicy(notifications_enabled=True),
        job_url_builder=lambda job: f"https://scout.test/jobs/{job.id}",
    )


@pytest.mark.parametrize(
    "scenario",
    ["question", "finding", "verification_complete", "approval_required"],
)
async def test_records_validate_against_the_domain_model(
    scenario: str,
    fake_adapter,
    make_job,
    make_question,
    make_finding,
    make_report,
) -> None:
    github = fake_adapter(NotificationChannel.GITHUB)
    chat = fake_adapter(NotificationChannel.GOOGLE_CHAT)
    email_adapter = fake_adapter(NotificationChannel.EMAIL)
    service = _service([github, chat, email_adapter])
    job = make_job()

    if scenario == "question":
        records = await service.notify_question(make_question(severity=Severity.HIGH), job)
    elif scenario == "finding":
        records = await service.notify_high_severity_finding(make_finding(), job)
    elif scenario == "verification_complete":
        records = await service.notify_verification_complete(
            make_report(recommendation=Recommendation.REQUEST_CHANGES), job
        )
    else:
        records = await service.notify_approval_required(job)

    assert records, f"{scenario} produced no notification records"
    for record in records:
        # ``NotificationRecord`` forbids extra fields, so this is a real schema check.
        validated = NotificationRecord.model_validate(record.model_dump(mode="json"))
        assert validated.id == record.id
        assert validated.dedup_key
        assert validated.subject
        assert validated.body
        assert validated.status in {
            NotificationStatus.SENT,
            NotificationStatus.FAILED,
            NotificationStatus.SUPPRESSED,
        }


async def test_every_record_has_a_terminal_status_and_consistent_fields(
    fake_adapter,
    make_question,
    make_job,
) -> None:
    github = fake_adapter(NotificationChannel.GITHUB)
    chat = fake_adapter(NotificationChannel.GOOGLE_CHAT)
    service = _service([github, chat])

    records = await service.notify_question(make_question(severity=Severity.CRITICAL), make_job())

    for record in records:
        payload = record.model_dump(mode="json")
        reparsed = NotificationRecord.model_validate(payload)
        if reparsed.status is NotificationStatus.SENT:
            assert reparsed.sent_at is not None
            assert reparsed.error is None
        if reparsed.status is NotificationStatus.FAILED:
            assert reparsed.error


async def test_google_chat_request_body_is_json_with_a_text_field(
    respx_mock,
    make_question,
    make_job,
) -> None:
    route = respx_mock.post(WEBHOOK_URL).mock(
        return_value=httpx.Response(200, json={"name": "spaces/AAA/messages/CONTRACT"})
    )
    adapter = GoogleChatChannelAdapter(webhook_url=WEBHOOK_URL)
    question = make_question()
    message = NotificationMessage(
        subject="Contract check",
        body="Should managers see export metadata?",
        severity=question.severity,
        job_url="https://scout.test/jobs/job_1",
    )

    try:
        result = await adapter.send(message)
    finally:
        await adapter.aclose()

    assert result.delivered is True
    request = route.calls[0].request
    assert request.headers["content-type"] == "application/json"

    payload = json.loads(request.content)
    assert isinstance(payload, dict)
    assert set(payload) == {"text"}
    assert isinstance(payload["text"], str)
    assert payload["text"]


async def test_email_message_is_multipart_alternative_with_both_parts(
    fake_adapter,
    make_question,
    make_job,
) -> None:
    email_adapter = fake_adapter(NotificationChannel.EMAIL)
    service = _service([email_adapter])
    await service.notify_question(make_question(severity=Severity.CRITICAL), make_job())
    message = email_adapter.sent[0]

    raw = build_email_message(
        message,
        sender="scout@acme.test",
        recipients=["oncall@acme.test"],
        html_body=message.metadata.get("html_body"),
    )

    parsed = email.message_from_string(raw.as_string(), policy=email.policy.default)
    assert parsed.is_multipart()
    assert parsed.get_content_type() == "multipart/alternative"
    subtypes = {part.get_content_subtype() for part in parsed.iter_parts()}
    assert subtypes == {"plain", "html"}
    assert parsed["Subject"] == message.subject
